"""调度策略、失败重试与邮件轮询计时器测试。

重点覆盖：
* 「阈值必须跟调度配置联动」——生产 daily_times 模式下间隔可达数小时，
  写死阈值会导致存活指标永久误报；
* 重试属于「一次执行」内部，重试耗尽才记为一次执行失败；
* 容忍 2 次执行失败，第 3 次才告警，且恢复前只报一次。
"""
from __future__ import annotations

import asyncio
import logging

import pytest

from app.tasks import scheduler
from app.tasks.mail_poll_loop import MailPollTimer
from app.tasks.retry import run_with_retry
from app.core.config import Settings


# ==================== 模式推导（配置层） ====================

def _settings(**overrides) -> Settings:
    """构造 Settings；密钥显式传，避免受 .env / 环境变量影响。"""
    kwargs = {
        "ACCESS_SECRET": "x" * 32,
        "REFRESH_SECRET": "y" * 32,
    }
    kwargs.update(overrides)
    return Settings(**kwargs)


@pytest.mark.parametrize(
    ("debug", "expected"),
    [
        (True, scheduler.MODE_DEV),
        (False, scheduler.MODE_DAILY),
    ],
)
def test_periodic_task_mode_follows_debug(debug, expected):
    """周期巡检模式由 DEBUG 推导：debug 固定 15 秒，生产按每日时刻。

    模式选择已从「三选一的手动开关」简化为 computed field，
    这里是它的契约：不再有 ``auto`` / ``interval`` 档。
    """
    assert _settings(debug=debug).PERIODIC_TASK_MODE == expected


@pytest.mark.parametrize("debug", [True, False])
def test_mail_poll_mode_ignores_debug(debug):
    """邮件轮询是独立开关：不看 DEBUG，恒为固定间隔。"""
    assert _settings(debug=debug).MAIL_POLL_MODE == scheduler.MODE_INTERVAL


def test_default_mail_poll_config():
    """默认值断言（看字段默认，避免受环境变量影响）。"""
    fields = Settings.model_fields
    assert fields["MAIL_POLL_INTERVAL_SECONDS"].default == 15
    assert fields["PERIODIC_TASK_DEV_INTERVAL_SECONDS"].default == 15
    assert fields["BACKGROUND_TASK_STARTUP_DELAY_SECONDS"].default == 120
    # 已删除的模式开关不该再出现，避免有人从 .env 里设了却毫无效果
    for removed in (
        "PERIODIC_TASK_SCHEDULING",
        "MAIL_POLL_SCHEDULING",
        "MAIL_POLL_INTERVAL_MINUTES",
        "EMAIL_REPLY_SWEEP_INTERVAL_MINUTES",
        "MAIL_SUBMISSION_SWEEP_INTERVAL_MINUTES",
    ):
        assert removed not in fields


# ==================== 每日时刻解析 ====================

def test_seconds_until_next_daily_time_2400_is_next_day():
    # 24:00 视为次日 00:00
    spec = "24:00"
    delay = scheduler.seconds_until_next_daily_time(spec)
    assert 0 < delay <= 24 * 3600


def test_daily_max_gap_wraps_around_midnight():
    # 08:00 → 12:00 → 17:00 → 24:00 → 次日 08:00
    # 最大间隔是 24:00 → 08:00 = 8 小时
    assert scheduler.daily_max_gap_seconds("08:00,12:00,17:00,24:00") == 8 * 3600


def test_daily_max_gap_single_time():
    assert scheduler.daily_max_gap_seconds("08:00") == 24 * 3600


def test_daily_max_gap_bad_spec():
    with pytest.raises(ValueError):
        scheduler.parse_daily_minutes("")


# ==================== 阈值推导 ====================

