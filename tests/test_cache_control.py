"""禁用缓存中间件测试。

背景：接口此前只发 ``content-length`` / ``content-type``，没有任何缓存指令。
浏览器对 ``GET 200`` 会做启发式缓存——DevTools 显示
``200 OK (from disk cache)``，请求**根本没有发到服务器**，
于是「数据变了却还是旧的」。这里锁定 ``Cache-Control: no-store`` 的契约。
"""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.middleware import setup_middlewares
from app.middleware.cache_control_middleware import no_store_middleware


def _app() -> FastAPI:
    app = FastAPI()

    @app.get("/ping")
    async def ping():
        return {"ok": True}

    return app


def test_middleware_sets_no_store():
    app = _app()
    app.middleware("http")(no_store_middleware)

    resp = TestClient(app).get("/ping")

    assert resp.status_code == 200
    assert resp.headers["Cache-Control"] == "no-store"


def test_setup_middlewares_wires_no_store():
    """注册入口要真的接上——只测中间件本身的话，漏注册不会被发现。"""
    app = _app()
    setup_middlewares(app)

    resp = TestClient(app).get("/ping")

    assert resp.headers.get("Cache-Control") == "no-store"


def test_no_store_covers_non_get_too():
    """POST 响应同样不该被缓存。"""
    app = _app()

    @app.post("/ping")
    async def ping_post():
        return {"ok": True}

    app.middleware("http")(no_store_middleware)

    assert TestClient(app).post("/ping").headers["Cache-Control"] == "no-store"
