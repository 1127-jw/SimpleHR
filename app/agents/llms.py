"""LLM 客户端工厂 —— **全项目唯一的模型构建入口**。

需要模型时统一从这里取：

* ``get_deepseek_llm()`` / ``get_qwen_llm()``：裸模型实例（进程内单例，首次调用才建连）；
* ``build_structured_llm(schema)``：主模型 + 备用模型，都绑定结构化输出，
  供评分 / 时间解析 / 意图识别等需要「严格 JSON」的场景使用。

**不要**在业务代码里自行拼装 ``with_structured_output`` + ``RunnableWithFallbacks``
（评分、时间解析、意图识别此前各写了一份，改一处漏两处）。
"""
from functools import lru_cache

from langchain_core.runnables import RunnableWithFallbacks
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
    """备用模型：主模型失败时由 ``RunnableWithFallbacks`` 接管。"""
    return _chat_model(FALLBACK_MODEL)


def build_structured_llm(schema) -> RunnableWithFallbacks:
    """构建「主 + 备」双模型的结构化输出链。

    Args:
        schema: 结构化输出的目标 schema（Pydantic 模型 / TypedDict / JSON Schema），
            直接透传给 ``with_structured_output``。

    Returns:
        ``RunnableWithFallbacks``：主模型失败时自动降级到备用模型。

    用法::

        llm = build_structured_llm(AgentCandidateScoreSchema)
        result = await llm.ainvoke(messages)

    需要在调用方自行缓存（``with_structured_output`` 每次都会包一层，
    别放在热路径里反复构造）。
    """
    primary = get_deepseek_llm().with_structured_output(schema)
    fallback = get_qwen_llm().with_structured_output(schema)
    return RunnableWithFallbacks(runnable=primary, fallbacks=[fallback])
