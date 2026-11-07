"""标准库 logging 配置：同时输出到控制台 + 文件（按天轮转 + 分级别）。"""
import logging
import logging.handlers
import sys
from pathlib import Path

LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)


"""
字段	含义
%(asctime)s	时间
%(levelname)-8s	日志级别
%(name)s	模块名
%(funcName)s	函数名
%(lineno)d	行号
%(threadName)s	线程名
%(message)s	日志内容
"""
LOG_FORMAT = (
    "%(asctime)s | %(levelname)-8s | %(name)s:%(funcName)s:%(lineno)d | "
    "%(threadName)s | %(message)s"
)
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(level: str = "INFO"):
    """初始化全局日志配置。
    参数 level：控制日志的最低采集级别，默认 "INFO"。
    类型提示 str：标注参数类型，便于 IDE 静态检查和代码可读性。
    默认值 "INFO"：生产环境通常只记录 INFO 及以上级别，调试时可传入 "DEBUG"。
"""
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # 避免重复添加 handler（热重载 / 多次调用时）
    if root.handlers:
        root.handlers.clear()

    formatter = logging.Formatter(fmt=LOG_FORMAT, datefmt=DATE_FORMAT)

    # ---- 控制台 ----
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(formatter)
    root.addHandler(console)

    # ---- 全量日志（按天轮转，保留 30 天）----
    app_file = logging.handlers.TimedRotatingFileHandler(
        filename=LOG_DIR / "workflow.log",
        when="midnight",
        interval=1,
        backupCount=30,
        encoding="utf-8",
        utc=False,
    )
    app_file.setLevel(logging.DEBUG)
    app_file.setFormatter(formatter)
    app_file.suffix = "%Y-%m-%d"
    root.addHandler(app_file)

    # ---- 错误日志单独文件（按大小轮转，保留 90 天）----
    error_file = logging.handlers.RotatingFileHandler(
        filename=LOG_DIR / "error.log",
        maxBytes=50 * 1024 * 1024,  # 50MB
        backupCount=90,
        encoding="utf-8",
    )
    error_file.setLevel(logging.ERROR)
    error_file.setFormatter(formatter)
    root.addHandler(error_file)

    # ---- 节点执行专用日志（独立 logger，只写 nodes.log）----
    nodes_logger = logging.getLogger("workflow.nodes")
    nodes_logger.setLevel(logging.INFO)
    nodes_logger.propagate = False  # 不再向 root 传播，避免重复
    node_file = logging.handlers.TimedRotatingFileHandler(
        filename=LOG_DIR / "nodes.log",
        when="midnight",
        interval=1,
        backupCount=60,
        encoding="utf-8",
    )
    node_file.setLevel(logging.INFO)
    node_file.setFormatter(formatter)
    node_file.suffix = "%Y-%m-%d"
    nodes_logger.addHandler(node_file)
