"""LangGraph AsyncPostgresSaver 检查点的创建与生命周期管理。

设计说明
--------
检查点存储是 **进程级单例**：应用启动时建立一次连接池并 ``setup()``，
整个进程内的所有请求 / 后台任务 / 邮件机器人共用同一实例。

为什么用连接池而不是单连接：
psycopg 的 ``AsyncConnection`` **不是协程安全的**（同一时刻只能执行一个操作）。
虽然 ``AsyncPostgresSaver`` 内部用 ``asyncio.Lock`` 串行化了所有 DB 操作
（见 ``_cursor()`` 中的 ``async with self.lock``），单连接也不会出错，
但连接池能带来：

* **连接健康检查 / 自动重连**：单连接断开后不会自愈，池会替换坏连接；
* **并发扩展能力**：若未来 LangGraph 弱化锁，池可支撑真并发；
* **资源可控**：``min_size`` / ``max_size`` 限制同时占用的连接数。

注意：``AsyncPostgresSaver`` 实例仍然持有 ``asyncio.Lock``，
所以当前 checkpoint 读写实际仍是串行的；池主要提升的是连接可靠性与可扩展性。
"""
import asyncio

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

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

_pool: AsyncConnectionPool | None = None
_saver: AsyncPostgresSaver | None = None
_init_lock = asyncio.Lock()


def _build_serde() -> JsonPlusSerializer:
    """按当前配置构造序列化器（注册所有业务类型避免反序列化警告）。"""
    return JsonPlusSerializer(
        allowed_msgpack_modules=_ALLOWED_MSGPACK_MODULES,
    )


def _build_pool(conninfo: str) -> AsyncConnectionPool:
    """构造检查点专用连接池（延迟打开，由 init 显式 open）。"""
    settings = get_settings()
    return AsyncConnectionPool(
        conninfo=conninfo,
        min_size=settings.CHECKPOINT_DB_MIN_SIZE,
        max_size=settings.CHECKPOINT_DB_MAX_SIZE,
        kwargs={
            "autocommit": True,
            "prepare_threshold": 0,
            "row_factory": dict_row,
        },
        open=False,
    )


async def init_checkpointer() -> AsyncPostgresSaver:
    """进程启动时调用一次，建立全局检查点存储。幂等。

    必须在事件循环内调用（FastAPI lifespan）。
    """
    global _pool, _saver

    if _saver is not None:
        return _saver

    async with _init_lock:
        settings = get_settings()
        pg_url = settings.database_url.replace("+psycopg", "")

        pool = _build_pool(pg_url)
        await pool.open()
        saver = AsyncPostgresSaver(conn=pool, serde=_build_serde())
        await saver.setup()

        _pool = pool
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
    """进程退出时调用：关闭连接池。幂等。"""
    global _pool, _saver

    _saver = None
    pool = _pool
    _pool = None

    if pool is not None:
        try:
            await pool.close()
        except Exception:  # noqa: BLE001 - 退出阶段兜底，不能因清理失败阻断关停
            pass
