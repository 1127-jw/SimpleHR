"""邮件机器人轮询计时器。

复用 ``PeriodicTaskManager`` 的生命周期 / 重试 / 连续失败告警，自己只决定
三件事：回调从哪来、模式怎么定、间隔多少。

* **回调**：``start(poll_once)`` 注入，存成类属性 ``_poll_once``；
* **模式**：恒为 ``interval``（``Settings.MAIL_POLL_MODE``），不看 ``DEBUG``、
  不跟随全局 ``PERIODIC_TASK_MODE``；
* **间隔**：``MAIL_POLL_INTERVAL_SECONDS``（默认 15 秒）。

为什么并入基类：此前 ``MailPollTimer`` 把 ``run_with_retry`` 的返回值丢掉了，
**失败从不计数、从不告警**——只刷重试日志，``[bg_task_alert]`` 永远不会为
邮件轮询触发。并入后走与周期巡检同一条 ``_on_failure`` 通路。

本模块**只负责「什么时候调用」**，具体动作委托给 ``MailManager._poll_once()``。
"""
from __future__ import annotations

from typing import Awaitable, Callable, Optional

from app.core.config import get_settings
from app.tasks import scheduler
from app.tasks.periodic_task import PeriodicTaskManager

PollOnce = Callable[[], Awaitable[None]]


class MailPollTimer(PeriodicTaskManager):
    """邮件轮询的计时循环（全局单例）。"""

    _task_name = "邮件轮询"

    # 回调由 start() 注入；全局只有一个轮询循环，用类属性保存即可
    _poll_once: Optional[PollOnce] = None

    # ==================== 生命周期 ====================

    @classmethod
    async def start(cls, poll_once: PollOnce) -> None:
        """注入回调并启动（基类 ``init`` 是 async）。"""
        cls._poll_once = poll_once
        await cls.init()

    # ==================== 子类实现 ====================

    @classmethod
    async def _run_once(cls) -> None:
        """跑一轮轮询。
        """
        if cls._poll_once is None:
            raise RuntimeError("MailPollTimer.start() 尚未注入 poll_once")
        await cls._poll_once()

    # ==================== 调度 ====================

    @classmethod
    def _resolve_mode(cls) -> str:
        """恒为固定间隔：独立开关，不看 ``DEBUG`` / 全局模式。"""
        return get_settings().MAIL_POLL_MODE

    @classmethod
    def _next_delay_seconds(cls) -> float:
        return scheduler.delay_seconds(
            cls._resolve_mode(),
            dev_interval=get_settings().PERIODIC_TASK_DEV_INTERVAL_SECONDS,
            interval_seconds=get_settings().MAIL_POLL_INTERVAL_SECONDS,
            daily_times=get_settings().PERIODIC_TASK_DAILY_TIMES,
        )

    @classmethod
    def _max_interval_seconds(cls) -> float:
        """相邻两次轮询的最大间隔，用于心跳阈值推导。"""
        return scheduler.max_interval_seconds(
            cls._resolve_mode(),
            dev_interval=get_settings().PERIODIC_TASK_DEV_INTERVAL_SECONDS,
            interval_seconds=get_settings().MAIL_POLL_INTERVAL_SECONDS,
            daily_times=get_settings().PERIODIC_TASK_DAILY_TIMES,
        )
