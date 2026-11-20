import os

from datetime import timedelta
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, UploadFile, File, BackgroundTasks, Query, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from typing import Annotated, List
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.audit import audit
from app.core.db import get_db
from app.core.auth import get_current_user, get_hr_or_superuser
from app.core.config import get_settings
from app.core.exceptions import StatusFlowError
from app.models.user import UserModel
from app.service.candidate_status_ser import resume_manual_decision
from uuid import uuid4
from app.schemas.candidate_schema import CandidateListSchema, CandidateStatusUpdateSchema, CandidateAIScoreRespSchema
from app.core.time import db_now

from app.schemas.candidate_schema import ResumeUploadRespSchema, ResumePaseSchema, ResumeParseTaskRespSchema, \
    TaskInfoSchema,CandidateCreateSchema
from app.service.candidate_ser import resume_upload_service, parse_resume_service, create_candidate_ser, candidate_distribution_ser
from app.service.candidate_status_ser import validate_transition, trigger_manual_retry, MANUAL_RETRY_SOURCES

from redis.asyncio import Redis
from app.core.redis_client import get_redis
from app.schemas.response_schema import ResponseSchema
from app.repository.resume_repo import CandidateRepo, CandidateAIScoreRepo, ResumeRepo
from app.tasks.agent_tasks import run_candidate_agent
from app.models.candidate import CandidateStatusEnum
from app.models import InterviewModel
from app.models.InterviewModel import InterviewResultEnum
from app.repository.Interview_repo import InterviewRepo
from utils.ocr_service import PaddleOcr
settings = get_settings()


router = APIRouter(prefix="/candidate", tags=["candidate"])

@router.post("/resume/upload", summary="上传简历", response_model=ResumeUploadRespSchema)
async def resume_upload(
    session:Annotated[AsyncSession, Depends(get_db)],
    current_user:Annotated[UserModel,Depends(get_current_user)],
    file: UploadFile = File(...),
):
    resume = await resume_upload_service(session,file,current_user)
    return {"resume": resume}

@router.post("/resume/parse", summary="简历解析", response_model=ResumeParseTaskRespSchema)
async def parse_resume(
    resume_data: ResumePaseSchema,

    background_tasks: BackgroundTasks,
    session:Annotated[AsyncSession, Depends(get_db)],
    _: UserModel = Depends(get_current_user),
    r: Redis = Depends(get_redis),
):
    # 创建一个识别简历的后台任务
    task_id = str(uuid4())
    await parse_resume_service(
        session,
        r,
        resume_id=resume_data.resume_id,
        task_id=task_id,
        background_tasks=background_tasks
    )
    return {"task_id": task_id}

@router.get("/resume/parse/{task_id}", summary="获取任务状态", response_model=TaskInfoSchema)
async def get_task_status(
    task_id: str,
    r: Redis = Depends(get_redis),
    _: UserModel = Depends(get_current_user)
):
    key = f"task:{task_id}"
    task_json = await r.get(key)
    if not task_json:
        raise HTTPException(status_code=404, detail=f"任务 {task_id} 不存在或已过期")
    task_info = TaskInfoSchema.model_validate_json(task_json)
    return task_info

@router.post("/create",summary="创建候选人")
async def create_candidate(
    candidate_data:CandidateCreateSchema,
    session:Annotated[AsyncSession, Depends(get_db)],
    current_user:Annotated[UserModel,Depends(get_current_user)],
    background_tasks: BackgroundTasks,
):
    candidate_schema, position_schema, interviewer_schema= await create_candidate_ser(session,current_user,candidate_data)

    # 「跳过筛选」等于绕过 AI 评估，只允许 HR / 超管使用。
    # 普通员工即使传了也直接拒绝，避免越权。
    if candidate_data.skip_scoring and not (
        current_user.is_hr or current_user.is_superuser
    ):
        raise HTTPException(
            status_code=403,
            detail="仅 HR / 超管可跳过 AI 筛选与评分",
        )

    background_tasks.add_task(
        run_candidate_agent,
        candidate=candidate_schema,
        position=position_schema,
        interviewer=interviewer_schema,
        skip_scoring=candidate_data.skip_scoring,
    )
    return ResponseSchema()

