"""LangGraph AsyncPostgresSaver 检查点的创建与生命周期管理。"""
from contextlib import asynccontextmanager
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from app.core.config import get_settings


@asynccontextmanager
async def get_checkpointer():
    """创建并管理 AsyncPostgresSaver 的生命周期。

    Yields:
        AsyncPostgresSaver: 已 setup 的检查点实例。

    用法:
        async with get_checkpointer() as checkpointer:
            app = build_candidate_graph(checkpointer)
            ...
    """
    settings = get_settings()
    pg_url = settings.database_url.replace("+psycopg", "")
    async with AsyncPostgresSaver.from_conn_string(pg_url) as checkpointer:
        await checkpointer.setup()
        yield checkpointer