def test_max_interval_follows_mode():
    """daily_times 的最大间隔必须按时刻表算，而不是固定值。"""
    daily = scheduler.max_interval_seconds(
        scheduler.MODE_DAILY,
        dev_interval=15,
        interval_seconds=30 * 60,
        daily_times="08:00,12:00,17:00,24:00",
    )
    assert daily == 8 * 3600

    dev = scheduler.max_interval_seconds(
        scheduler.MODE_DEV,
        dev_interval=15,
        interval_seconds=30 * 60,
        daily_times="08:00,12:00,17:00,24:00",
    )
    assert dev == 15

    fixed = scheduler.max_interval_seconds(
        scheduler.MODE_INTERVAL,
        dev_interval=15,
        interval_seconds=30 * 60,
        daily_times="08:00,12:00,17:00,24:00",
    )
    assert fixed == 30 * 60


def test_delay_seconds_matches_mode():
    assert scheduler.delay_seconds(
        scheduler.MODE_DEV, dev_interval=15, interval_seconds=30 * 60, daily_times="08:00"
    ) == 15
    assert scheduler.delay_seconds(
        scheduler.MODE_INTERVAL, dev_interval=15, interval_seconds=30 * 60, daily_times="08:00"
    ) == 30 * 60


# ==================== 参数按需传入 ====================


def test_params_omittable_per_mode():
    """每个模式只需传自己真正用到的参数。

    旧签名把 ``interval_seconds`` / ``daily_times`` 都写成必填，
    调用方为了调 ``dev_interval`` 模式不得不 dummy 传值——正是这个
    「反正用不到」的假设，让调用方养成了无条件求值的坏习惯。
    """
    assert scheduler.delay_seconds(scheduler.MODE_DEV, dev_interval=15) == 15
    assert scheduler.delay_seconds(
        scheduler.MODE_INTERVAL, dev_interval=15, interval_seconds=1800
    ) == 1800
    assert scheduler.delay_seconds(
        scheduler.MODE_DAILY, dev_interval=15, daily_times="08:00,24:00"
    ) > 0

    assert scheduler.max_interval_seconds(scheduler.MODE_DEV, dev_interval=15) == 15
    assert scheduler.max_interval_seconds(
        scheduler.MODE_INTERVAL, dev_interval=15, interval_seconds=1800
    ) == 1800
    assert scheduler.max_interval_seconds(
        scheduler.MODE_DAILY, dev_interval=15, daily_times="08:00,24:00"
    ) == 16 * 3600


@pytest.mark.parametrize(
    ("mode", "kwargs", "missing"),
    [
        (scheduler.MODE_INTERVAL, {"dev_interval": 15}, "interval_seconds"),
        (scheduler.MODE_DAILY, {"dev_interval": 15}, "daily_times"),
    ],
)
def test_mode_still_validates_its_own_params(mode, kwargs, missing):
    """省略可以，但该模式缺了自己要用的参数仍必须报错，不能静默算错。"""
    with pytest.raises(ValueError, match=missing):
        scheduler.delay_seconds(mode, **kwargs)
    with pytest.raises(ValueError, match=missing):
        scheduler.max_interval_seconds(mode, **kwargs)


def test_periodic_task_has_no_per_task_interval_setting():
    """周期巡检不再有 per-task 间隔配置：只剩 dev / daily 两档。

    旧实现把 ``cls._get_interval_minutes() * 60`` 写在**传参位置**，
    三种模式都会求值——而生产默认是 ``daily_times``，于是「一个当前模式
    根本用不到的配置项」缺了 / 拼错，就能把任务带崩。``interval`` 档既已从
    周期巡检移除，`_interval_setting` / `_get_interval_minutes` 一并删除。
    """
    import app.tasks.periodic_task as pt

    assert not hasattr(pt.PeriodicTaskManager, "_get_interval_minutes")
    assert not hasattr(pt.PeriodicTaskManager, "_interval_setting")
    # 模式改由配置层的 computed field 给出
    assert _settings(debug=True).PERIODIC_TASK_MODE == scheduler.MODE_DEV
    assert _settings(debug=False).PERIODIC_TASK_MODE == scheduler.MODE_DAILY