@router.get("/list", summary="获取候选人列表", response_model=CandidateListSchema)
async def get_candidate_list(
    session:Annotated[AsyncSession, Depends(get_db)],
    page: int = Query(1, ge=1, description="页码，从 1 开始"),
    size: int = Query(20, ge=1, le=100, description="每页数量，1-100"),
    position_id: str|None = Query(None, description="职位的ID"),
    status: CandidateStatusEnum|None = Query(None, description="候选人状态"),
    current_user: UserModel = Depends(get_current_user),
):
    async with session.begin():
        candidate_repo = CandidateRepo(session=session)
        candidates, total = await candidate_repo.get_list(
            current_user=current_user,
            position_id=position_id,
            status=status,
            page=page,
            size=size,
        )
        return {"candidates": candidates, "total": total}


def _resolve_resume_path(file_name: str) -> Path:
    """把库里存的相对文件名解析为绝对路径，并阻断路径穿越。"""
    base = Path(settings.RESUME_DIR).resolve()
    path = (base / file_name).resolve()
    if base not in path.parents:
        raise HTTPException(status_code=400, detail="非法的简历路径")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="简历文件不存在")
    return path


# 简历附件不全是 PDF：上传 doc/docx 后为避免二次转换失败会原样落盘。
# 硬编码 application/pdf 会让浏览器用错误的阅读器打开。
_MEDIA_TYPES = {
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


def _resume_response(path: Path) -> FileResponse:
    media_type = _MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(path, media_type=media_type, filename=path.name)


@router.get("/{candidate_id}/resume", summary="下载候选人简历")
async def download_candidate_resume(
    candidate_id: str,
    session: Annotated[AsyncSession, Depends(get_db)],
    current_user: UserModel = Depends(get_current_user),
):
    """
    需要登录，并且只能下载自己有权访问的候选人的简历。
    """
    async with session.begin():
        candidate = await CandidateRepo(session).get_for_user(candidate_id, current_user)
        if candidate is None:
            # 「不存在」与「无权访问」返回同一状态，避免被用来枚举候选人 id
            raise HTTPException(status_code=404, detail="候选人不存在或无权访问")
        if candidate.resume is None:
            raise HTTPException(status_code=404, detail="该候选人没有简历")
        file_name = candidate.resume.file_path

    path = _resolve_resume_path(file_name)
    audit(
        "resume.download",
        user_id=current_user.id,
        target=f"candidate:{candidate_id}",
        detail=file_name,
    )
    return _resume_response(path)


@router.get("/resume/{resume_id}", summary="下载简历文件（上传后预览）")
async def download_resume_file(
    resume_id: str,
    session: Annotated[AsyncSession, Depends(get_db)],
    current_user: UserModel = Depends(get_current_user),
):
    """预览场景：简历刚上传、还没关联候选人，只允许上传者本人下载。"""
    async with session.begin():
        resume = await ResumeRepo(session).get_by_id_or_none(resume_id)
        if resume is None:
            raise HTTPException(status_code=404, detail="简历不存在")
        if resume.uploader_id != current_user.id and not current_user.is_superuser:
            raise HTTPException(status_code=403, detail="无权访问该简历")
        file_name = resume.file_path

    path = _resolve_resume_path(file_name)
    audit(
        "resume.preview",
        user_id=current_user.id,
        target=f"resume:{resume_id}",
        detail=file_name,
    )
    return _resume_response(path)


class ManualDecisionSchema(BaseModel):
    """人工闸门决定。"""

    decision: Literal["approve", "reject", "archive"] = Field(
        ...,
        description=(
            "approve=发送面试邀请邮件并继续；reject=不发送，淘汰；"
            "archive=直接归档收尾（流程终止，状态保持细分值）"
        ),
    )


@router.post("/{candidate_id}/manual-decision", summary="人工决定是否发送面试邀请", response_model=ResponseSchema)
async def manual_decision(
    candidate_id: str,
    payload: ManualDecisionSchema,
    session: Annotated[AsyncSession, Depends(get_db)],
    current_user: UserModel = Depends(get_hr_or_superuser),
):
    """唤醒停在「待人工确认」的招聘图。

    权限与 ``PATCH /candidate/{id}/status`` 一致：HR / 超管 + 可见性校验。
    """
    async with session.begin():
        candidate = await CandidateRepo(session).get_for_user(candidate_id, current_user)
        if not candidate:
            raise HTTPException(status_code=404, detail="候选人不存在或无权访问")

    try:
        await resume_manual_decision(candidate_id, payload.decision)
    except StatusFlowError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    audit(
        "candidate.manual_decision",
        user_id=current_user.id,
        target=f"candidate:{candidate_id}",
        detail=payload.decision,
    )
    return ResponseSchema()

@router.patch("/{candidate_id}/status", summary="更新候选人状态", response_model=ResponseSchema)
async def update_candidate_status(
    session:Annotated[AsyncSession, Depends(get_db)],
    candidate_id: str,
    status_data: CandidateStatusUpdateSchema,
    background_tasks: BackgroundTasks,
    current_user: UserModel = Depends(get_hr_or_superuser),
):
    async with session.begin():
        candidate_repo = CandidateRepo(session)
        interview_repo = InterviewRepo(session)
        # 用 get_for_user 而不是 get_by_id：可见性判定要和列表/下载接口一致，
        # 否则任何登录用户凭 candidate_id 就能改写他人候选人的状态、新建面试记录。
        # 「不存在」与「无权访问」返回同一状态，避免被用来枚举候选人 id。
        candidate = await candidate_repo.get_for_user(candidate_id, current_user)
        if not candidate:
            raise HTTPException(status_code=404, detail="候选人不存在或无权访问")

        # 记录流转前状态，供审计使用
        before_status = candidate.status

        # 校验状态流转（图状转移表）
        try:
            validate_transition(candidate.status, status_data.status)
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))

        if status_data.status == CandidateStatusEnum.WAITING_FOR_INTERVIEW:
            if not status_data.interview_time:
                raise HTTPException(status_code=400, detail="变更为待面试时必须填写面试时间")
            await interview_repo.create_interview(
                dict(
                    scheduled_time=status_data.interview_time,
                    candidate_id=candidate_id,
                    interviewer_id=current_user.id,
                )
            )
        elif status_data.status == CandidateStatusEnum.INTERVIEW_REJECTED:
            if not status_data.rejection_reason:
                raise HTTPException(status_code=400, detail="变更为面试未通过时必须填写未通过原因")
            # 如果面试失败了，一般来讲，应该是之前就已经创建过一个面试记录了
            interview: InterviewModel = await interview_repo.get_by_candidate_id(candidate_id)
            if interview is not None:
                await interview_repo.update_interview(
                    interview_id=interview.id,
                    interview_dict={
                        "feeback": status_data.rejection_reason,
                        "result": InterviewResultEnum.FAILED,
                    }
                )
            else:
                await interview_repo.create_interview(
                    dict(
                        scheduled_time=status_data.interview_time,
                        feedback=status_data.rejection_reason,
                        result=InterviewResultEnum.FAILED,
                        candidate_id=candidate_id,
                        interviewer_id=current_user.id,
                    )
                )

        # 更新候选人状态
        await candidate_repo.update_candidate_status(candidate_id=candidate_id, status=status_data.status)

    # 状态流转（尤其"人工通过/重试"会重启流程）属于高影响操作，记审计
    audit(
        "candidate.status_update",
        user_id=current_user.id,
        target=f"candidate:{candidate_id}",
        detail=f"{before_status} -> {status_data.status}",
    )

    # 人工通过/重试：异步触发工作流重启（跳转到 email_invite 节点）
    if status_data.status == CandidateStatusEnum.MANUAL_PASSED:
        background_tasks.add_task(trigger_manual_retry, candidate_id)

    return ResponseSchema()



