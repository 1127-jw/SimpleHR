"""时间统一策略的守门测试。

替代 ``utils/time_ISO8601_datatime.py`` 之后，全项目的时间只从 ``app/core/time.py``
进出。这里既测换算语义，也扫源码防止「手写时区」重新长回来 ——
后者更重要：一旦有第二处 ``timedelta(hours=8)``，naive/aware 混用的比较错误
就只能等运行时 ``TypeError`` 才暴露。
"""
from __future__ import annotations

import importlib.util
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.core.db import Base
from app.core.time import (
    BEIJING_TZ,
    BEIJING_TZ_NAME,
    db_now,
    from_iso8601,
    from_naive_beijing,
    now_beijing,
    to_iso8601,
    to_naive_beijing,
)
from app.models.candidate import CandidateModel

ROOT = Path(__file__).resolve().parents[1]
# datetime.now(timezone.utc) 是 JWT/审计日志的既定策略，不归北京时间模块管
UTC_SOURCE_ALLOWLIST = {"app/core/auth.py", "app/core/audit.py"}


def _sources() -> list[tuple[str, str]]:
    return [
        (path.relative_to(ROOT).as_posix(), path.read_text(encoding="utf-8"))
        for sub in ("app", "utils")
        for path in sorted((ROOT / sub).rglob("*.py"))
        if "__pycache__" not in path.parts
    ]


def test_old_module_is_gone():
    assert importlib.util.find_spec("utils.time_ISO8601_datatime") is None


def test_no_hand_rolled_timezone():
    offenders = []
    for rel_path, source in _sources():
        if rel_path == "app/core/time.py":
            continue  # 时区常量只允许在 app/core/time.py 里定义
        for banned in (
            "time_ISO8601_datatime",
            "timedelta(hours=8)",
            "replace(tzinfo=None)",
            "datetime.utcnow",
        ):
            if banned in source:
                offenders.append(f"{rel_path}: {banned}")
        if rel_path not in UTC_SOURCE_ALLOWLIST and "datetime.now()" in source:
            offenders.append(f"{rel_path}: 裸 datetime.now()")
    assert not offenders, "取时间/转时区请统一走 app.core.time：" + "；".join(offenders)


def test_db_columns_store_naive_beijing():
    # SQLAlchemy 会把 default/onupdate 的可调用对象包一层（签名多一个 ctx），
    # 因此只能断言产出值的形态，不能比对函数身份
    created = CandidateModel.__table__.c.created_at
    updated = CandidateModel.__table__.c.updated_at
    assert created.type.timezone is False, "created_at 是 TIMESTAMP WITHOUT TIME ZONE"
    assert created.default.is_callable and updated.onupdate.is_callable
    assert created.default.arg(None).tzinfo is None
    assert updated.onupdate.arg(None).tzinfo is None


def test_base_metadata_columns_are_naive():
    for model in Base.registry.mappers:
        for name in ("created_at", "updated_at"):
            assert model.local_table.c[name].type.timezone is False, f"{model.class_.__name__}.{name}"


def test_db_now_matches_beijing_wall_clock():
    naive = db_now()
    assert naive.tzinfo is None
    assert abs((now_beijing().replace(tzinfo=None) - naive).total_seconds()) < 5


def test_aware_utc_converted_before_storing():
    assert to_naive_beijing(
        datetime(2026, 11, 20, 2, 30, tzinfo=timezone.utc)
    ) == datetime(2026, 11, 20, 10, 30)


def test_naive_input_is_already_beijing():
    naive = datetime(2026, 11, 20, 10, 30)
    assert to_naive_beijing(naive) == naive
    assert from_naive_beijing(naive) == naive.replace(tzinfo=BEIJING_TZ)


def test_round_trip_keeps_wall_clock():
    original = now_beijing()
    assert from_naive_beijing(to_naive_beijing(original)) == original


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2026-11-20T10:30:00+08:00", datetime(2026, 11, 20, 10, 30, tzinfo=BEIJING_TZ)),
        ("2026-11-20T02:30:00Z", datetime(2026, 11, 20, 10, 30, tzinfo=BEIJING_TZ)),
        ("2026-11-20T10:30:00.123456+08:00", datetime(2026, 11, 20, 10, 30, 0, 123456, tzinfo=BEIJING_TZ)),
        # 无时区标记的按北京时间理解
        ("2026-11-20T10:30:00", datetime(2026, 11, 20, 10, 30, tzinfo=BEIJING_TZ)),
    ],
)
def test_from_iso8601(raw: str, expected: datetime):
    assert from_iso8601(raw) == expected


def test_to_iso8601_marks_offset_and_drops_microseconds():
    assert to_iso8601(datetime(2026, 11, 20, 10, 30, 12, 999999)) == "2026-11-20T10:30:12+08:00"


def test_iso8601_round_trip():
    assert from_iso8601(to_iso8601(now_beijing())) is not None


def test_none_passes_through():
    assert to_naive_beijing(None) is None
    assert to_iso8601(None) is None
    assert from_iso8601(None) is None


def test_zone_name_shared_with_dingtalk():
    assert BEIJING_TZ_NAME == "Asia/Shanghai"
    assert BEIJING_TZ.tzname(None) == BEIJING_TZ_NAME
