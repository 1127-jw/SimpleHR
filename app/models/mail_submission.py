from datetime import datetime
from enum import StrEnum
from typing import Optional

from sqlalchemy import String, Text, JSON, DateTime, Index
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import BaseModel


class MailSubmissionStatusEnum(StrEnum):
    PENDING = "PENDING"      # 待 HR 确认
    CONFIRMED = "CONFIRMED"  # 已确认，建人并起图
    REJECTED = "REJECTED"    # 已拒绝
    EXPIRED = "EXPIRED"      # 超期未处理


class MailResumeSubmissionModel(BaseModel):
    """邮件投递（RESUME_SUBMISSION）的待确认记录。

    邮件命中投递意图后先落这张表（PENDING），由 HR 在确认台选择职位后
    再走既有的建人 + 起图流程，避免自动建错职位 / 建错人。
    """

    __tablename__ = "mail_resume_submissions"
    __table_args__ = (
        Index("ix_mail_submission_status_created", "status", "created_at"),
    )

    email_uid: Mapped[str] = mapped_column(String(64), default="")
    message_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    from_address: Mapped[str] = mapped_column(String(255), index=True, default="")
    from_name: Mapped[str] = mapped_column(String(255), default="")
    subject: Mapped[str] = mapped_column(String(500), default="")
    body_excerpt: Mapped[str] = mapped_column(Text, default="")
    attachment_paths: Mapped[list] = mapped_column(JSON, default=list)
    parsed_info: Mapped[dict] = mapped_column(JSON, default=dict)
    suggested_position_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    position_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    resume_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default=MailSubmissionStatusEnum.PENDING)
    reviewer_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    review_note: Mapped[str] = mapped_column(Text, default="")
