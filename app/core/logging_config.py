"""标准库 logging 配置：同时输出到控制台 + 文件（按天轮转 + 分级别）。"""
import logging
import logging.handlers
import sys
from pathlib import Path

LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)

LOG_FORMAT = (
    "%(asctime)s | %(levelname)-8s | %(name)s:%(funcName)s:%(lineno)d | "
    "%(threadName)s | %(message)s"
)
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(level: str = "INFO"):
    """初始化全局日志配置。"""
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

    # ---- 节点执行专用日志（只记 INFO 以上）----
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
    root.addHandler(node_file)