@router.get("/ai-score/{candidate_id}", summary="获取候选人AI得分", response_model=CandidateAIScoreRespSchema)
async def get_candidate_ai_score(
    candidate_id: str,
    session:Annotated[AsyncSession, Depends(get_db)],
    current_user: UserModel = Depends(get_current_user),
):
    async with session.begin():
        # AI 评分含面试官对候选人的主观评价，属于敏感读取。
        # 这里必须和列表/下载/改状态/删除用同一套可见性判定，
        # 否则任何登录用户拿到 candidate_id 就能读走别人的评分。
        candidate = await CandidateRepo(session).get_for_user(candidate_id, current_user)
        if candidate is None:
            raise HTTPException(status_code=404, detail="候选人不存在或无权访问")

        score_repo = CandidateAIScoreRepo(session)
        ai_score = await score_repo.get_by_candidate_id(candidate_id)
        if not ai_score:
            raise HTTPException(status_code=400, detail="候选人的AI评分不存在！")
        return {"ai_score": ai_score}


@router.get("/candidate/distribution", summary="候选人分布统计（学历/状态/职位）")
async def get_candidate_distribution(
    session:Annotated[AsyncSession, Depends(get_db)],
    _: UserModel = Depends(get_current_user),
):
    """看板三个饼图的数据源"""
    async with session.begin():
        return await candidate_distribution_ser(session)


