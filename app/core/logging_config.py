"""日志配置：统一由独立的日志监听进程落盘。

架构
====
多进程部署（gunicorn，推荐）
----------------------------
    主进程（gunicorn master）
      ├─ multiprocessing.Queue        ← 日志记录的跨进程通道
      ├─ 日志监听进程（listener）      ← 全局唯一写盘者
      └─ worker × N
            └─ logger.info() → QueueHandler → 队列

    worker 不接触任何日志文件；所有记录经队列交给监听进程统一落盘 / 轮转。
    主进程在 fork worker 之前调用 ``bootstrap_logging()``
    （gunicorn 的 ``when_ready`` 钩子正好满足这个时机）。

单进程部署（``uvicorn --workers 1``）
-------------------------------------
    没有"主进程"，退化为进程内 ``QueueListener`` 线程：
    ``logger.info() → QueueHandler → 队列 → 监听线程 → handler``。
    仍然只有一个写入者，不存在多进程竞争。

    ⚠️ 该模式**只支持单进程**。多 worker 必须走 gunicorn（见
    ``gunicorn.conf.py``）—— ``uvicorn --workers N`` 用 spawn 启动 worker，
    拿不到共享队列，会退化成每个 worker 各写各的日志文件并互相覆盖。

记录的格式化（字符串拼接）在 worker 侧完成，真正的 IO 在监听进程/线程，
因此业务线程（含 asyncio 事件循环）不会被日志 IO 阻塞。
"""
from __future__ import annotations

import atexit
import logging
import logging.handlers
import multiprocessing
import queue
import sys
import threading
from pathlib import Path

from app.core.audit import AUDIT_LOGGER_NAME
from app.core.config import get_settings
from app.core.request_context import get_request_id

settings = get_settings()
NODE_LOGGER_NAME = settings.NODE_LOGGER_NAME

# 日志目录：**不在导入期创建**，避免「import 一个模块就产生文件系统副作用」。
# 真正的 mkdir 发生在 _build_handlers()，即只有确实要写日志文件时才建目录。
LOG_DIR = Path("logs")

"""
字段	含义
%(asctime)s	时间
%(levelname)-8s	日志级别
%(name)s	模块名
%(funcName)s	函数名
%(lineno)d	行号
%(threadName)s	线程名
%(request_id)s	请求 ID（由 request_context 注入，非请求上下文时为 "-"）
%(message)s	日志内容
"""
LOG_FORMAT = (
    "%(asctime)s | %(levelname)-8s | %(name)s:%(funcName)s:%(lineno)d | "
    "%(threadName)s | req=%(request_id)s | %(message)s"
)
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# 进程内队列（单进程回退模式）
_LOG_QUEUE = queue.Queue(-1)
_listener: logging.handlers.QueueListener | None = None
_listener_lock = threading.Lock()

# 跨进程共享队列（多进程模式）：由主进程 bootstrap_logging() 创建，
# fork 出的 worker 直接继承这份引用。
_SHARED_QUEUE: multiprocessing.Queue | None = None
_LISTENER_PROCESS: multiprocessing.Process | None = None

_record_factory_installed = False


def _install_record_factory() -> None:
    """给每条 LogRecord 挂上 ``request_id``。

    必须在**产生记录的进程**里安装：多进程模式下记录会被序列化后送给监听
    进程，监听进程的 contextvar 与 worker 无关，所以只能在 worker 侧取值。
    """
    global _record_factory_installed
    if _record_factory_installed:
        return

    base_factory = logging.getLogRecordFactory()

    def _factory(*args, **kwargs):
        record = base_factory(*args, **kwargs)
        record.request_id = get_request_id()
        return record

    logging.setLogRecordFactory(_factory)
    _record_factory_installed = True


class _RequestIdFilter(logging.Filter):
    """兜底：确保记录一定带 ``request_id`` 属性，避免格式化时 KeyError。"""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = "-"
        return True


class _LoggerNameFilter(logging.Filter):
    """按 logger 名前缀决定记录是否交给某个 handler。

    用于保持原有分流：``workflow.nodes`` 的记录只写 nodes.log，
    ``audit`` 的记录只写 audit.log，其余记录写 console / workflow.log / error.log。
    """

    def __init__(self, prefix: str, include: bool):
        super().__init__()
        self._prefix = prefix
        self._include = include

    def filter(self, record: logging.LogRecord) -> bool:
        hit = record.name.startswith(self._prefix)
        return hit if self._include else not hit


