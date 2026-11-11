"""LangGraph AsyncPostgresSaver 检查点的创建与生命周期管理。

设计说明
--------
检查点存储是 **进程级单例**：应用启动时建立一次连接并 ``setup()``，
整个进程内的所有请求 / 后台任务 / 邮件机器人共用同一实例。

为什么不是「每次调用建一个」：
``AsyncPostgresSaver.from_conn_string`` 是一个 ``asynccontextmanager``，
每调用一次就会新建一条 Postgres 连接，并且 ``setup()`` 会再执行一遍
建表 / 迁移 DDL。原实现每次 ``ainvoke`` 都进出一次上下文，连接反复
创建销毁，既浪费又放大了数据库压力。

单例后：每进程恒定 1 条连接，``setup()`` 只执行一次。
"""
import asyncio

from langgraph.checkpoint.postgres.aio import AsyncConnection, AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from psycopg.rows import dict_row

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

_connection: AsyncConnection | None = None
_saver: AsyncPostgresSaver | None = None
_init_lock = asyncio.Lock()


def _build_serde() -> JsonPlusSerializer:
    """按当前配置构造序列化器（注册所有业务类型避免反序列化警告）。"""
    return JsonPlusSerializer(
        allowed_msgpack_modules=_ALLOWED_MSGPACK_MODULES,
    )


async def init_checkpointer() -> AsyncPostgresSaver:
    """进程启动时调用一次，建立全局检查点存储。幂等。

    必须在事件循环内调用（FastAPI lifespan）。
    """
    global _connection, _saver

    if _saver is not None:
        return _saver

    async with _init_lock:
        if _saver is None:
            settings = get_settings()
            pg_url = settings.database_url.replace("+psycopg", "")

            conn = await AsyncConnection.connect(
                pg_url, autocommit=True, prepare_threshold=0, row_factory=dict_row
            )
            saver = AsyncPostgresSaver(conn=conn, serde=_build_serde())
            await saver.setup()

            _connection = conn
            _saver = saver

    return _saver


def get_checkpointer() -> AsyncPostgresSaver:
    """返回已初始化的检查点存储。

    Raises:
        RuntimeError: 未在应用 lifespan 中调用 ``init_checkpointer()``。
    """
    if _saver is None:
        raise RuntimeError(
            "checkpointer 未初始化：请在应用启动（lifespan）时调用 init_checkpointer()"
        )
    return _saver


async def close_checkpointer() -> None:
    """进程退出时调用：释放底层连接。幂等。"""
    global _connection, _saver

    _saver = None
    conn = _connection
    _connection = None

    if conn is not None:
        try:
            await conn.close()
        except Exception:  # noqa: BLE001 - 退出阶段兜底，不能因清理失败阻断关停
            pass
