"""gunicorn 配置：多进程部署时由主进程（master）统一接管日志。

用法：
    gunicorn app.main:app -c gunicorn.conf.py


"""
from sqlalchemy.dialects.mysql import limit

worker_class = "uvicorn_worker.UvicornWorker"
bind = "127.0.0.1:8000"

# 受邮件机器人约束，默认单 worker；拆分邮件机器人后可按需调大
workers = 1

# 不需要 preload：when_ready 会在 master 里 import 日志模块，
# worker fork 后通过 sys.modules 继承同一份模块状态（含共享队列引用）。
preload_app = False

timeout = 60
graceful_timeout = 60
keepalive = 5

accesslog = "/home/hrsystem/hr-backend/log/access.log"
errorlog = "/home/hrsystem/hr-backend/log/error.log"
loglevel = "info"

limit_request_line = 4094
limit_request_field_size = 8190
limit_request_file = 100


def when_ready(server):
    """master 进程在 fork worker 之前执行：建立共享日志队列 + 日志监听进程。"""
    from app.core.logging_config import bootstrap_logging

    bootstrap_logging()


def on_exit(server):
    """master 进程退出：让监听进程排空队列后退出。"""
    from app.core.logging_config import stop_logging_server

    stop_logging_server()
