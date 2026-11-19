"""统一限流：调用级（LLM）与任务级（Agent / 图流程 / 解析）。

分工：任务级保护下游副作用（钉钉日程 / 邮件 / DB 写），调用级保护模型服务压力。
注意：信号量是**进程内**的，仅在 ``gunicorn workers=1`` 下有效；
多 worker 部署需改为 Redis 信号量（RPM 那层已是 Redis，天然跨进程）。
"""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager

from app.core.config import get_settings
from app.core.redis_client import get_redis

logger = logging.getLogger(__name__)

# key -> Semaphore（进程内单例，懒创建）
_semaphores: dict[str, asyncio.Semaphore] = {}

# 等待超过该秒数才记 warning，避免正常排队刷屏
_WAIT_LOG_THRESHOLD_SECONDS = 1.0
# RPM 超限后的软等待（秒）：平滑突发，而不是直接失败
_RPM_SOFT_WAIT_SECONDS = 1.0

# 所有 LLM 调用共享的池 key（调用点传入的 key 仅用于日志与 RPM 分组）
_LLM_SLOT_KEY = "__llm__"


def _get_semaphore(key: str, limit: int) -> asyncio.Semaphore:
    if key not in _semaphores:
        _semaphores[key] = asyncio.Semaphore(limit)
    return _semaphores[key]


@asynccontextmanager
async def _acquire(key: str, limit: int, log_tag: str):
    """公共部分：占用一个名额并记录等待时长。"""
    started = time.monotonic()
    sem = _get_semaphore(key, limit)
    await sem.acquire()
    waited = time.monotonic() - started
    if waited > _WAIT_LOG_THRESHOLD_SECONDS:
        logger.warning(
            "[limit] 等待 %.2fs 才拿到名额 | key=%s | limit=%s", waited, log_tag, limit,
        )
    try:
        yield
    finally:
        sem.release()


async def _hit_rpm(key: str) -> bool:
    """滑动窗口记一次调用。

    Returns:
        True 表示放行；False 表示本分钟已超限。
        Redis 不可用时返回 True（降级为不限速）。
    """
    limit = get_settings().LLM_RPM_LIMIT
    if limit <= 0:
        return True
    try:
        from app.core.rate_limit import hit

        return await hit(get_redis(), key, limit=limit, window=60)
    except Exception:  # noqa: BLE001 - 限流是增强项，Redis 抖动不能拖垮业务
        logger.debug("LLM RPM 限速不可用（Redis 未就绪），本轮跳过", exc_info=True)
        return True


@asynccontextmanager
async def llm_slot(key: str = "llm:default"):
    """调用级：占用一个 LLM 调用名额（全局共享池 + RPM 限速）。

    RPM 超限采用**软等待**（等 1 秒再继续）而非直接失败——LLM 调用对延迟不敏感，
    平滑突发优于快速失败。等待超时**不会**发生：名额获取是无超时的排队。

    Args:
        key: 仅用于日志与 RPM 分组（如 ``llm:intent``），不影响池的划分。
    """
    if not await _hit_rpm(key):
        logger.info("[limit] RPM 超限，软等待 %ss | key=%s", _RPM_SOFT_WAIT_SECONDS, key)
        await asyncio.sleep(_RPM_SOFT_WAIT_SECONDS)

    async with _acquire(_LLM_SLOT_KEY, get_settings().LLM_MAX_CONCURRENCY, key):
        yield


@asynccontextmanager
async def task_slot(key: str, limit: int | None = None):
    """任务级：占用一个任务名额（按 key 分池）。

    Args:
        key: 限流分组名。同一业务的所有入口应使用**同一个 key**，
            否则会退化成几个互不相干的池子，限不住总量。
        limit: 并发上限（从 Settings 读取后传入）。

    用法::

        async with task_slot("recruit", settings.RECRUIT_TASK_MAX_CONCURRENCY):
            await candidate_graph.ainvoke(...)
    """
    if limit is None:
        raise ValueError("task_slot 需要显式传入 limit")
    async with _acquire(key, limit, key):
        yield
