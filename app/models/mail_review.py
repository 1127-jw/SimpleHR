"""意图识别「人工复核」待办。

LLM 判定意图时若 ``needs_review`` 或判成 ``AMBIGUOUS``，这封邮件就落进这张表，
由 HR 在「候选人管理 → 意图复核」里人工确认意图并**执行对应动作**
（恢复招聘图 / 建投递记录 / 标记忽略）。

与 ``mail_resume_submissions`` 的区别：那张表是「投递简历」这一**确定意图**的待确认，
本表是**意图本身不确定**的待办。
"""
from datetime import datetime
from enum import StrEnum
from typing import Optional

from sqlalchemy import JSON, Float, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import BaseModel


class MailReviewStatusEnum(StrEnum):
    PENDING = "PENDING"    # 待人工复核
    RESOLVED = "RESOLVED"  # 已确认意图并执行
    IGNORED = "IGNORED"    # 已忽略（无关邮件等）


class MailReviewModel(BaseModel):
    """一封待复核邮件。"""

    __tablename__ = "mail_reviews"
    __table_args__ = (
        Index("ix_mail_review_status_created", "status", "created_at"),
    )

    email_uid: Mapped[str] = mapped_column(String(64), default="")
    # message_id 全局唯一，作为幂等键：同一封邮件重复命中不会插两条
    message_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    from_address: Mapped[str] = mapped_column(String(255), index=True, default="")
    from_name: Mapped[str] = mapped_column(String(255), default="")
    subject: Mapped[str] = mapped_column(String(500), default="")
    # 正文留全量：复核时要人工读上下文才能判意图，截断会让人无从下手
    body_excerpt: Mapped[str] = mapped_column(Text, default="")
    attachment_paths: Mapped[list] = mapped_column(JSON, default=list)

    # LLM 的猜测与判据，供人工参考
    intent_guess: Mapped[str] = mapped_column(String(32), default="")
    sub_intent: Mapped[str] = mapped_column(String(32), default="")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    reason: Mapped[str] = mapped_column(Text, default="")
    evidence: Mapped[str] = mapped_column(Text, default="")

    status: Mapped[str] = mapped_column(
        String(16), default=MailReviewStatusEnum.PENDING, index=True
    )
    resolver_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    # 认领（占位）时间：**只用于「执行中」的租约判断**，处理动作失败会清空。
    # 与 resolved_at（处理完成时间）语义不同，不能合并：
    #   * claimed_at —— 谁在什么时候开始处理，用于防止两个 HR 同时执行；
    #   * resolved_at —— 什么时候处理完成，面向展示与统计。
    claimed_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    # 人工最终选定的意图（execute 时）或忽略原因
    resolution: Mapped[str] = mapped_column(Text, default="")
