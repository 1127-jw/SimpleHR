"""LLM 客户端工厂。
需要模型时调用 ``get_deepseek_llm()`` / ``get_qwen_llm()``：进程内单例，
首次调用才建连。
"""
from functools import lru_cache

from langchain_openai import ChatOpenAI

from app.core.config import get_settings

BASE_URL = "https://maas.qianwenaiapi.com/compatible-mode/v1"
PRIMARY_MODEL = "deepseek-v4.1-flash"
FALLBACK_MODEL = "qwen3.8-max"


def _chat_model(model: str) -> ChatOpenAI:
    """按 Settings 组装一个 OpenAI 兼容协议的模型实例。"""
    return ChatOpenAI(
        model=model,
        base_url=BASE_URL,
        api_key=get_settings().DASHSCOPE_API_KEY,
    )


@lru_cache(maxsize=1)
def get_deepseek_llm() -> ChatOpenAI:
    """主模型。"""
    return _chat_model(PRIMARY_MODEL)


@lru_cache(maxsize=1)
def get_qwen_llm() -> ChatOpenAI:
    """备用模型：主模型失败时由 ``RunnableWithFallbacks`` / ``ModelFallbackMiddleware`` 接管。"""
    return _chat_model(FALLBACK_MODEL)