@router.get("/candidate/7d", summary="获取最近7天系统中新增候选人的数量")
async def get_7d_candidate(
    session:Annotated[AsyncSession, Depends(get_db)],
    _: UserModel = Depends(get_current_user),
):
    async with session.begin():
        candidate_repo = CandidateRepo(session)
        # 库里的 created_at 是 naive 北京时间，查询窗口用 db_now() 保持同一种表示形式
        now = db_now()
        # 起始：6 天前的 00:00
        start_time = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=6)
        # 结束：当前时间。
        # 注意：不要用「今天 00:00」当结束时间，否则 `created_at <= end_time` 会把
        # 当天 00:00 之后新增的候选人全部排除，「近 7 天」图表当天那一桶永远是 0。
        end_time = now
        rows = await candidate_repo.candidate_count(
            start_time=start_time,
            end_time=end_time,
        )
        day_counts = {day: count for day, count in rows}
        result = []
        for x in range(7):
            day_x = (start_time + timedelta(days=x)).date()
            result.append({
                "day": day_x.isoformat(),
                "count": day_counts.get(day_x, 0),
            })
        return result


@router.delete("/{candidate_id}", summary="删除候选人", response_model=ResponseSchema)
async def delete_candidate(
    session: Annotated[AsyncSession, Depends(get_db)],
    candidate_id: str,
    current_user: UserModel = Depends(get_hr_or_superuser),
):
    """删除候选人及其关联数据（AI 评分、面试记录）。

    权限：HR 或超级管理员，且候选人须在该用户的可见范围内（自己管理的部门/自己发布的职位）。
    """
    async with session.begin():
        candidate_repo = CandidateRepo(session)
        # delete_candidate 只按 id 删，不看归属：不先做可见性校验的话，
        # 任何 HR 都能删除其它部门的候选人。
        if await candidate_repo.get_for_user(candidate_id, current_user) is None:
            raise HTTPException(status_code=404, detail="候选人不存在或无权访问")
        deleted = await candidate_repo.delete_candidate(candidate_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="候选人不存在或无权访问")

    audit(
        "candidate.delete",
        user_id=current_user.id,
        target=f"candidate:{candidate_id}",
    )
    return ResponseSchema()
