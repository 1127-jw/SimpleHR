"""邮件回复超时兜底巡检。

背景
----
招聘状态图在 ``get_candidate_email_reply_node`` 通过 ``interrupt()`` 暂停，
等待候选人回复邮件。原先只有「协商轮次超限」会流转到协商超时，
**候选人始终不回信** 没有任何兜底：流程会永久停在 interrupt 上。

本模块提供一个周期性巡检任务：

1. 查询 ``email_invite_sent_at`` 早于 ``now - EMAIL_REPLY_TIMEOUT_HOURS``
   且状态仍处于 ``AI_FILTER_PASSED`` / ``MANUAL_PASSED`` 的候选人
   （这两个状态意味着流程正停在等待回复的中断点）；
2. 用 ``Command(resume=EMAIL_REPLY_TIMEOUT)`` 恢复状态图，
   由既有的 ``negotiation_expired_node`` 把状态推进到 ``NEGOTIATION_EXPIRED``。

安全性
------
* 恢复前先用 ``graph.aget_state()`` 确认该 thread **确实存在待响应的
  interrupt**，避免对已完成 / 未启动的 thread 误恢复。
* 用 Redis ``SET NX`` 做跨 worker 互斥，多进程部署下同一时刻只有一个
  巡检在执行。
"""
import logging
from datetime import timedelta

from langgraph.types import Command

from app.core.checkpointer import get_checkpointer
from app.core.config import get_settings
from app.core.db import AsyncSessionFactory
from app.core.redis_client import get_redis
from app.repository.resume_repo import CandidateRepo
from app.workflow.candidate_graph import build_candidate_graph
from app.workflow.candidate_state import EMAIL_REPLY_TIMEOUT
from utils.time_ISO8601_datatime import now_beijing, to_naive_beijing

settings = get_settings()
logger = logging.getLogger(__name__)

# 跨 worker 互斥锁：key + 锁有效期（略大于巡检间隔即可）
_SWEEP_LOCK_KEY = "sweep:email_reply_timeout"
_SWEEP_LOCK_TTL_SECONDS = 1800
# 单轮最多处理多少个候选人，避免一次扫全场
_SWEEP_BATCH_LIMIT = 200


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

    # 跨 worker 互斥：抢不到锁说明已有实例在跑
    if not await r.set(_SWEEP_LOCK_KEY, "1", nx=True, ex=_SWEEP_LOCK_TTL_SECONDS):
        logger.debug("邮件回复超时巡检已由其他 worker 持有，跳过本轮")
        return 0

    try:
        cutoff = to_naive_beijing(
            now_beijing() - timedelta(hours=settings.EMAIL_REPLY_TIMEOUT_HOURS)
        )

        async with AsyncSessionFactory() as session:
            async with session.begin():
                candidates = await CandidateRepo(session).list_stale_email_waiters(
                    cutoff, limit=_SWEEP_BATCH_LIMIT
                )

        if not candidates:
            return 0

        logger.info(
            "[timeout_sweep] 发现 %s 个等待回复超时的候选人", len(candidates)
        )

        candidate_graph = build_candidate_graph(get_checkpointer())
        swept = 0

        for candidate in candidates:
            config = {"configurable": {"thread_id": candidate.email}}
            try:
                # 仅当中断点仍在等待恢复时才 resume，避免误伤已完成 / 未启动的 thread
                snapshot = await candidate_graph.aget_state(config)
                if not snapshot.interrupts:
                    logger.info(
                        "[timeout_sweep] thread 无待处理 interrupt，跳过 | candidate_id=%s",
                        candidate.id,
                    )
                    continue

                await candidate_graph.ainvoke(
                    Command(resume=EMAIL_REPLY_TIMEOUT), config
                )
                swept += 1
                logger.info(
                    "[timeout_sweep] 已推进到协商超时 | candidate_id=%s | email=%s",
                    candidate.id, candidate.email,
                )
            except Exception:
                logger.exception(
                    "[timeout_sweep] 推进失败 | candidate_id=%s | email=%s",
                    candidate.id, candidate.email,
                )

        return swept
    finally:
        try:
            await r.delete(_SWEEP_LOCK_KEY)
        except Exception:  # noqa: BLE001 - 清理失败不影响主流程
            pass
