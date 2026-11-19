"""后台任务的失败重试。

语义：**重试属于「一次执行」内部**。只有重试全部耗尽仍失败，
才记为一次「执行失败」，进而参与连续失败计数与告警判定。

    ┌─ 一次调度执行 ──────────────────────────────┐
    │ 首次尝试 ─► 失败 ─► 重试1 ─► 重试2 ─► 重试3   │
    │      └─ 任一次成功 ─► 本次执行成功            │
    └────────────────────────────────────────────┘

注意：``asyncio.CancelledError`` **不重试**，直接向上抛（应用关闭时不该拖延）。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

# 重试间隔：base * 2^(n-1)，再夹到 max
DEFAULT_BASE_DELAY_SECONDS = 1.0
DEFAULT_MAX_DELAY_SECONDS = 30.0


async def run_with_retry(
    fn: Callable[[], Awaitable[None]],
    *,
    retries: int = 3,
    base_delay: float = DEFAULT_BASE_DELAY_SECONDS,
    max_delay: float = DEFAULT_MAX_DELAY_SECONDS,
    on_retry: Optional[Callable[[int, float, BaseException], None]] = None,
) -> Optional[BaseException]:
    """执行 ``fn``，失败后按指数退避重试。

    Args:
        fn: 无参异步函数（通常绑定到具体任务）。
        retries: **重试次数**（不含首次）。``retries=3`` 即最多尝试 4 次。
        base_delay: 首次重试前等待秒数，之后按 2 倍递增。
        max_delay: 单次等待上限。
        on_retry: 每次重试前的回调，参数为 (第几次重试, 等待秒数, 本次异常)。

    Returns:
        ``None`` 表示最终成功；否则为**重试耗尽时的最后一个异常**。
    Raises:
        asyncio.CancelledError: 取消时不重试，直接向上抛。
    """
    last_exc: BaseException | None = None
    for attempt in range(retries + 1):
        try:
            await fn()
            return None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last_exc = exc
            if attempt >= retries:
                return last_exc
            delay = min(max_delay, base_delay * (2 ** attempt))
            if on_retry is not None:
                on_retry(attempt + 1, delay, exc)
            await asyncio.sleep(delay)
    return last_exc   # 兜底，理论上走不到
