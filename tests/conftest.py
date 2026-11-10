"""pytest 公共夹具。

原则：**不依赖数据库 / Redis / 网络 / LLM**，纯内存与纯函数，
保证测试能在任何机器上秒级跑完。
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest


class FakeRedis:
    """够用的内存版 Redis，覆盖 token 相关命令。"""

    def __init__(self) -> None:
        self.store: dict[str, Any] = {}
        self.ttl: dict[str, int] = {}

    async def setex(self, name: str, time: int, value: Any) -> bool:
        self.store[name] = value
        self.ttl[name] = time
        return True

    async def set(self, name: str, value: Any, ex: int | None = None) -> bool:
        self.store[name] = value
        if ex is not None:
            self.ttl[name] = ex
        return True

    async def get(self, name: str) -> Any:
        return self.store.get(name)

    async def exists(self, *names: str) -> int:
        return sum(1 for n in names if n in self.store)

    async def delete(self, *names: str) -> int:
        removed = 0
        for name in names:
            if name in self.store:
                del self.store[name]
                removed += 1
        return removed


@pytest.fixture
def fake_redis() -> FakeRedis:
    return FakeRedis()


@pytest.fixture
def state_factory():
    """构造一份最小的 CandidateRecruitmentState。

    路由函数只用到 ``candidate.id`` / ``candidate.name``，所以用
    SimpleNamespace 代替重量级的 Pydantic schema。
    """

    def _make(**overrides) -> dict:
        state: dict[str, Any] = {
            "candidate": SimpleNamespace(id="cand-001", name="张三"),
            "position": SimpleNamespace(id="pos-001", title="后端工程师"),
            "interviewer": SimpleNamespace(id="user-001"),
            "current_stage": "screening",
        }
        state.update(overrides)
        return state

    return _make


@pytest.fixture
def work_dir():
    """工作区内的临时目录。

    不用 pytest 的 ``tmp_path``：它落在系统 Temp 下，部分受限环境
    （沙箱 / 只读 Temp）会写不进去。
    """
    base = Path(__file__).resolve().parent / ".tmp"
    path = base / uuid4().hex
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture(autouse=True)
def _quiet_logging():
    """测试期间把 root logger 压到 WARNING，避免日志刷屏。"""
    root = logging.getLogger()
    previous = root.level
    root.setLevel(logging.WARNING)
    try:
        yield
    finally:
        root.setLevel(previous)
