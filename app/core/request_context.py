"""请求上下文：在异步调用链中传递 request_id。

用 ``contextvars`` 而不是 threading.local —— Web 层是异步的，同一个线程会
承载多个并发请求，threading.local 会串味。

用法：
    - 由 ``main.py`` 的中间件在请求进入时 ``set_request_id()``
    - 日志通过 ``logging`` 的 LogRecordFactory 自动带上 ``request_id``
    - 需要时调用 ``get_request_id()`` 读取
"""
from __future__ import annotations

import uuid
from contextvars import ContextVar

_request_id: ContextVar[str] = ContextVar("request_id", default="-")


def new_request_id() -> str:
    """生成一个新的 request id。"""
    return uuid.uuid4().hex


def set_request_id(request_id: str) -> None:
    _request_id.set(request_id)


def get_request_id() -> str:
    """当前上下文的 request id；不在请求内时返回 ``"-"``。"""
    return _request_id.get()
