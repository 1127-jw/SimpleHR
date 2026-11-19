import os
from fastapi import HTTPException, UploadFile, status,BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from uuid import uuid4
from pathlib import Path
from app.repository.resume_repo import ResumeRepo, CandidateRepo
from app.repository.candidate_history_repo import CandidateStatusHistoryRepo
from app.models.user import UserModel
from app.models.candidate import ResumeModel
from app.schemas.candidate_schema import TaskInfoSchema, CandidateCreateSchema, CandidateSchema
from app.core.exceptions import ResumeProcessingError
from app.tasks.ocr_tasks import ocr_parse_resume_task
from app.schemas.position_schema import PositionSchema
from app.schemas.user_schema import UserSH
from utils.PDFser import WordToPdfConverter
import logging

import aiofiles
from redis.asyncio import Redis
from app.core.config import get_settings

logger = logging.getLogger(__name__)

settings = get_settings()


async def resume_upload_service(
        session: AsyncSession,
        file:UploadFile,
        current_user:UserModel,
):
    # 1. 校验文件类型
    # 简历：图片、pdf、word
    allowed_mime_types = [
        "application/pdf",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "image/jpeg",
        "image/png",
        "image/jpg",
    ]
    if file.content_type not in allowed_mime_types:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="该文件不支持！")

    # 2. 保存文件
    resume_dir = settings.RESUME_DIR
    file_extension = os.path.splitext(file.filename)[-1]
    unique_filename = f"{uuid4()}{file_extension}"
    file_path = os.path.join(resume_dir, unique_filename)

    try:
        async with aiofiles.open(file_path, mode="wb") as fp:
            content = await file.read(1024)
            while content:
                await fp.write(content)
                content = await file.read(1024)

    except OSError as e:
        logger.error("文件写入失败: %s", e)
        raise ResumeProcessingError(f"简历文件写入失败: {e}") from e


    # 3. 如果是 word 文档，转化为 PDF
    if file_extension in (".doc", ".docx"):
        pdf_path = file_path.replace(file_extension, ".pdf")
        converter = WordToPdfConverter(
            word_path=file_path,
            output_pdf_path=pdf_path,
        )
        try:
            await converter.convert()
            file_path = pdf_path
        except Exception as e:
            logger.error("Word 转 PDF 失败: %s", e)
            raise ResumeProcessingError(f"Word 转 PDF 失败: {e}") from e

    # 4. 将简历数据存储到数据库中
    async with session.begin():
        resume_repo = ResumeRepo(session=session)
        # 在数据库中只保存文件名
        file_name = Path(file_path).name
        resume = await resume_repo.create_resume(file_path=file_name, uploader_id=current_user.id)
        return resume


async def parse_resume_service(
        session: AsyncSession,
        r:Redis,
        resume_id:str,
        task_id:str,
        background_tasks:BackgroundTasks
):

    async with session.begin():
        resume_repo = ResumeRepo(session=session)
        resume: ResumeModel = await resume_repo.get_by_id(resume_id)
    file_path = os.path.join(settings.RESUME_DIR, resume.file_path)
    key = f"task:{task_id}"
    task_info = TaskInfoSchema(task_id=task_id, status='pending')
    await r.set(key, task_info.model_dump_json(), ex=60*60)
    background_tasks.add_task(ocr_parse_resume_task,file_path, r, task_id, key)


async def create_candidate_ser(
        session: AsyncSession,
        current_user:UserModel,
        candidate_data:CandidateCreateSchema
):
    async with session.begin():
        # 建图查重：同邮箱已有活跃流程就不允许重复创建。
        # 归档（'-1'）不在活跃集合里，所以归档后仍可用同一邮箱再投递。
        active = await CandidateStatusHistoryRepo(session).get_active_by_email(
            candidate_data.email
        )
        if active is not None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"邮箱 {candidate_data.email} 已有进行中的流程，不能重复创建",
            )

        # skip_scoring 是流程控制参数，不是 CandidateModel 的字段
        candidate_dict = candidate_data.model_dump(exclude={"skip_scoring"})
        candidate_dict['creator_id'] = current_user.id
        candidate_repo = CandidateRepo(session=session)
        candidate = await candidate_repo.create_candidate(candidate_dict)
        candidate_schema = CandidateSchema.model_validate(candidate)
        position_schema = PositionSchema.model_validate(candidate.position)
        interviewer_schema = UserSH.model_validate(candidate.position.creator)

        # 链头：创建即「已投递」，后续每次状态变更都往这条链上追加。
        # 没有链头的话，建图查重会认为该邮箱「无记录」而放过重复投递。
        from app.models.candidate import CandidateStatusEnum

        await CandidateStatusHistoryRepo(session).record_change(
            candidate_schema.id,
            to_status=CandidateStatusEnum.APPLICATION.value,
            candidate_email=candidate_schema.email,
            position_id=position_schema.id,
            position_title=position_schema.title,
        )

    return candidate_schema, position_schema, interviewer_schema


# 学历解析规则：按优先级从高到低匹配，命中即返回对应层级
EDUCATION_RULES = [
    ("博士", "博士"),
    ("硕士", "硕士"),
    ("研究生", "硕士"),
    ("本科", "本科"),
    ("学士", "本科"),
    ("大专", "大专"),
    ("专科", "大专"),
    ("中专", "中专/高中"),
    ("高中", "中专/高中"),
]


def parse_education(text: str | None) -> str:
    """从教育经历自由文本里推断学历层级，推断不出来记为「未知」。"""
    if not text:
        return "未知"
    for keyword, label in EDUCATION_RULES:
        if keyword in text:
            return label
    return "未知"


async def candidate_distribution_ser(session: AsyncSession) -> dict:
    """看板三个饼图的数据：学历分布 / 状态分布 / 职位分布。"""
    rows = await CandidateRepo(session).distribution_rows()

    education: dict[str, int] = {}
    status: dict[str, int] = {}
    position: dict[str, int] = {}

    for candidate_status, education_experience, position_title in rows:
        edu_label = parse_education(education_experience)
        education[edu_label] = education.get(edu_label, 0) + 1
        status[candidate_status] = status.get(candidate_status, 0) + 1
        position[position_title] = position.get(position_title, 0) + 1

    def to_list(counts: dict[str, int]) -> list[dict]:
        return [{"name": name, "value": value} for name, value in counts.items()]

    return {
        "education": to_list(education),
        "status": to_list(status),
        "position": to_list(position),
    }