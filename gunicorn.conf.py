"""gunicorn 配置：多进程部署时由主进程（master）统一接管日志。

用法：
    gunicorn app.main:app -c gunicorn.conf.py

时序是关键
----------
gunicorn 的 master 先执行 ``when_ready()``，之后才 fork worker
（gunicorn/arbiter.py：``Arbiter.start()`` 里调 ``cfg.when_ready()``，
随后 ``Arbiter.run()`` 才 ``manage_workers()`` → ``os.fork()``）。
所以在 ``when_ready`` 里创建的 ``multiprocessing.Queue`` 与日志监听进程
会被所有 worker 继承 —— 这正是"主进程建队列、worker 只入队"的实现基础。

注意事项
--------
1. 使用独立的 ``uvicorn-worker`` 包（``uvicorn_worker.UvicornWorker``）。
   uvicorn 自带的 ``uvicorn.workers`` 模块已被官方标记为 deprecated，
   且它同样依赖 gunicorn，没有理由继续用。
2. 多进程部署必须走 gunicorn。不要直接用 ``uvicorn --workers N`` ——
   uvicorn 用 **spawn** 启动 worker（见 uvicorn/_subprocess.py：
   ``multiprocessing.get_context("spawn")``），worker 拿不到共享队列，
   会退化成每个 worker 各写各的日志文件。
3. worker 数量仍受**邮件机器人**约束（见 docs/deployment.md 0.3 第 4 条）：
   ``MailManager`` 在 FastAPI lifespan 中初始化，多进程会启动多套 IMAP
   长连接 + APScheduler，造成重复拉信。在把邮件机器人拆成独立服务之前，
   ``workers`` 保持 1。日志本身已经不再是多进程的障碍。
"""

worker_class = "uvicorn_worker.UvicornWorker"
bind = "127.0.0.1:8000"

# 受邮件机器人约束，默认单 worker；拆分邮件机器人后可按需调大
workers = 1

# 不需要 preload：when_ready 会在 master 里 import 日志模块，
# worker fork 后通过 sys.modules 继承同一份模块状态（含共享队列引用）。
preload_app = False


def when_ready(server):
    """master 进程在 fork worker 之前执行：建立共享日志队列 + 日志监听进程。"""
    from app.core.logging_config import bootstrap_logging

    bootstrap_logging()


def on_exit(server):
    """master 进程退出：让监听进程排空队列后退出。"""
    from app.core.logging_config import stop_logging_server

    stop_logging_server()