def _build_handlers(
    formatter: logging.Formatter,
    *,
    log_dir: Path | None = None,
) -> list[logging.Handler]:
    """构建所有真实 handler（标准库实现）。
    log_dir 显式传入，避免监听进程因重新导入模块而丢失主进程的目录设置。
    """
    base = Path(log_dir) if log_dir is not None else LOG_DIR
    base.mkdir(parents=True, exist_ok=True)  # 只在真正要写文件时才建目录
    timed_cls = logging.handlers.TimedRotatingFileHandler
    size_cls = logging.handlers.RotatingFileHandler

    # 业务日志 handler 的公共过滤器：带 request_id + 排除审计记录
    def _business(handler: logging.Handler) -> logging.Handler:
        handler.addFilter(_RequestIdFilter())
        handler.addFilter(_LoggerNameFilter(AUDIT_LOGGER_NAME, include=False))
        return handler

    # ---- 控制台 ----
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(formatter)
    console.addFilter(_LoggerNameFilter(NODE_LOGGER_NAME, include=False))
    _business(console)

    # ---- 全量日志（按天轮转，保留 30 天）----
    app_file = timed_cls(
        filename=base / "workflow.log",
        when="midnight",
        interval=1,
        backupCount=30,
        encoding="utf-8",
        utc=False,
    )
    app_file.setLevel(logging.DEBUG)
    app_file.setFormatter(formatter)
    app_file.suffix = "%Y-%m-%d"
    app_file.addFilter(_LoggerNameFilter(NODE_LOGGER_NAME, include=False))
    _business(app_file)

    # ---- 错误日志单独文件（按大小轮转，保留 90 天）----
    error_file = size_cls(
        filename=base / "error.log",
        maxBytes=50 * 1024 * 1024,  # 50MB
        backupCount=90,
        encoding="utf-8",
    )
    error_file.setLevel(logging.ERROR)
    error_file.setFormatter(formatter)
    error_file.addFilter(_LoggerNameFilter(NODE_LOGGER_NAME, include=False))
    _business(error_file)

    # ---- 节点执行专用日志（只写 nodes.log）----
    node_file = timed_cls(
        filename=base / "nodes.log",
        when="midnight",
        interval=1,
        backupCount=60,
        encoding="utf-8",
    )
    node_file.setLevel(logging.INFO)
    node_file.setFormatter(formatter)
    node_file.suffix = "%Y-%m-%d"
    node_file.addFilter(_LoggerNameFilter(NODE_LOGGER_NAME, include=True))
    _business(node_file)

    # ---- 安全审计日志（只写 audit.log，JSON Lines）----
    audit_file = timed_cls(
        filename=base / "audit.log",
        when="midnight",
        interval=1,
        backupCount=90,
        encoding="utf-8",
    )
    audit_file.setLevel(logging.INFO)
    audit_file.setFormatter(logging.Formatter("%(message)s"))
    audit_file.suffix = "%Y-%m-%d"
    audit_file.addFilter(_RequestIdFilter())
    audit_file.addFilter(_LoggerNameFilter(AUDIT_LOGGER_NAME, include=True))

    return [console, app_file, error_file, node_file, audit_file]


def _detach_handlers(logger: logging.Logger) -> None:
    """移除 logger 上的旧 handler；只关闭文件类 handler（不能关 sys.stdout）。"""
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        try:
            handler.flush()
        except Exception:
            pass
        if isinstance(handler, logging.FileHandler):
            handler.close()


# ==================== 多进程模式：独立日志监听进程 ====================


def logging_listener_main(
    shared_queue: multiprocessing.Queue, level: str = "INFO", log_dir: Path | None = None
) -> None:
    """日志监听进程入口：整个系统里唯一写日志文件的地方。
    """
    logging.getLogger().setLevel(getattr(logging, level.upper(), logging.INFO))
    formatter = logging.Formatter(fmt=LOG_FORMAT, datefmt=DATE_FORMAT)
    handlers = _build_handlers(formatter, log_dir=log_dir)

    try:
        while True:
            try:
                record = shared_queue.get()
            except (EOFError, OSError):
                # 队列所在进程已退出
                break
            if record is None:  # 停止哨兵
                break
            # 注意：Handler.handle() 本身不做 level 检查（那是 Logger 侧
            # callHandlers 的职责），这里必须显式判断，否则 INFO 也会写进
            # error.log。等价于 QueueListener(respect_handler_level=True)。
            for handler in handlers:
                if record.levelno >= handler.level:
                    handler.handle(record)
    finally:
        for handler in handlers:
            try:
                handler.flush()
            except Exception:
                pass
            handler.close()


