"""候选人状态变更历史（状态链）。

每次状态变更插一条记录，用 ``next_id`` 串成单向时间链：

* ``next_id IS NULL``——链上最新（流程仍在进行），每个候选人**有且只有一条**
  （由部分唯一索引 ``ux_csh_active`` 保证）；
* ``next_id = '-1'``——归档终止（淘汰/拒绝/异常收尾）；
* ``next_id = '-2'``——结算终止（``confirm`` 确认面试时间，转人工维护）。

设计说明见 ``docs/candidate_status_history_design.md``。
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import BaseModel

# next_id 的取值常量：用字符串而不是魔法数字散落各处
NEXT_ID_ARCHIVED = "-1"   # 归档（流程终止，不再自动跟进）
NEXT_ID_SETTLED = "-2"   # 结算（流程正常完成，转人工维护）


class CandidateStatusHistoryModel(BaseModel):
    """一条状态变更记录。"""

    __tablename__ = "candidate_status_history"

    candidate_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    candidate_email: Mapped[str] = mapped_column(String(255), nullable=False)
    position_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    position_title: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # 用 varchar 而不是 PG 枚举：这里记录的是「当时的 status 值」，
    # 不该因为枚举新增值而需要同步改历史表。
    from_status: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    to_status: Mapped[str] = mapped_column(String(50), nullable=False)

    changed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    # 最后接到该候选人邮件的时间；NULL 表示协商期间从没收到过
    last_email_received_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )

    next_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    __table_args__ = (
        # 每条链「有且只有一条」最新记录，防止并发写入把链写断
        Index(
            "ux_csh_active",
            "candidate_id",
            unique=True,
            postgresql_where=text("next_id IS NULL"),
        ),
    )
