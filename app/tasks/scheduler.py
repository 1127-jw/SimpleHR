"""后台任务的调度策略。

生效模式由 ``Settings`` 的 computed field 给出，本模块只负责**按模式算时间**：

* ``PERIODIC_TASK_MODE``：周期巡检用，``debug`` → ``dev_interval``（15 秒），
  否则 ``daily_times``（每日时刻）；
* ``MAIL_POLL_MODE``：邮件轮询用，恒为 ``interval``（默认 15 秒）。

三种模式常量：

* ``dev_interval``：开发/调试，固定短间隔
* ``interval``：固定间隔（秒）
* ``daily_times``：每日指定时刻
"""
from __future__ import annotations

from datetime import datetime, timedelta

from app.core.time import now_beijing

MODE_DEV = "dev_interval"
MODE_INTERVAL = "interval"
MODE_DAILY = "daily_times"

_DAY_SECONDS = 24 * 3600


def parse_daily_minutes(spec: str) -> list[int]:
    """把 ``"08:00,12:00,17:00,24:00"`` 解析成「当天第几分钟」列表。

    ``24:00`` 归一化为 ``1440``（即次日 00:00），便于计算跨天间隔。
    结果去重并升序排列。
    """
    minutes: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        hour_s, minute_s = part.split(":")
        hour, minute = int(hour_s), int(minute_s)
        if not (0 <= minute < 60):
            raise ValueError(f"时刻分钟数非法: {part!r}")
        if hour >= 24:
            if hour != 24 or minute != 0:
                raise ValueError(f"超过 24:00 的时刻不支持: {part!r}")
            minutes.add(_DAY_SECONDS // 60)
        else:
            minutes.add(hour * 60 + minute)
    if not minutes:
        raise ValueError(f"每日执行时刻配置解析为空: {spec!r}")
    return sorted(minutes)


def seconds_until_next_daily_time(spec: str, *, now: datetime | None = None) -> float:
    """计算距离下一个每日执行时刻还有多少秒。

    Args:
        spec: 形如 ``"08:00,12:00,17:00,24:00"``；``24:00`` 视为次日 00:00。
        now: 当前时间（默认取北京时区的现在），便于测试。

    Returns:
        距下一个时刻的秒数（恒为正数）。
    """
    now = now or now_beijing()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    candidates = []
    for m in parse_daily_minutes(spec):
        base = today + timedelta(minutes=m)
        if base <= now:
            base += timedelta(days=1)
        candidates.append(base)
    return (min(candidates) - now).total_seconds()


def daily_max_gap_seconds(spec: str) -> float:
    """时刻表中相邻两次执行的最大间隔（含首尾跨天），单位秒。

    用于推导心跳阈值：只要任务在「最大间隔 × 倍数」内成功执行过，就视为存活。
    只配置了一个时刻时，按 24 小时计。
    """
    minutes = parse_daily_minutes(spec)
    if len(minutes) < 2:
        return float(_DAY_SECONDS)
    gaps = [
        minutes[i + 1] - minutes[i] for i in range(len(minutes) - 1)
    ]
    # 首尾跨天：最后一个时刻 → 次日第一个时刻
    gaps.append(minutes[0] + (_DAY_SECONDS // 60) - minutes[-1])
    return float(max(gaps) * 60)


def delay_seconds(
    mode: str,
    *,
    dev_interval: float,
    interval_seconds: float | None = None,
    daily_times: str | None = None,
) -> float:
    """按模式计算下一次执行的等待秒数。

    只有当前模式真正用到的参数才需要传：``interval_seconds`` 仅在
    ``interval`` 模式、``daily_times`` 仅在 ``daily_times`` 模式被读取。
    这样调用方可以**按需取值**——否则「读一个当前模式用不到的配置」一旦失败，
    会把一个本来完全正常的模式一起带崩（见 ``PeriodicTaskManager`` 的注释）。
    """
    if mode == MODE_DEV:
        return max(1.0, float(dev_interval))
    if mode == MODE_INTERVAL:
        if interval_seconds is None:
            raise ValueError("interval 模式必须提供 interval_seconds")
        return max(1.0, float(interval_seconds))
    if not daily_times:
        raise ValueError("daily_times 模式必须提供 daily_times")
    return seconds_until_next_daily_time(daily_times)


def max_interval_seconds(
    mode: str,
    *,
    dev_interval: float,
    interval_seconds: float | None = None,
    daily_times: str | None = None,
) -> float:
    """按模式计算「相邻两次执行的最大间隔」，用于心跳阈值推导。

    这是后台任务健康检查的关键输入：阈值必须跟调度配置联动，
    写死（例如固定 300 秒）在 ``daily_times`` 模式下会永久误报。

    参数语义同 :func:`delay_seconds`：用不到的参数可以不传。
    """
    if mode == MODE_DEV:
        return max(1.0, float(dev_interval))
    if mode == MODE_INTERVAL:
        if interval_seconds is None:
            raise ValueError("interval 模式必须提供 interval_seconds")
        return max(1.0, float(interval_seconds))
    if not daily_times:
        raise ValueError("daily_times 模式必须提供 daily_times")
    return daily_max_gap_seconds(daily_times)
