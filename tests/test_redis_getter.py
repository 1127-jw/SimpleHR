"""Redis 客户端 getter 的契约测试。

线上事故：``get_redis`` 曾写成 ``async def``，``timeout_tasks`` / ``bot`` /
``limiters`` 三处调用方漏了 await，拿到的 ``r`` 其实是 coroutine，
一调 ``.set()`` / ``.eval()`` 就报
``'coroutine' object has no attribute 'set'`` ——
``TimeoutSweepManager`` 因此连续失败 30 轮（日志里能看到
``执行失败（重试耗尽，连续第 30 次）``）。

这里锁两条契约：

1. ``get_redis`` 必须是**同步** getter（它只是取模块级单例，没有 I/O）；
2. 巡检走**真实** ``get_redis`` 能跑通——旧测试把它 monkeypatch 成 sync lambda，
   恰好把这个 bug 藏住了。
"""
from __future__ import annotations

import inspect

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.core import redis_client
from app.core.redis_client import get_redis


def test_get_redis_is_a_plain_getter():
    """它只是取单例，不该是 async——async 正是这次事故的根因。"""
    assert not inspect.iscoroutinefunction(get_redis)


def test_sync_getter_still_works_as_fastapi_dependency(monkeypatch, fake_redis):
    """``Depends(get_redis)`` 这条路要仍然可用——10 个路由在用它。

    getter 从 async 改成 sync 后，唯一的风险就是依赖注入路径。
    """
    monkeypatch.setattr(redis_client, "redis_client", fake_redis)

    app = FastAPI()

    @app.get("/who")
    async def who(r=Depends(get_redis)):
        return {"same": r is fake_redis}

    assert TestClient(app).get("/who").json() == {"same": True}


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    def begin(self):
        return _Tx()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


async def test_sweep_works_with_real_get_redis(monkeypatch, fake_redis):
    """用真实 ``get_redis`` 跑一遍巡检，而不是替换成 sync lambda。

    改前这里会直接抛 ``AttributeError: 'coroutine' object has no attribute 'set'``；
    改后应正常返回。
    """
    from app.tasks import timeout_tasks

    monkeypatch.setattr(redis_client, "redis_client", fake_redis)
    monkeypatch.setattr(timeout_tasks.settings, "GRAPH_OPEN", True)

    class _Repo:
        def __init__(self, session=None):
            pass

        async def list_stale_negotiating(self, cutoff, limit=200):
            return []

    monkeypatch.setattr(timeout_tasks, "AsyncSessionFactory", lambda: _Session())
    monkeypatch.setattr(timeout_tasks, "CandidateStatusHistoryRepo", _Repo)

    # 刻意不碰 timeout_tasks.get_redis —— 走真实 getter
    assert await timeout_tasks.sweep_stale_email_replies() == 0
    assert fake_redis.store.get(timeout_tasks._SWEEP_LOCK_KEY) is None, "本轮应释放自己加的锁"
