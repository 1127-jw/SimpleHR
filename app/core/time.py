"""
全项目唯一的时间出入口 —— 统一时区策略

策略：
1. 业务时间在代码里始终是 timezone-aware 的北京时间；只有跨过两道边界时才改变表示形式。
2. 数据库边界：``candidates`` 等表的时间列是 ``TIMESTAMP WITHOUT TIME ZONE``，
   存的是**已换算成北京时间的 naive 数值**。写库过 ``db_now()`` / ``to_naive_beijing()``，
   读出来要参与业务计算时用 ``from_naive_beijing()`` 补回时区。
3. 外部 API 边界（钉钉）：用 ISO-8601 字符串 + ``+08:00`` 偏移，过 ``to_iso8601()`` /
   ``from_iso8601()``。
4. JWT/审计日志用 UTC（``app/core/auth.py``、``app/core/audit.py``），不归本模块管。

除本模块外，业务代码里不应再出现 ``timedelta(hours=8)``、``replace(tzinfo=None)``
或裸 ``datetime.now()``。
"""
from datetime import datetime, timedelta, timezone

# 中国自 1991 年起不再实行夏令时，固定 +08:00 与 ZoneInfo("Asia/Shanghai") 等价，
# 但不依赖镜像里的 tzdata（python:slim 系列默认没有）。
BEIJING_TZ_NAME = "Asia/Shanghai"
BEIJING_TZ = timezone(timedelta(hours=8), name=BEIJING_TZ_NAME)


def now_beijing() -> datetime:
    """当前时间（aware，北京时间）。"""
    return datetime.now(BEIJING_TZ)


def db_now() -> datetime:
    """当前时间（naive 北京时间），给 ORM 时间列和查询条件直接用。"""
    return now_beijing().replace(tzinfo=None)


def to_naive_beijing(dt: datetime | None) -> datetime | None:
    """aware 或 naive datetime → naive 北京时间（写库）。

    naive 输入视为已经是北京时间数值，只剥时区不改数值。
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(BEIJING_TZ).replace(tzinfo=None)


def from_naive_beijing(dt: datetime) -> datetime:
    """naive 北京时间（数据库读出的值、时间字符串）→ aware 北京时间。

    aware 输入只做时区换算，原样返回，因此调用方不必分辨来源。
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=BEIJING_TZ)
    return dt.astimezone(BEIJING_TZ)


def to_iso8601(dt: datetime) -> str | None:
    """datetime → ISO-8601 字符串（北京时间，带 +08:00，精确到秒）。

    钉钉接口不接受微秒，这里统一截掉；naive 输入视为北京时间。
    """
    if dt is None:
        return None
    return from_naive_beijing(dt).replace(microsecond=0).isoformat()


def from_iso8601(iso_str: str | None) -> datetime | None:
    """ISO-8601 字符串 → aware 北京时间。

    支持 ``+08:00`` 偏移、``Z``、任意位数的秒小数；无时区标记的按北京时间处理。
    Python 3.11+ 的 ``fromisoformat`` 已覆盖这些形式，无需再做字符串预处理。
    """
    if iso_str is None:
        return None
    return from_naive_beijing(datetime.fromisoformat(iso_str))
