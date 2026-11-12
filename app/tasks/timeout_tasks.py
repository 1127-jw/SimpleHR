"""邮件回复超时兜底巡检。
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
import asyncio
import logging
from contextlib import suppress
from datetime import timedelta
from typing import Optional

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


# ============================================================
# 生命周期管理：init / close
# ============================================================

class TimeoutSweepManager:
    """超时巡检任务的生命周期管理器（与 MailManager 风格一致）。

    用法::

        await TimeoutSweepManager.init()    # 启动后台巡检任务
        ...
        await TimeoutSweepManager.close()   # 取消并等待任务结束

    巡检只依赖 DB + Redis + checkpointer，不依赖邮件服务，
    因此不受 MAIL_BOT_ENABLED 控制。
    """

    _task: Optional[asyncio.Task] = None

    @classmethod
    async def init(cls) -> None:
        """启动后台巡检任务（全局只启动一次）。"""
        if cls._task is not None:
            logger.warning("超时巡检已初始化，跳过重复 init")
            return

        cls._task = asyncio.create_task(
            _sweep_loop(), name="email-reply-timeout-sweep"
        )
        logger.info(
            "邮件回复超时巡检已启动，间隔 %s 分钟",
            settings.EMAIL_REPLY_SWEEP_INTERVAL_MINUTES,
        )

    @classmethod
    async def close(cls) -> None:
        """取消并等待巡检任务结束。"""
        if cls._task is None:
            return

        cls._task.cancel()
        with suppress(asyncio.CancelledError):
            await cls._task
        cls._task = None
        logger.info("邮件回复超时巡检已停止")


async def _sweep_loop() -> None:
    """周期性执行超时巡检，直到任务被取消。

    先等待一个周期再开始，避免应用启动瞬间就压一波数据库查询；
    单 worker 下没有并发问题，多 worker 时由 sweep 内部的 Redis 锁互斥。
    """
    interval_seconds = settings.EMAIL_REPLY_SWEEP_INTERVAL_MINUTES * 60
    await asyncio.sleep(interval_seconds)

    while True:
        try:
            swept = await sweep_stale_email_replies()
            if swept:
                logger.info("邮件回复超时巡检：已推进 %s 个候选人", swept)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("邮件回复超时巡检失败，将在下个周期重试")
        await asyncio.sleep(interval_seconds)
