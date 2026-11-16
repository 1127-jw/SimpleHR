"""待确认投递的超期巡检。

周期性地把超过 ``MAIL_SUBMISSION_EXPIRE_DAYS`` 仍为 PENDING 的邮件投递
置为 EXPIRED，避免确认台无限积压。

生命周期管理（init / close / 定时循环）已抽到 ``PeriodicTaskManager`` 基类。
"""
import logging

from app.core.config import get_settings
from app.service.mail_submission_ser import expire_stale_submissions
from app.tasks.periodic_task import PeriodicTaskManager

settings = get_settings()
logger = logging.getLogger(__name__)


class MailSubmissionSweepManager(PeriodicTaskManager):
    """待确认投递超期巡检的生命周期管理器。"""

    _task_name = "待确认投递巡检"
    _interval_setting = "MAIL_SUBMISSION_SWEEP_INTERVAL_MINUTES"

    @classmethod
    async def _run_once(cls) -> None:
        expired = await expire_stale_submissions()
        if expired:
            logger.info("待确认投递巡检：置为 EXPIRED %s 条", expired)
