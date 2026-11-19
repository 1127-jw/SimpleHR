"""邮件回复超时兜底巡检。

业务逻辑：查询 ``email_invite_sent_at`` 早于 ``now - EMAIL_REPLY_TIMEOUT_HOURS``
且状态仍处于 ``AI_FILTER_PASSED`` / ``MANUAL_PASSED`` 的候选人（这两个状态意味着
流程正停在等待回复的中断点），用 ``Command(resume=EMAIL_REPLY_TIMEOUT)`` 恢复
状态图，由既有的 ``negotiation_expired_node`` 把状态推进到 ``NEGOTIATION_EXPIRED``。

生命周期管理（init / close / 定时循环）已抽到 ``PeriodicTaskManager`` 基类。
"""
import asyncio
import logging
import uuid
from datetime import timedelta

from langgraph.types import Command

from app.core.checkpointer import get_checkpointer
from app.core.config import get_settings
from app.core.db import AsyncSessionFactory
from app.core.redis_client import get_redis
from app.repository.candidate_history_repo import CandidateStatusHistoryRepo
from app.repository.resume_repo import CandidateRepo
from app.service.candidate_status_ser import mark_candidate_error
from app.tasks.periodic_task import PeriodicTaskManager
from app.workflow.candidate_graph import build_candidate_graph
from app.workflow.candidate_state import (
    EMAIL_REPLY_INTERRUPT,
    EMAIL_REPLY_TIMEOUT,
    is_waiting_on,
    thread_id_of,
)
from app.core.time import db_now

settings = get_settings()
logger = logging.getLogger(__name__)

# 跨 worker 互斥锁：key + 锁有效期（略大于巡检间隔即可）
_SWEEP_LOCK_KEY = "sweep:email_reply_timeout"
_SWEEP_LOCK_TTL_SECONDS = 1800
# 单轮最多处理多少个候选人，避免一次扫全场
_SWEEP_BATCH_LIMIT = 200

# 释放锁必须校验持有者：直接 DEL 会在「本轮超时（>TTL）而别的 worker 已重新加锁」
# 时删掉别人的锁，两个巡检于是并发跑同一批候选人，各自 resume 同一个 thread。
_SWEEP_LOCK_RELEASE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


async def sweep_stale_email_replies() -> int:
    """巡检并推进超时的「等待候选人回复」流程。

    Returns:
        本轮成功推进的候选人数量。
    """
    if not settings.GRAPH_OPEN:
        # 非图模式没有状态图中断点，无需巡检
        return 0

    try:
        r = get_redis()
    except RuntimeError:
        logger.warning("Redis 未初始化，跳过本轮邮件回复超时巡检")
        return 0

    # 跨 worker 互斥：抢不到锁说明已有实例在跑。
    # 锁值用一次性 token，释放时校验，避免误删别的实例重新获取的锁。
    lock_token = uuid.uuid4().hex
    if not await r.set(
        _SWEEP_LOCK_KEY, lock_token, nx=True, ex=_SWEEP_LOCK_TTL_SECONDS
    ):
        logger.debug("邮件回复超时巡检已由其他 worker 持有，跳过本轮")
        return 0

    try:
        cutoff = db_now() - timedelta(hours=settings.EMAIL_REPLY_TIMEOUT_HOURS)

        async with AsyncSessionFactory() as session:
            async with session.begin():
                # 取数改为「状态变更链」：只扫协商中 + 链上最新 + 起算时间超时的。
                # 比原来的 email_invite_sent_at 判据精确——那是流程级字段，
                # 同邮箱的多条记录会互相干扰（实测过误伤）。
                rows = await CandidateStatusHistoryRepo(session).list_stale_negotiating(
                    cutoff, limit=_SWEEP_BATCH_LIMIT
                )

        if not rows:
            return 0

        logger.info("[timeout_sweep] 发现 %s 个等待回复超时的候选人", len(rows))

        candidate_graph = build_candidate_graph(get_checkpointer())
        swept = 0

        for row in rows:
            # thread_id 按 candidate_id 隔离，不用邮箱——否则会唤醒同邮箱
            # 另一条记录的中断点（实测事故根因）。
            config = {"configurable": {"thread_id": thread_id_of(row.candidate_id)}}
            try:
                # 仅当挂起的是「等待候选人回复」这个中断点时才 resume。
                # 只看「有没有中断点」是不够的：图里还有其他 interrupt 节点
                # （人工闸门），用 TIMEOUT 去 resume 它们会把人直接判成淘汰。
                snapshot = await candidate_graph.aget_state(config)
                if not is_waiting_on(snapshot.interrupts, EMAIL_REPLY_INTERRUPT):
                    logger.info(
                        "[timeout_sweep] 中断点不是等待候选人回复，跳过 | candidate_id=%s",
                        row.candidate_id,
                    )
                    continue

                from app.core.limiters import task_slot

                # 超时恢复也占用 "recruit" 名额，避免巡检批量恢复时挤占并发
                async with task_slot("recruit", settings.RECRUIT_TASK_MAX_CONCURRENCY):
                    # 恢复侧打 thread_id：与挂起处的日志配对，定位「谁被唤醒了」
                    logger.info(
                        "[timeout_sweep] 恢复超时的招聘图 | candidate_id=%s | thread_id=%s",
                        row.candidate_id, thread_id_of(row.candidate_id),
                    )
                    await candidate_graph.ainvoke(
                        Command(resume=EMAIL_REPLY_TIMEOUT), config
                    )
                swept += 1
                logger.info(
                    "[timeout_sweep] 已推进到协商超时 | candidate_id=%s | email=%s",
                    row.candidate_id, row.candidate_email,
                )
            except Exception as e:
                logger.exception(
                    "[timeout_sweep] 推进失败 | candidate_id=%s | email=%s",
                    row.candidate_id, row.candidate_email,
                )
                # 后台链路：异常不会经过全局处理器，就地标记为可重试
                await mark_candidate_error(
                    row.candidate_id, f"超时推进失败: {e}"
                )

        return swept
    finally:
        try:
            await r.eval(_SWEEP_LOCK_RELEASE_LUA, 1, _SWEEP_LOCK_KEY, lock_token)
        except Exception:  # noqa: BLE001 - 清理失败不影响主流程（锁会自然过期）
            logger.warning("释放巡检锁失败，等待 TTL 自动过期", exc_info=True)


class TimeoutSweepManager(PeriodicTaskManager):
    """邮件回复超时巡检的生命周期管理器。"""

    _task_name = "邮件回复超时巡检"

    @classmethod
    async def _run_once(cls) -> None:
        swept = await sweep_stale_email_replies()
        if swept:
            logger.info("邮件回复超时巡检：已推进 %s 个候选人", swept)
