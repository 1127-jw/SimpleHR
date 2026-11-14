"""CORS 跨域中间件配置。"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware


def setup_cors(app: FastAPI) -> None:
    """配置 CORS 中间件。

    - allow_origins=["*"]：允许所有来源
    - allow_credentials=False：不允许携带凭证（与 "*" 不兼容）
    - allow_methods=["*"]：允许所有 HTTP 方法
    - allow_headers=["*"]：允许所有请求头
    """
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