def test_default_mail_poll_config():
    """默认：邮件轮询 15 秒一次，独立于 DEBUG 与全局调度。

    断言字段默认值（而非运行时解析值），避免受环境变量影响。
    """
    fields = Settings.model_fields
    assert fields["MAIL_POLL_INTERVAL_SECONDS"].default == 15
    assert fields["PERIODIC_TASK_DEV_INTERVAL_SECONDS"].default == 15
    assert fields["BACKGROUND_TASK_STARTUP_DELAY_SECONDS"].default == 120


# ==================== 计时器循环 ====================

def test_mail_poll_timer_calls_poll_once(monkeypatch):
    """计时器应在启动宽限后调用 poll_once（而不是等一整个周期）。"""
    calls: list[float] = []
    monkeypatch.setattr(MailPollTimer, "_startup_delay_seconds", classmethod(lambda cls: 0.01))
    monkeypatch.setattr(MailPollTimer, "_next_delay_seconds", classmethod(lambda cls: 0.01))

    async def poll_once():
        calls.append(1)

    async def run():
        await MailPollTimer.start(poll_once)
        await asyncio.sleep(0.1)
        await MailPollTimer.close()

    asyncio.run(run())
    assert len(calls) >= 1


def test_periodic_task_first_run_after_startup_delay(monkeypatch):
    """周期任务首次执行在启动宽限后发生，而不是等一整个调度周期。"""
    import types

    import app.tasks.periodic_task as pt

    class Dummy(pt.PeriodicTaskManager):
        _task_name = "dummy"
        runs = 0

        @classmethod
        async def _run_once(cls):
            cls.runs += 1

    # 启动宽限 0.01s、调度周期 5s：若仍等一整个周期，0.05s 内不会执行
    monkeypatch.setattr(
        pt, "settings",
        types.SimpleNamespace(
            BACKGROUND_TASK_STARTUP_DELAY_SECONDS=0.01,
            BACKGROUND_TASK_RETRY_TIMES=0,
            BACKGROUND_TASK_RETRY_BASE_DELAY_SECONDS=1.0,
            BACKGROUND_TASK_RETRY_MAX_DELAY_SECONDS=1.0,
        ),
    )
    monkeypatch.setattr(Dummy, "_next_delay_seconds", classmethod(lambda cls: 5.0))
    monkeypatch.setattr(
        Dummy, "_resolve_mode", classmethod(lambda cls: scheduler.MODE_INTERVAL)
    )

    async def run():
        await Dummy.init()
        await asyncio.sleep(0.05)
        await Dummy.close()

    asyncio.run(run())
    assert Dummy.runs >= 1


def test_mail_poll_timer_survives_poll_error(monkeypatch):
    """单轮失败不终止循环（重试耗尽后进入下一轮），且失败会计数告警。

    并入 ``PeriodicTaskManager`` 后，``run_with_retry`` 的返回值不再被丢弃：
    重试耗尽会走 ``_on_failure``，达到阈值打 ``[bg_task_alert]``。
    """
    import types

    import app.tasks.periodic_task as pt

    calls: list[int] = []
    monkeypatch.setattr(MailPollTimer, "_startup_delay_seconds", classmethod(lambda cls: 0.01))
    monkeypatch.setattr(MailPollTimer, "_next_delay_seconds", classmethod(lambda cls: 0.01))
    monkeypatch.setattr(
        pt, "settings",
        types.SimpleNamespace(
            BACKGROUND_TASK_RETRY_TIMES=0,            # 不重试，直接算执行失败
            BACKGROUND_TASK_RETRY_BASE_DELAY_SECONDS=1.0,
            BACKGROUND_TASK_RETRY_MAX_DELAY_SECONDS=1.0,
            PERIODIC_TASK_FAILURE_ALERT_THRESHOLD=1,
        ),
    )

    async def poll_once():
        calls.append(1)
        raise RuntimeError("boom")

    async def run():
        await MailPollTimer.start(poll_once)
        await asyncio.sleep(0.15)
        await MailPollTimer.close()

    asyncio.run(run())
    assert len(calls) > 1
    assert MailPollTimer._consecutive_failures >= 1, "失败应被计数"


