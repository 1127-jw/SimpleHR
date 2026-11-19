"""意图识别「人工复核」待办的接口。

LLM 判不准意图的邮件会落库，HR 在这里人工确认意图并执行对应动作。
权限与「邮件投递确认」一致：HR / 超管。
"""
from __future__ import annotations

from typing import Annotated, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import audit
from app.core.auth import get_hr_or_superuser
from app.core.db import get_db
from app.models.user import UserModel
from app.schemas.response_schema import ResponseSchema
from app.service.mail_review_ser import list_reviews, resolve_review

router = APIRouter(prefix="/mail-reviews", tags=["邮件意图复核"])


class MailReviewItem(BaseModel):
    id: str
    from_address: str
    from_name: str
    subject: str
    intent_guess: str
    sub_intent: str
    confidence: float
    reason: str
    status: str
    resolver_id: Optional[str] = None
    resolved_at: Optional[str] = None
    resolution: str = ""
    created_at: str


class MailReviewListResponse(BaseModel):
    items: list[MailReviewItem]
    total: int


class MailReviewDetail(MailReviewItem):
    body_excerpt: str
    evidence: str
    attachment_paths: list[str] = Field(default_factory=list)


class ResolveSchema(BaseModel):
    """人工处理动作。"""

    action: Literal["execute", "ignore"] = Field(
        ..., description="execute=按 intent 执行动作；ignore=标记忽略"
    )
    intent: Optional[str] = Field(
        None,
        description="人工选定的意图（action=execute 时必填），取值见 MailIntent",
    )


def _serialize(row) -> dict:
    return {
        "id": row.id,
        "from_address": row.from_address,
        "from_name": row.from_name,
        "subject": row.subject,
        "intent_guess": row.intent_guess,
        "sub_intent": row.sub_intent,
        "confidence": row.confidence,
        "reason": row.reason,
        "status": row.status,
        "resolver_id": row.resolver_id,
        "resolved_at": row.resolved_at.isoformat() if row.resolved_at else None,
        "resolution": row.resolution,
        "created_at": row.created_at.isoformat() if row.created_at else "",
    }


@router.get("", summary="意图复核列表", response_model=MailReviewListResponse)
async def list_mail_reviews(
    session: Annotated[AsyncSession, Depends(get_db)],
    status: Optional[str] = Query(
        None, description="PENDING / RESOLVED / IGNORED；不传为全部"
    ),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    _: UserModel = Depends(get_hr_or_superuser),
):
    rows, total = await list_reviews(session, status=status, page=page, size=size)
    return {"items": [_serialize(r) for r in rows], "total": total}


@router.get("/{review_id}", summary="意图复核详情", response_model=MailReviewDetail)
async def get_mail_review(
    review_id: str,
    session: Annotated[AsyncSession, Depends(get_db)],
    _: UserModel = Depends(get_hr_or_superuser),
):
    from app.repository.mail_review_repo import MailReviewRepo

    async with session.begin():
        row = await MailReviewRepo(session).get_by_id(review_id)
    if row is None:
        raise HTTPException(status_code=404, detail="复核记录不存在")
    return {
        **_serialize(row),
        "body_excerpt": row.body_excerpt,
        "evidence": row.evidence,
        "attachment_paths": row.attachment_paths or [],
    }


@router.post(
    "/{review_id}/resolve",
    summary="处理复核（确认意图并执行 / 忽略）",
    response_model=ResponseSchema,
)
async def resolve(
    review_id: str,
    payload: ResolveSchema,
    session: Annotated[AsyncSession, Depends(get_db)],
    current_user: UserModel = Depends(get_hr_or_superuser),
):
    try:
        await resolve_review(
            session,
            review_id,
            resolver_id=current_user.id,
            intent=payload.intent,
            action=payload.action,
        )
    except ValueError as e:
        msg = str(e)
        if "不存在" in msg:
            raise HTTPException(status_code=404, detail=msg)
        raise HTTPException(status_code=400, detail=msg) from e

    audit(
        "mail_review.resolve",
        user_id=current_user.id,
        target=f"mail_review:{review_id}",
        detail=f"{payload.action}:{payload.intent or '-'}",
    )
    return ResponseSchema()
