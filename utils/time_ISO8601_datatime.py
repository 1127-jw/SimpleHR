"""
时区处理工具模块 —— 全项目统一时区策略

策略约定：
1. 所有业务时间均为北京时间（UTC+8），内部使用 timezone-aware datetime
2. 数据库存储使用 naive datetime（已转为北京时间后剥离时区信息）
3. JWT/认证相关时间使用 UTC（保持不变）
4. 外部 API（钉钉）使用 ISO-8601 格式字符串 + Asia/Shanghai 时区标识

使用方式：
    from utils.time_ISO8601_datatime import BEIJING_TZ, now_beijing, to_naive_beijing, to_iso8601, from_iso8601

    # 获取当前北京时间（带时区）
    now = now_beijing()

    # 存入数据库前：转为 naive 北京时间
    db_value = to_naive_beijing(some_datetime)

    # 传给钉钉 API
    iso_str = to_iso8601(some_datetime)

    # 从钉钉 API 解析
    dt = from_iso8601(iso_str)
"""
from datetime import datetime, timezone, timedelta
import re

# ============================================================
# 全局唯一时区常量 —— 整个项目共享，不再在每个函数内部创建
# ============================================================
BEIJING_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
"""北京时间时区（UTC+8），全项目统一使用"""


# ============================================================
# 辅助函数
# ============================================================

def now_beijing() -> datetime:
    """获取当前北京时间（timezone-aware）"""
    return datetime.now(BEIJING_TZ)


def to_naive_beijing(dt: datetime) -> datetime:
    """
    将任意 datetime 转为 naive 北京时间（用于数据库存储）。

    处理逻辑：
    1. 如果是 naive datetime → 假设已是北京时间，直接剥离时区（不变数值）
    2. 如果是 aware datetime → 先转为北京时间，再剥离时区

    这确保存入 PostgreSQL 的 DateTime 列始终是北京时间数值。
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=None)
    return dt.astimezone(BEIJING_TZ).replace(tzinfo=None)


def to_iso8601(dt: datetime) -> str:
    """
    将 datetime 转为 ISO-8601 格式字符串（北京时间，带 +08:00 偏移）。

    用于：钉钉 API 调用、邮件中的时间展示、日志记录。
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        # naive datetime 视为北京时间
        dt = dt.replace(tzinfo=BEIJING_TZ)
    else:
        dt = dt.astimezone(BEIJING_TZ)
    return dt.replace(microsecond=0).isoformat()


def from_iso8601(iso_str: str) -> datetime:
    """
    将 ISO-8601 格式字符串解析为北京时间 datetime（timezone-aware）。

    支持格式：
    - 带时区偏移: "2022-11-27T08:30:00+08:00"
    - UTC 标记: "2022-11-27T00:30:00Z"
    - 无时区（naive）: "2022-11-27T08:30:00"（视为北京时间）
    """
    if iso_str is None:
        return None

    normalized_str = iso_str.strip()
    if normalized_str.endswith('Z'):
        normalized_str = normalized_str[:-1] + '+00:00'

    # 处理微秒部分可能缺少补零的情况
    normalized_str = re.sub(
        r'(\.\d{1,6})(?![\d:])',
        lambda m: m.group(1) + '0' * (6 - len(m.group(1)) + 1),
        normalized_str
    )

    try:
        dt = datetime.fromisoformat(normalized_str)
    except ValueError:
        try:
            if ' ' in normalized_str and 'T' not in normalized_str:
                normalized_str = normalized_str.replace(' ', 'T', 1)
            dt = datetime.fromisoformat(normalized_str)
        except Exception:
            raise ValueError(f"无法解析 ISO 8601 字符串: '{iso_str}'")

    if dt.tzinfo is None:
        return dt.replace(tzinfo=BEIJING_TZ)
    return dt.astimezone(BEIJING_TZ)


# ============================================================
# 向后兼容的别名（保持旧代码可用）
# ============================================================

def datetime_to_iso8601_beijing(dt: datetime) -> str:
    """向后兼容别名，等同于 to_iso8601"""
    return to_iso8601(dt)


def iso8601_to_datetime_beijing(iso_str: str) -> datetime:
    """向后兼容别名，等同于 from_iso8601"""
    return from_iso8601(iso_str)
