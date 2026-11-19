"""周期性任务的通用生命周期管理器。

把「启动后台任务 / 定时循环 / 取消等待」这套模板从各业务巡检里抽出来，
子类只需实现 ``_run_once`` 并声明 ``_task_name``。

用法::

    class MySweepManager(PeriodicTaskManager):
        _task_name = "我的巡检"

        @classmethod
        async def _run_once(cls) -> None:
            ...

调度模式由 ``Settings.PERIODIC_TASK_MODE`` 统一给出（debug → 固定 15 秒，
生产 → 每日时刻），子类不感知模式选择。
"""
import asyncio
import logging
from contextlib import suppress
from typing import Optional

from app.core.config import get_settings
from app.tasks import scheduler
from app.tasks.retry import run_with_retry

settings = get_settings()
logger = logging.getLogger(__name__)


class PeriodicTaskManager:
    """所有周期性巡检任务的基类。

    子类需要：

    * 设置 ``_task_name``（用于日志与 asyncio 任务名）
    * 实现 ``_run_once()``（单轮业务逻辑）
    """

    _task: Optional[asyncio.Task] = None
    _task_name: str = "周期性任务"

    # 连续失败计数 / 告警状态（类属性，各子类独立）
    _consecutive_failures: int = 0
    _alert_sent: bool = False

    # ==================== 生命周期 ====================

    @classmethod
    async def init(cls) -> None:
        """启动后台巡检任务（全局只启动一次）。"""
        if cls._task is not None:
            logger.warning("%s已初始化，跳过重复 init", cls._log_name())
            return

        cls._task = asyncio.create_task(cls._loop(), name=cls._task_name)
        logger.info(
            "%s已启动：首次执行约在 %.0f 秒后，此后按 %s 模式调度",
            cls._log_name(),
            cls._startup_delay_seconds(),
            cls._resolve_mode(),
        )

    @classmethod
    async def close(cls) -> None:
        """取消并等待巡检任务结束。"""
        if cls._task is None:
            return
        cls._task.cancel()

        # 优雅关闭 / 退出逻辑：服务关闭时 cancel 后台常驻任务，
        # 然后 await 它收尾，不想因为取消抛异常打断关闭流程。
        with suppress(asyncio.CancelledError):
            await cls._task
        cls._task = None
        logger.info("%s已停止", cls._log_name())

    # ==================== 调度 ====================

    @classmethod
    def _log_name(cls) -> str:
        """日志里使用的任务标识：``类名:任务名``。
        """
        return f"{cls.__name__}:{cls._task_name}"

    @classmethod
    def _resolve_mode(cls) -> str:
        """当前生效的调度模式，由 ``Settings.PERIODIC_TASK_MODE`` 给出。

        模式选择已上移到配置层：debug → ``dev_interval``（15 秒一轮），
        生产 → ``daily_times``（按每日时刻）。子类不再各自解释模式。
        """
        return settings.PERIODIC_TASK_MODE

    @classmethod
    def _next_delay_seconds(cls) -> float:
        """计算下一次执行的等待秒数。

        模式只有 ``dev_interval`` / ``daily_times`` 两种，``interval_seconds``
        不再适用，因此不传。
        """
        return scheduler.delay_seconds(
            cls._resolve_mode(),
            dev_interval=settings.PERIODIC_TASK_DEV_INTERVAL_SECONDS,
            daily_times=settings.PERIODIC_TASK_DAILY_TIMES,
        )

    @classmethod
    def _max_interval_seconds(cls) -> float:
        """当前模式下相邻两次执行的最大间隔，用于心跳阈值推导。

        ``daily_times`` 模式下返回时刻表中的最大间隔（含跨天），
        避免用固定阈值在生产环境产生永久误报。
        """
        return scheduler.max_interval_seconds(
            cls._resolve_mode(),
            dev_interval=settings.PERIODIC_TASK_DEV_INTERVAL_SECONDS,
            daily_times=settings.PERIODIC_TASK_DAILY_TIMES,
        )

    @classmethod
    def _startup_delay_seconds(cls) -> float:
        """启动后到「首次执行」的宽限秒数。

        抽成钩子是为了让子类（以及测试）能覆盖它：默认 2 分钟既避免启动瞬间
        压一波数据库查询，也避免 ``daily_times`` 模式下首次任务要等上数小时。
        """
        return float(settings.BACKGROUND_TASK_STARTUP_DELAY_SECONDS)

    # ==================== 内部循环 ====================

    @classmethod
    async def _loop(cls) -> None:
        """定时循环：启动宽限后先跑一轮，再按周期反复执行 ``_run_once``。

        * 首次执行前只等 ``_startup_delay_seconds()``（默认 2 分钟）：
          既避免应用启动瞬间压一波数据库查询，也避免 ``daily_times`` 模式下
          首次任务要等上数小时（最长 8 小时）。
        * 每轮用 :func:`run_with_retry` 执行：**重试属于本次执行内部**，
          重试耗尽才记为一次「执行失败」，参与连续失败计数与告警。
        * 之后每轮结束用 ``_next_delay_seconds()`` 计算下一次等待，
          因此每日时刻模式下会自动对齐到下一个时刻。
        """
        await asyncio.sleep(cls._startup_delay_seconds())

        while True:
            last_exc = await run_with_retry(
                cls._run_once,
                retries=settings.BACKGROUND_TASK_RETRY_TIMES,
                base_delay=settings.BACKGROUND_TASK_RETRY_BASE_DELAY_SECONDS,
                max_delay=settings.BACKGROUND_TASK_RETRY_MAX_DELAY_SECONDS,
                on_retry=cls._on_retry,
            )
            if last_exc is None:
                cls._on_success()
            else:
                cls._on_failure(last_exc)

            await asyncio.sleep(cls._next_delay_seconds())

    # ==================== 执行结果处理 ====================

    @classmethod
    def _on_retry(cls, attempt: int, delay: float, exc: Exception) -> None:
        """单次重试前记录（只记一行，避免日志被刷）。"""
        logger.warning(
            "%s第 %s 次执行失败，%.1fs 后重试 | error=%s",
            cls._log_name(), attempt, delay, exc,
        )

    @classmethod
    def _on_success(cls) -> None:
        """本次执行成功（可能中途重试过）：清零计数并解除告警状态。"""
        if cls._consecutive_failures:
            logger.info(
                "%s已恢复（此前连续失败 %s 次）",
                cls._log_name(), cls._consecutive_failures,
            )
        cls._consecutive_failures = 0
        cls._alert_sent = False

    @classmethod
    def _on_failure(cls, exc: Exception) -> None:
        """重试耗尽：记为一次「执行失败」，达到阈值则告警（恢复前只报一次）。

        阈值 ``PERIODIC_TASK_FAILURE_ALERT_THRESHOLD`` 默认 3，
        即**容忍 2 次执行失败**，第 3 次才告警。
        """
        cls._consecutive_failures += 1
        count = cls._consecutive_failures
        threshold = settings.PERIODIC_TASK_FAILURE_ALERT_THRESHOLD

        # 简短告警避免刷屏；每 3 次保留完整堆栈便于排查
        logger.warning(
            "%s执行失败（重试耗尽，连续第 %s 次）| error=%s",
            cls._log_name(), count, exc,
        )
        if count % 3 == 0:
            logger.exception(
                "%s已连续失败 %s 次，保留完整堆栈", cls._log_name(), count
            )

        if count >= threshold and not cls._alert_sent:
            cls._alert_sent = True
            logger.error(
                "[bg_task_alert]%s连续 %s 次执行失败"
                "（每次已重试 %s 次，阈值 %s），需人工介入；恢复前不再重复告警",
                cls._log_name(), count,
                settings.BACKGROUND_TASK_RETRY_TIMES, threshold,
            )

    # ==================== 子类实现 ====================

    @classmethod
    async def _run_once(cls) -> None:
        """单轮业务逻辑，由子类实现。"""
        raise NotImplementedError