def bootstrap_logging(
    level: str = "INFO", log_dir: Path | None = None
) -> multiprocessing.Queue:
    """主进程调用：创建 ``multiprocessing.Queue`` 并启动独立的日志监听进程。

    **必须在 fork worker 之前调用**（gunicorn 的 ``when_ready`` 钩子正好
    满足这个时机，见 gunicorn/arbiter.py: ``start()`` 先调 ``when_ready``，
    之后 ``run()`` 才 ``manage_workers()`` → ``os.fork()``）。

    返回的队列会保存为模块全局，fork 出的 worker 直接继承这份引用；
    worker 侧只需 ``setup_logging()`` 即可切换成 ``QueueHandler``。
    """
    global _SHARED_QUEUE, _LISTENER_PROCESS

    if _SHARED_QUEUE is not None:
        return _SHARED_QUEUE

    _install_record_factory()
    shared_queue: multiprocessing.Queue = multiprocessing.Queue()
    listener_process = multiprocessing.Process(
        target=logging_listener_main,
        args=(shared_queue, level, log_dir),
        name="simplejob-logger",
        daemon=True,  # 主进程异常退出时不留孤儿进程
    )
    listener_process.start()

    _SHARED_QUEUE = shared_queue
    _LISTENER_PROCESS = listener_process
    atexit.register(stop_logging_server)
    return shared_queue


def stop_logging_server() -> None:
    """主进程退出时调用：通知监听进程排空队列后退出。"""
    global _SHARED_QUEUE, _LISTENER_PROCESS

    shared_queue, listener_process = _SHARED_QUEUE, _LISTENER_PROCESS
    _SHARED_QUEUE = None
    _LISTENER_PROCESS = None

    if shared_queue is not None:
        try:
            shared_queue.put(None)  # 哨兵
        except Exception:
            pass
    if listener_process is not None and listener_process.is_alive():
        listener_process.join(timeout=5)
        if listener_process.is_alive():
            listener_process.terminate()


def _install_shared_queue_handler(numeric_level: int) -> None:
    """worker 模式：把 root / workflow.nodes 的 handler 替换为 QueueHandler。

    worker 从此不再接触任何日志文件，记录全部经共享队列交给监听进程。
    """
    nodes_logger = logging.getLogger(NODE_LOGGER_NAME)
    _detach_handlers(nodes_logger)
    nodes_logger.setLevel(logging.INFO)
    nodes_logger.propagate = False  # 避免记录被 root 重复入队
    # nodes_logger.propagate = True  # 避免记录被 root 重复入队

    root = logging.getLogger()
    _detach_handlers(root)
    root.setLevel(numeric_level)
    root.addHandler(logging.handlers.QueueHandler(_SHARED_QUEUE))
    nodes_logger.addHandler(logging.handlers.QueueHandler(_SHARED_QUEUE))


# ==================== 单进程回退模式：进程内监听线程 ====================

def _start_inline_listener(level: str) -> None:
    """单进程模式：``QueueHandler`` → 队列 → ``QueueListener`` 线程 → handler。"""
    global _listener

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    _detach_handlers(root)

    nodes_logger = logging.getLogger(NODE_LOGGER_NAME)
    _detach_handlers(nodes_logger)
    nodes_logger.setLevel(logging.INFO)
    nodes_logger.propagate = False

    formatter = logging.Formatter(fmt=LOG_FORMAT, datefmt=DATE_FORMAT)

    _listener = logging.handlers.QueueListener(
        _LOG_QUEUE,
        *_build_handlers(formatter, log_dir=LOG_DIR),
        respect_handler_level=True,
    )
    _listener.start()

    root.addHandler(logging.handlers.QueueHandler(_LOG_QUEUE))
    nodes_logger.addHandler(logging.handlers.QueueHandler(_LOG_QUEUE))

    atexit.register(stop_logging)


def setup_logging(level: str = "INFO"):
    """初始化日志（幂等）。

    多进程模式：主进程已调用 ``bootstrap_logging()``，本进程是 worker，
    只挂 ``QueueHandler`` 把记录丢进共享队列。

    单进程模式：没有主进程，退化为进程内 ``QueueListener`` 线程。
    """
    _install_record_factory()

    if _SHARED_QUEUE is not None:
        with _listener_lock:
            _install_shared_queue_handler(getattr(logging, level.upper(), logging.INFO))
        return None

    with _listener_lock:
        if _listener is not None:
            logging.getLogger().setLevel(getattr(logging, level.upper(), logging.INFO))
            logging.getLogger(NODE_LOGGER_NAME).setLevel(logging.INFO)
            return _listener
        _start_inline_listener(level)
        return _listener


def stop_logging() -> None:
    """清理当前进程的日志 handler。

    多进程模式：本进程是 worker，移除 ``QueueHandler`` 并把队列里的剩余记录
    冲刷出去；监听进程由主进程通过 ``stop_logging_server()`` 关闭。

    单进程模式：停止进程内的监听线程。
    """
    global _listener

    if _SHARED_QUEUE is not None:
        _detach_handlers(logging.getLogger())
        _detach_handlers(logging.getLogger(NODE_LOGGER_NAME))
        try:
            _SHARED_QUEUE.close()
            _SHARED_QUEUE.join_thread()
        except Exception:
            pass
        return

    with _listener_lock:
        if _listener is None:
            return
        _listener.stop()
        _listener = None
