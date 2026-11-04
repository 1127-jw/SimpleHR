"""LangGraph AsyncPostgresSaver 检查点的创建与生命周期管理。"""
from contextlib import asynccontextmanager

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

from app.core.config import get_settings

# 允许在 checkpoint 中序列化/反序列化的类型
# 这些类型会被 LangGraph 的 msgpack 序列化器用于状态持久化
_ALLOWED_MSGPACK_MODULES = [
    ("app.models.position", "EducationEnum"),
    ("app.models.user", "UserStatus"),
    ("app.models.candidate", "GenderEnum"),
    ("app.models.candidate", "CandidateStatusEnum"),
    ("app.models.InterviewModel", "InterviewResultEnum"),
    ("app.schemas.position_schema", "PositionSchema"),
    ("app.schemas.candidate_schema", "CandidateSchema"),
    ("app.schemas.user_schema", "UserSH"),
    ("app.schemas.agent_schema", "AgentCandidateScoreSchema"),
]


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

    # 使用自定义 serde，注册所有业务类型避免反序列化警告
    serde = JsonPlusSerializer(
        allowed_msgpack_modules=_ALLOWED_MSGPACK_MODULES,
    )

    async with AsyncPostgresSaver.from_conn_string(pg_url, serde=serde) as checkpointer:
        await checkpointer.setup()
        yield checkpointer
