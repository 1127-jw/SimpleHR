import os
from langchain_openai import ChatOpenAI
from app.core.config import get_settings
from langchain.chat_models import init_chat_model

settings = get_settings()

api_key = settings.DASHSCOPE_API_KEY

qwen_llm = ChatOpenAI(
    model="qwen3.8-max",
    base_url="https://maas.qianwenaiapi.com/compatible-mode/v1",
    api_key=api_key,
)

deepseek_llm = ChatOpenAI(
    model="deepseek-v4.1-flash",
    base_url="https://maas.qianwenaiapi.com/compatible-mode/v1",
    api_key=api_key,
)



llm = init_chat_model(
    "deepseek-v4.1-flash",
    model_provider="openai",
    api_key=api_key,
    base_url="https://maas.qianwenaiapi.com/compatible-mode/v1",
)