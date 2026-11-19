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
    """够用的内存版 Redis，覆盖 token 与限流相关命令。

    ``eval`` 会**按脚本文本分派**到对应的 Python 等价实现，而不是无条件返回成功：
    原实现直接忽略 ``script`` 参数做了一次 GETDEL，于是滑动窗口限流脚本
    （ZREMRANGEBYSCORE / ZCARD / ZADD）在测试里被当成「取一个 key」，
    返回恒为真值 → 限流测试全部空过。
    未知脚本直接抛错，避免以后再出现「加了 Lua 但测试静默通过」。
    """

    def __init__(self) -> None:
        self.store: dict[str, Any] = {}
        self.ttl: dict[str, int] = {}
        # ZSET：key -> {member: score}
        self.zsets: dict[str, dict[str, float]] = {}

    async def setex(self, name: str, time: int, value: Any) -> bool:
        self.store[name] = value
        self.ttl[name] = time
        return True

    async def set(
        self,
        name: str,
        value: Any,
        ex: int | None = None,
        nx: bool = False,
    ) -> bool | None:
        # nx=True 用于分布式锁：键已存在时不写入并返回 None（与 redis-py 一致）
        if nx and name in self.store:
            return None
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
            if name in self.zsets:
                del self.zsets[name]
        return removed

    async def zadd(self, name: str, mapping: dict[str, float]) -> int:
        zset = self.zsets.setdefault(name, {})
        added = 0
        for member, score in mapping.items():
            if member not in zset:
                added += 1
            zset[member] = score
        return added

    async def zcard(self, name: str) -> int:
        return len(self.zsets.get(name, {}))

    async def zremrangebyscore(self, name: str, low: float, high: float) -> int:
        zset = self.zsets.get(name)
        if not zset:
            return 0
        doomed = [m for m, score in zset.items() if low <= score <= high]
        for member in doomed:
            del zset[member]
        return len(doomed)

    async def eval(self, script: str, numkeys: int, *args: Any) -> Any:
        keys = list(args[:numkeys])
        argv = list(args[numkeys:])

        if "ZREMRANGEBYSCORE" in script:
            # 滑动窗口限流：清理窗口外成员 → 计数 → 未超限则记录本次
            key = keys[0]
            now, window, limit = float(argv[0]), float(argv[1]), int(argv[2])
            member = argv[3]
            await self.zremrangebyscore(key, 0, now - window)
            if await self.zcard(key) < limit:
                await self.zadd(key, {member: now})
                return 1
            return 0

        if script.strip().startswith("if redis.call('GET'"):
            # 带持有者校验的释放锁：GET == ARGV[1] 才 DEL。
            # 必须排在通用 GET 分支之前——两个脚本都包含 "redis.call('GET'"。
            current = self.store.get(keys[0])
            if current is not None and current == argv[0]:
                del self.store[keys[0]]
                return 1
            return 0

        if "redis.call('GET'" in script:
            # 原子消费（refresh token）：GET 到就 DEL，否则 nil
            value = self.store.get(keys[0])
            if value is not None:
                del self.store[keys[0]]
                return value
            return None

        raise AssertionError(
            "FakeRedis 未实现该 Lua 脚本的等价语义，请补实现而不是让它静默通过"
        )


@pytest.fixture
def fake_redis() -> FakeRedis:
    return FakeRedis()


@pytest.fixture
def state_factory():
    """构造一份最小的 CandidateRecruitmentState。
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
    """
    工作区内的临时目录。
    """
    base = Path(__file__).resolve().parent / ".tmp"
    path = base / uuid4().hex
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)

# autouse=True 是 pytest 中 @pytest.fixture 的一个参数。
# 设置后，这个 fixture 会在它的作用域内自动对所有测试生效，
# 不需要在测试函数参数里显式写出 fixture 名
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
