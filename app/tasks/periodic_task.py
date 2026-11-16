"""周期性任务的通用生命周期管理器。

把「启动后台任务 / 定时循环 / 取消等待」这套模板从各业务巡检里抽出来，
子类只需实现 ``_run_once`` 并声明 ``_task_name`` / ``_interval_setting``。

用法::

    class MySweepManager(PeriodicTaskManager):
        _task_name = "我的巡检"
        _interval_setting = "MY_SWEEP_INTERVAL_MINUTES"

        @classmethod
        async def _run_once(cls) -> None:
            ...
"""
import asyncio
import logging
from contextlib import suppress
from typing import Optional

from app.core.config import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)


class PeriodicTaskManager:
    """所有周期性巡检任务的基类。

    子类需要：

    * 设置 ``_task_name``（用于日志与 asyncio 任务名）
    * 设置 ``_interval_setting``（Settings 中的间隔分钟数配置名）
    * 实现 ``_run_once()``（单轮业务逻辑）
    """

    _task: Optional[asyncio.Task] = None
    _task_name: str = "周期性任务"
    _interval_setting: str = ""

    # ==================== 生命周期 ====================

    @classmethod
    async def init(cls) -> None:
        """启动后台巡检任务（全局只启动一次）。"""
        if cls._task is not None:
            logger.warning("%s已初始化，跳过重复 init", cls._task_name)
            return

        cls._task = asyncio.create_task(cls._loop(), name=cls._task_name)
        logger.info(
            "%s已启动，间隔 %s 分钟", cls._task_name, cls._get_interval_minutes()
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
        logger.info("%s已停止", cls._task_name)

    # ==================== 内部循环 ====================

    @classmethod
    def _get_interval_minutes(cls) -> int:
        """从 Settings 读取间隔（分钟）。"""
        if not cls._interval_setting:
            raise NotImplementedError(
                f"{cls.__name__} 未设置 _interval_setting"
            )
        return int(getattr(settings, cls._interval_setting))

    @classmethod
    async def _loop(cls) -> None:
        """定时循环：先等一个周期，再反复执行 ``_run_once``。

        先等待是为了避免应用启动瞬间就压一波数据库查询；
        单 worker 无并发问题，多 worker 由业务侧自行加锁互斥。
        """
        interval_seconds = cls._get_interval_minutes() * 60
        await asyncio.sleep(interval_seconds)

        while True:
            try:
                await cls._run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("%s失败，将在下个周期重试", cls._task_name)
            await asyncio.sleep(interval_seconds)

    # ==================== 子类实现 ====================

    @classmethod
    async def _run_once(cls) -> None:
        """单轮业务逻辑，由子类实现。"""
        raise NotImplementedError