def test_mail_poll_timer_close_is_idempotent():
    async def run():
        await MailPollTimer.close()   # 未启动时调用不应报错
        await MailPollTimer.close()

    asyncio.run(run())


# ==================== 失败重试 ====================

def test_run_with_retry_recovers():
    """中途成功：返回 None，且不再继续尝试。"""
    calls: list[int] = []

    async def fn():
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("boom")

    err = asyncio.run(run_with_retry(fn, retries=3, base_delay=0.001))
    assert err is None
    assert len(calls) == 3


def test_run_with_retry_exhausts():
    """重试耗尽：返回最后一个异常，共尝试 首次 + retries 次。"""
    calls: list[int] = []

    async def fn():
        calls.append(1)
        raise RuntimeError("boom")

    err = asyncio.run(run_with_retry(fn, retries=2, base_delay=0.001))
    assert isinstance(err, RuntimeError)
    assert len(calls) == 3


def test_run_with_retry_zero():
    """retries=0：只尝试一次，不重试。"""
    calls: list[int] = []

    async def fn():
        calls.append(1)
        raise RuntimeError("boom")

    err = asyncio.run(run_with_retry(fn, retries=0))
    assert isinstance(err, RuntimeError)
    assert len(calls) == 1


def test_run_with_retry_cancel_not_retried():
    """取消不重试，直接上抛（关闭时不该被重试拖延）。"""
    calls: list[int] = []

    async def fn():
        calls.append(1)
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run_with_retry(fn, retries=3))
    assert len(calls) == 1


# ==================== 执行失败计数与告警 ====================

def _make_dummy_periodic(monkeypatch, *, fail: bool, threshold: int = 3):
    """构造一个可控成败的周期任务子类，并把时间参数压缩到毫秒级。"""
    import types

    import app.tasks.periodic_task as pt

    class Dummy(pt.PeriodicTaskManager):
        _task_name = "dummy"
        should_fail = fail
        runs = 0

        @classmethod
        async def _run_once(cls):
            cls.runs += 1
            if cls.should_fail:
                raise RuntimeError("boom")

    monkeypatch.setattr(pt, "settings", types.SimpleNamespace(
        BACKGROUND_TASK_STARTUP_DELAY_SECONDS=0.01,
        BACKGROUND_TASK_RETRY_TIMES=0,            # 不重试，直接算执行失败
        BACKGROUND_TASK_RETRY_BASE_DELAY_SECONDS=1.0,
        BACKGROUND_TASK_RETRY_MAX_DELAY_SECONDS=1.0,
        PERIODIC_TASK_FAILURE_ALERT_THRESHOLD=threshold,
    ))
    monkeypatch.setattr(Dummy, "_next_delay_seconds", classmethod(lambda cls: 0.01))
    monkeypatch.setattr(
        Dummy, "_resolve_mode", classmethod(lambda cls: scheduler.MODE_INTERVAL)
    )
    return Dummy


def test_alert_after_tolerating_two_failures(monkeypatch, caplog):
    """容忍 2 次执行失败，第 3 次告警；恢复前只报一次。"""
    Dummy = _make_dummy_periodic(monkeypatch, fail=True, threshold=3)

    async def run():
        await Dummy.init()
        await asyncio.sleep(0.2)     # 足够跑很多轮
        await Dummy.close()

    with caplog.at_level(logging.ERROR):
        asyncio.run(run())

    alerts = [r for r in caplog.records if "[bg_task_alert]" in r.getMessage()]
    assert len(alerts) == 1                      # 只报一次
    assert "连续 3 次执行失败" in alerts[0].getMessage()
    assert Dummy._consecutive_failures >= 3


