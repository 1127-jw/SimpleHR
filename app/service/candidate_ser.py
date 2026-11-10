import os
from fastapi import HTTPException, UploadFile, status,BackgroundTasks
from sqlalchemy.ext.asyncio import AsyncSession
from uuid import uuid4
from pathlib import Path
from app.repository.resume_repo import ResumeRepo, CandidateRepo
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
        candidate_dict = candidate_data.model_dump()
        candidate_dict['creator_id'] = current_user.id
        candidate_repo = CandidateRepo(session=session)
        candidate = await candidate_repo.create_candidate(candidate_dict)
        candidate_schema = CandidateSchema.model_validate(candidate)
        position_schema = PositionSchema.model_validate(candidate.position)
        interviewer_schema = UserSH.model_validate(candidate.position.creator)

    return candidate_schema, position_schema, interviewer_schema