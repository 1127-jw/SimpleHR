"""禁用浏览器 / 中间层缓存。

为什么需要它：浏览器对 ``GET 200`` 会做**启发式缓存**。响应只要没带
``Cache-Control`` / ``Expires``，Chrome 在本地开发期、重复刷新、重复导航时
就会直接命中磁盘缓存——DevTools 显示 ``200 OK (from disk cache)``，
请求**根本没有发到服务器**。动态接口一旦被这样缓存，就会出现
「数据明明变了却还是旧的」的错觉。

实测：本项目接口此前只发 ``content-length`` / ``content-type``，
没有任何缓存指令，等于把决定权完全交给浏览器。

``no-store`` 是最高优先级的指令：任何缓存都不得存储该响应。
（对比 ``no-cache``：仍然存储，只是每次都要 revalidate，仍会发请求。）

注意：真正的静态资源（JS/CSS/图片）**不要**加这个头，否则每次都要重新下载。
本项目已移除 ``/media`` 静态挂载，当前没有需要放过的静态路由。
"""
from fastapi import FastAPI, Request, Response


async def no_store_middleware(request: Request, call_next) -> Response:
    """给所有响应加 ``Cache-Control: no-store``。"""
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


def setup_no_store(app: FastAPI) -> None:
    """注册禁用缓存中间件。"""
    app.middleware("http")(no_store_middleware)
