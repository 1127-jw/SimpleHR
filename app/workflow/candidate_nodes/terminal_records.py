"""链收尾：归档 / 结算的公共写库逻辑。

两者都只做一件事——**往状态变更链表里追加链尾记录**，区别只在：

* 归档（``next_id = '-1'``）：同时写 ``candidates.archived_at``；
* 结算（``next_id = '-2'``）：流程正常完成（已确认面试时间），转人工维护。

``candidates.status`` 在这两种情况下都**保持细分值**（见设计文档 §3.1）：
归档后仍是「AI筛选失败 / 拒绝面试 / 协商超时 / 处理异常」，
旧的按状态筛选与统计查询因此一行都不用改。
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import update

from app.core.config import get_settings
from app.core.db import AsyncSessionFactory
from app.core.time import db_now
from app.models.candidate import CandidateModel
from app.models.candidate_status_history import (
    NEXT_ID_SETTLED,
)
from app.repository.candidate_history_repo import CandidateStatusHistoryRepo

settings = get_settings()
logger = logging.getLogger(settings.NODE_LOGGER_NAME)


async def write_terminal_record(
    candidate,
    position,
    *,
    to_status: str,
    from_status: str | None = None,
    next_id: str,
    changed_at: datetime | None = None,
) -> None:
    """追加链尾记录。
    """
    async with AsyncSessionFactory() as session:
        async with session.begin():
            await CandidateStatusHistoryRepo(session).record_change(
                candidate.id,
                to_status=to_status,
                from_status=from_status or str(getattr(candidate, "status", "") or None),
                candidate_email=getattr(candidate, "email", None),
                position_id=getattr(position, "id", None),
                position_title=getattr(position, "title", None),
                changed_at=changed_at,
                next_id=next_id,
            )


async def mark_candidate_archived(candidate_id: str) -> None:
    """写 ``candidates.archived_at``"""
    async with AsyncSessionFactory() as session:
        async with session.begin():
            await session.execute(
                update(CandidateModel)
                .where(CandidateModel.id == candidate_id)
                .values(archived_at=db_now())
            )


def describe(next_id: str) -> str:
    return "结算" if next_id == NEXT_ID_SETTLED else "归档"


async def record_node_transition(
    candidate,
    position,
    to_status,
    *,
    from_status: str | None = None,
) -> None:
    """节点级状态变更：``candidates.status`` + 状态变更链，同一事务。

    终态 / 中间态节点统一走这里，避免「状态写了但链断」或「链写了状态没改」。

    **去重**：若链上最新记录已经是 ``to_status``，就不再插一条——
    否则同一状态被两个节点先后设置时（例如 ``screening`` 未通过 → ``rejected``）
    链里会出现两条一模一样的记录，时间链就没法看了。
    """
    from app.repository.resume_repo import CandidateRepo

    value = getattr(to_status, "value", to_status)
    async with AsyncSessionFactory() as session:
        async with session.begin():
            await CandidateRepo(session).update_candidate_status(
                candidate_id=candidate.id, status=to_status,
            )
            history = CandidateStatusHistoryRepo(session)
            active = await history.get_active(candidate.id)
            if active is not None and active.to_status == value:
                return
            await history.record_change(
                candidate.id,
                to_status=value,
                from_status=from_status or str(getattr(candidate, "status", "") or None),
                candidate_email=getattr(candidate, "email", None),
                position_id=getattr(position, "id", None),
                position_title=getattr(position, "title", None),
            )