def test_no_alert_within_tolerance(monkeypatch, caplog):
    """阈值 3 时，只失败 2 次不应告警。"""
    Dummy = _make_dummy_periodic(monkeypatch, fail=True, threshold=3)

    with caplog.at_level(logging.ERROR):
        Dummy._on_failure(RuntimeError("boom"))
        Dummy._on_failure(RuntimeError("boom"))
        assert "[bg_task_alert]" not in [
            r.getMessage() for r in caplog.records
        ]
        Dummy._on_failure(RuntimeError("boom"))   # 第 3 次 → 告警
        assert any("[bg_task_alert]" in r.getMessage() for r in caplog.records)


def test_recovery_resets_counter_and_allows_next_alert(monkeypatch, caplog):
    """恢复后清零计数并解除告警状态。"""
    Dummy = _make_dummy_periodic(monkeypatch, fail=True, threshold=3)

    with caplog.at_level(logging.ERROR):
        for _ in range(3):
            Dummy._on_failure(RuntimeError("boom"))
        assert Dummy._alert_sent is True

        Dummy._on_success()
        assert Dummy._consecutive_failures == 0
        assert Dummy._alert_sent is False

        for _ in range(3):
            Dummy._on_failure(RuntimeError("boom"))
        # 恢复后再次达标 → 允许再次告警
        assert sum(
            "[bg_task_alert]" in r.getMessage() for r in caplog.records
        ) == 2


# ==================== 日志标识 ====================

def test_log_name_carries_class_and_task_name():
    """日志标识 = ``类名:任务名``，不依赖 _task_name 起得不重样。

    只写 ``_task_name`` 时，一旦某个子类忘记覆盖（会落到基类默认的
    "周期性任务"），两个不同任务的日志就分不开了。
    """
    import app.tasks.periodic_task as pt

    class Dummy(pt.PeriodicTaskManager):
        _task_name = "dummy"

    assert Dummy._log_name() == "Dummy:dummy"


def test_all_task_logs_use_the_same_identifying_name(monkeypatch, caplog):
    """同一任务的启动 / 失败 / 告警日志必须用同一个标识，不能一半带类名一半不带。"""
    import logging
    import types

    import app.tasks.periodic_task as pt

    class Dummy(pt.PeriodicTaskManager):
        _task_name = "dummy"

    monkeypatch.setattr(
        pt, "settings",
        types.SimpleNamespace(
            BACKGROUND_TASK_STARTUP_DELAY_SECONDS=0.01,
            BACKGROUND_TASK_RETRY_TIMES=0,
            BACKGROUND_TASK_RETRY_BASE_DELAY_SECONDS=1.0,
            BACKGROUND_TASK_RETRY_MAX_DELAY_SECONDS=1.0,
            PERIODIC_TASK_FAILURE_ALERT_THRESHOLD=1,
        ),
    )
    monkeypatch.setattr(Dummy, "_next_delay_seconds", classmethod(lambda cls: 5.0))
    monkeypatch.setattr(
        Dummy, "_resolve_mode", classmethod(lambda cls: scheduler.MODE_INTERVAL)
    )

    with caplog.at_level(logging.INFO):
        asyncio.run(Dummy.init())
        asyncio.run(asyncio.sleep(0.02))
        Dummy._on_failure(RuntimeError("boom"))
        asyncio.run(Dummy.close())

    tagged = [
        r.getMessage()
        for r in caplog.records
        if r.getMessage().startswith("Dummy:dummy")
    ]
    assert len(tagged) >= 3, f"启动/失败/停止都应带统一标识，实际={tagged}"
    assert any("已启动" in m for m in tagged)
    assert any("执行失败" in m for m in tagged)
    assert any("已停止" in m for m in tagged)
    # 裸任务名单独出现（不带类名）说明又漏改了某处
    assert not [
        r.getMessage()
        for r in caplog.records
        if r.getMessage().startswith("dummy")
    ]
