"""邮件投递待确认接口。

列表 / 详情 / 确认（建人并起图）/ 拒绝。
设计见 docs/intent_recognition_plan.md 第九节。
"""
from typing import Annotated, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.auth import get_hr_or_superuser
from app.core.db import get_db
from app.models.mail_submission import MailSubmissionStatusEnum
from app.models.user import UserModel
from app.repository.mail_submission_repo import MailSubmissionRepo
from app.schemas.response_schema import ResponseSchema
from app.service.mail_submission_ser import (
    confirm_mail_submission,
    reject_mail_submission,
)
from app.tasks.agent_tasks import run_candidate_agent

router = APIRouter(prefix="/mail-submissions", tags=["mail-submissions"])


class ConfirmSchema(BaseModel):
    position_id: str = Field(..., description="候选人应聘的职位 ID")


class RejectSchema(BaseModel):
    note: str = Field(default="", description="拒绝原因")


@router.get("", summary="邮件投递列表")
async def list_submissions(
    session: Annotated[AsyncSession, Depends(get_db)],
    status: MailSubmissionStatusEnum = Query(
        MailSubmissionStatusEnum.PENDING,
        description="投递状态；非法值返回 422 而不是静默返回空列表",
    ),
    page: int = Query(1, ge=1, description="页码，从 1 开始"),
    size: int = Query(20, ge=1, le=100, description="每页数量，1-100"),
    _: UserModel = Depends(get_hr_or_superuser),
):
    async with session.begin():
        repo = MailSubmissionRepo(session)
        rows, total = await repo.list_by_status_page(status, page=page, size=size)
    return {
        "items": [
            {
                "id": r.id,
                "from_address": r.from_address,
                "from_name": r.from_name,
                "subject": r.subject,
                "status": r.status,
                "attachment_paths": r.attachment_paths,
                "parsed_info": r.parsed_info,
                "suggested_position_id": r.suggested_position_id,
                "created_at": r.created_at,
            }
            for r in rows
        ],
        "total": total,
    }


@router.get("/{submission_id}", summary="邮件投递详情")
async def get_submission(
    submission_id: str,
    session: Annotated[AsyncSession, Depends(get_db)],
    current_user: UserModel = Depends(get_hr_or_superuser),
):
    sub = await MailSubmissionRepo(session).get_by_id(submission_id)
    if sub is None:
        raise HTTPException(status_code=404, detail="记录不存在")
    # 详情含候选人 PII（姓名/邮箱/电话/简历附件），属于敏感读取
    audit(
        "mail_submission.view",
        user_id=current_user.id,
        target=f"submission:{submission_id}",
        detail=sub.from_address,
    )
    return {
        "id": sub.id,
        "from_address": sub.from_address,
        "from_name": sub.from_name,
        "subject": sub.subject,
        "body_excerpt": sub.body_excerpt,
        "attachment_paths": sub.attachment_paths,
        "parsed_info": sub.parsed_info,
        "suggested_position_id": sub.suggested_position_id,
        "status": sub.status,
        "review_note": sub.review_note,
        "created_at": sub.created_at,
    }


@router.post("/{submission_id}/confirm", summary="确认投递（建人并起图）", response_model=ResponseSchema)
async def confirm(
    submission_id: str,
    payload: ConfirmSchema,
    background_tasks: BackgroundTasks,
    session: Annotated[AsyncSession, Depends(get_db)],
    current_user: UserModel = Depends(get_hr_or_superuser),
):
    try:
        candidate, position, interviewer = await confirm_mail_submission(
            session, submission_id, payload.position_id, current_user
        )
    except ValueError as e:
        msg = str(e)
        if "不存在" in msg:
            raise HTTPException(status_code=404, detail=msg)
        if "不可确认" in msg:
            raise HTTPException(status_code=409, detail=msg)
        raise HTTPException(status_code=400, detail=msg)

    background_tasks.add_task(
        run_candidate_agent,
        candidate=candidate, position=position, interviewer=interviewer,
    )
    audit(
        "mail_submission.confirm",
        user_id=current_user.id,
        target=f"submission:{submission_id}",
        detail=payload.position_id,
    )
    return ResponseSchema()


@router.post("/{submission_id}/reject", summary="拒绝投递", response_model=ResponseSchema)
async def reject(
    submission_id: str,
    session: Annotated[AsyncSession, Depends(get_db)],
    payload: Optional[RejectSchema] = None,
    current_user: UserModel = Depends(get_hr_or_superuser),
):
    await reject_mail_submission(session, submission_id, current_user, payload.note if payload else "")
    audit(
        "mail_submission.reject",
        user_id=current_user.id,
        target=f"submission:{submission_id}",
        detail=payload.note if payload else "",
    )
    return ResponseSchema()
