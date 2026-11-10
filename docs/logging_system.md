# 日志系统：独立监听进程统一落盘 技术文档

> **变更范围**：`app/core/logging_config.py`、`app/main.py`、`gunicorn.conf.py`
> **目标**：
> 1. 业务线程 / asyncio 事件循环不被日志 IO 阻塞
> 2. **worker 不再直接写日志文件** —— 多进程部署下不会互相覆盖、丢日志
> 3. 为后续并行扩容铺路

---

## 1. 背景与问题

### 1.1 原方案

每个进程（worker）各自在 root logger 上挂载同步 handler，直接写同一批文件：

| logger | handler | sink | 轮转 |
|---|---|---|---|
| root | `StreamHandler` | stdout | — |
| root | `TimedRotatingFileHandler` | `logs/workflow.log` | 按天，保留 30 |
| root | `RotatingFileHandler` | `logs/error.log` | 50MB，保留 90 |
| `workflow.nodes`（`propagate=False`） | `TimedRotatingFileHandler` | `logs/nodes.log` | 按天，保留 60 |

### 1.2 问题一：阻塞调用线程

`logging.Handler.handle()` 先取 handler 锁再 `emit()`，全程在调用线程内完成：

| # | 阻塞源 | 后果 |
|---|---|---|
| 1 | 同步写 stdout / 文件 | 磁盘 IO 进入调用路径 |
| 2 | 管道写满 | `write()` 无限期阻塞 |
| 3 | 轮转 | 重命名 / 删备份都在调用线程 |
| 4 | handler 锁竞争 | 多线程互相串行等待 |
| 5 | 异步链路阻塞 | 卡住整个 event loop |

### 1.3 问题二：多进程轮转竞争（实测复现）

`os.rename` + `os.remove` 没有跨进程协调，多 worker 同时轮转时：

- Windows：`PermissionError: [WinError 32] 另一个程序正在使用此文件`，该条记录**丢失**
- POSIX：一方已轮转、另一方仍持旧 inode 句柄 → 后续日志一直写进昨天那个文件
- 备份链 `.1 → .2 → .3` 搬移被并发踩踏 → 备份缺失、内容错位

---

## 2. 目标架构

```
┌──────────────────────── 主进程（gunicorn master）────────────────────────┐
│                                                                          │
│   bootstrap_logging()                                                    │
│     ├─ multiprocessing.Queue  ← 日志记录的跨进程通道（唯一通道）          │
│     └─ 日志监听进程（simplejob-logger, daemon）                           │
│            └─ 全局唯一写盘者：4 个 handler（stdout + 3 个文件）           │
│                                                                          │
│   fork ─────────────────────────────┬──────────────┬──────────────┐      │
│                                     ↓              ↓              ↓      │
│   worker 1                        worker 2      worker 3      worker N    │
│     logger.info()                   ...           ...           ...      │
│       → QueueHandler                  → 全部丢进同一个 multiprocessing.Queue
│       （不接触任何日志文件）                                              │
└──────────────────────────────────────────────────────────────────────────┘
```

**核心不变量：整个系统里只有一个进程打开日志文件进行写操作。**

---

## 3. 两种运行模式

| 模式 | 触发条件 | 队列 | 写盘者 | 文件 handler |
|---|---|---|---|---|
| **多进程模式** | 主进程调用过 `bootstrap_logging()`（gunicorn `when_ready`） | `multiprocessing.Queue` | 独立日志监听**进程** | 标准库 handler |
| **单进程回退** | 没有共享队列（如 `uvicorn --workers 1`） | 进程内 `queue.Queue` | 进程内 `QueueListener` **线程** | 标准库 handler |

### 3.1 多进程模式（生产推荐，gunicorn）

```python
# gunicorn.conf.py
def when_ready(server):          # master 里执行，早于 fork worker
    bootstrap_logging()

def on_exit(server):
    stop_logging_server()
```

```python
# app/core/logging_config.py —— worker 侧只需一句
setup_logging("INFO")            # 检测到共享队列 → 挂 QueueHandler
```

worker 的 `logger.info()` 只做一次 `QueueHandler.emit()` → `queue.put_nowait()`，
**不写盘、不加文件锁、不碰轮转**。

### 3.2 单进程回退（`uvicorn --workers 1`）

没有"主进程"，退化为进程内监听线程：

```
logger.info() → QueueHandler → queue.Queue → QueueListener 线程 → handler
```

仍然只有一个写入者。

> ⚠️ 该模式**只支持单进程**。多 worker 必须走 gunicorn —— `uvicorn --workers N`
> 用 spawn 启动 worker，拿不到共享队列，会退化成每个 worker 各写各的日志文件
> 并互相覆盖。这也是"多 worker 不被推荐"的另一个理由。

---

## 4. 实现细节

### 4.1 全局状态

```python
_SHARED_QUEUE: multiprocessing.Queue | None = None      # 多进程模式的共享队列
_LISTENER_PROCESS: multiprocessing.Process | None = None
_LOG_QUEUE = queue.Queue(-1)                            # 单进程回退的进程内队列
_listener: logging.handlers.QueueListener | None = None
```

### 4.2 日志监听进程

`logging_listener_main()`（[logging_config.py:168](app/core/logging_config.py#L168)）
是监听进程的入口，也是**唯一写盘的地方**：

```python
def logging_listener_main(shared_queue, level="INFO", log_dir=None):
    handlers = _build_handlers(formatter, process_safe=False, log_dir=log_dir)
    while True:
        record = shared_queue.get()
        if record is None:          # 停止哨兵
            break
        for handler in handlers:
            if record.levelno >= handler.level:   # 必须显式判断，见 4.6
                handler.handle(record)
```

用标准库 handler 即可 —— 既然只有一个写入者，就不需要任何跨进程文件锁
（这正是"单写入者"架构替代 `concurrent-log-handler` 的原因）。

### 4.3 主进程 bootstrap

`bootstrap_logging()`（[logging_config.py:201](app/core/logging_config.py#L201)）：

1. `multiprocessing.Queue()`
2. `multiprocessing.Process(target=logging_listener_main, daemon=True).start()`
3. 保存到模块全局 `_SHARED_QUEUE`，供 fork 出的 worker 继承
4. `atexit.register(stop_logging_server)` 兜底

`stop_logging_server()`（[logging_config.py:233](app/core/logging_config.py#L233)）
向队列投递 `None` 哨兵，监听进程排空后退出；超时则 `terminate()`。

### 4.4 gunicorn 时序（为什么必须在 when_ready）

gunicorn/arbiter.py 的执行顺序：

```
Arbiter.run()
  └─ Arbiter.start()
       ├─ cfg.on_starting(server)
       ├─ cfg.when_ready(server)      ← 在这里 bootstrap（arbiter.py:224）
       └─ ...
  └─ manage_workers()                 ← 这里才 fork worker（arbiter.py:248）
       └─ spawn_worker() → os.fork()  （arbiter.py:734）
```

`when_ready` 在 fork 之前执行，因此主进程创建的队列与监听进程会被所有
worker 继承（`sys.modules` 里的 `_SHARED_QUEUE` 也一并继承）。

### 4.5 为什么不能用 `uvicorn --workers N`

`uvicorn/_subprocess.py` 里写死了：

```python
spawn = multiprocessing.get_context("spawn")
```

spawn 启动的 worker 是全新的 Python 解释器，**无法继承**主进程的
`multiprocessing.Queue`，uvicorn 也没有提供把自定义对象传进 worker 的入口。
所以多进程部署必须走 gunicorn（fork），否则会退化成 3.2 的回退模式。

### 4.6 一个容易踩的坑：handler level 检查

`Handler.handle()` **本身不做 level 检查** —— level 过滤是
`Logger.callHandlers()` 的职责（`if record.levelno >= hdlr.level`）。
监听进程绕过了 Logger 直接调用 `handler.handle(record)`，必须自己判断，
否则 INFO 记录也会写进 `error.log`。实现里已显式加上该判断。

### 4.7 分流

`_LoggerNameFilter`（[logging_config.py:77](app/core/logging_config.py#L77)）
按 `record.name` 前缀过滤，保持原有路由：

| record.name | stdout | workflow.log | error.log | nodes.log |
|---|:---:|:---:|:---:|:---:|
| `workflow.nodes` | ✗ | ✗ | ✗ | ✓ |
| 其他 | ✓ | ✓ | ✓（仅 ERROR+） | ✗ |

worker 侧 `workflow.nodes` 设为 `propagate=False` 并单独挂 `QueueHandler`，
避免记录被 root 重复入队。

> **节点 logger 名可配置**：名字来自 `Settings.NODE_LOGGER_NAME`
> （默认 `"workflow.nodes"`，可用环境变量 `NODE_LOGGER_NAME` 覆盖）。
> 节点模块与 `logging_config` 的过滤器**都从 settings 读取同一个值**
> （[`logging_config.py:45`](app/core/logging_config.py#L45)），
> 因此改名后分流依然一致 —— 改一处即可，不会出现「logger 叫 A、过滤器按 B 匹配」
> 的错配。

### 4.8 生命周期

```python
# app/main.py
@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.core.logging_config import setup_logging, stop_logging
    setup_logging(level="INFO")     # worker：挂 QueueHandler；单进程：起监听线程
    ...
    yield
    ...
    stop_logging()                  # worker：摘掉 QueueHandler 并冲刷队列
```

顺序上 `stop_logging()` 放在其他清理之后，确保关闭阶段的日志也能落盘。

---

## 5. 语义保证

| 语义 | 说明 |
|---|---|
| **唯一写盘者** | 多进程模式下只有监听进程打开日志文件；单进程模式下只有监听线程 |
| **worker 零文件接触** | worker 侧只有 `QueueHandler`，不创建任何 FileHandler |
| **traceback 不丢** | `QueueHandler.prepare()` 在 worker 侧把异常文本并入 `record.msg`，跨进程序列化后仍完整 |
| **level 过滤不变** | 监听进程显式比较 `record.levelno >= handler.level` |
| **同进程顺序性** | 单队列 + 单消费者 → 顺序与调用顺序一致；跨进程只能保证不丢不重 |
| **worker 崩溃不影响监听进程** | 监听进程由 master 管理，worker 挂了日志链路不断 |
| **主进程崩溃不留孤儿** | 监听进程是 daemon，随主进程退出 |
| **fork 安全** | 队列在 fork 之前创建；worker 继承同一份引用 |
| **`%(threadName)s`** | 记录的是调用方线程（创建时快照），用于定位"谁打了日志" |

---

## 6. 验证

### 6.1 多进程：4 个真实进程 + 独立监听进程

主进程 `bootstrap_logging()` 创建队列与监听进程，4 个 spawn 进程各自
`setup_logging()` 后写 INFO / ERROR / 节点日志（模拟 gunicorn worker）：

| # | 检查项 | 结果 |
|---|---|---|
| 1 | INFO 200/200 全部落盘、无缺失、无重复 | PASS |
| 2 | ERROR 8/8 全部落盘 | PASS |
| 3 | traceback 完整写入 | PASS |
| 4 | 节点日志 4/4，未泄漏到 workflow.log | PASS |
| 5 | INFO 未泄漏到 error.log | PASS |
| 6 | ERROR 未泄漏到 nodes.log | PASS |
| 7 | 无 `Logging error`、无进程崩溃 | PASS |
| 8 | 监听进程是独立进程（pid ≠ 主进程） | PASS |

### 6.2 单进程回退回归

| # | 检查项 | 结果 |
|---|---|---|
| 1 | 三文件收到记录 + traceback | PASS |
| 2 | 分流正确（nodes / workflow / error 互不泄漏） | PASS |
| 3 | 调用方 200 次日志耗时 | **0.0022 s**（同步 handler 为 0.41 s） |
| 4 | `stop_logging()` 排空队列 | 0.469 s，202 条全部落盘 |

---

## 7. 影响与权衡

| 项 | 说明 |
|---|---|
| **进程数** | 多进程模式比原来多一个常驻监听进程（约几 MB 内存） |
| **日志延迟** | 记录经队列异步落盘，比直接写文件多几毫秒延迟；崩溃前未落盘的记录会丢 |
| **硬杀丢日志** | `kill -9` 主进程时队列里未消费完的记录丢失；`atexit` / `on_exit` 覆盖正常退出 |
| **队列冲刷** | worker 退出前 `stop_logging()` 会 `close()` + `join_thread()` 冲刷自己的 feeder 线程 |
| **部署约束** | 多 worker 必须 `gunicorn -c gunicorn.conf.py`；`uvicorn --workers N` 不支持 |
| **业务约束仍在** | 多 worker 的真正障碍是**邮件机器人**（`MailManager` 在 lifespan 初始化，多进程会重复拉信）和 `mailbot_state.json` 游标 —— 与日志无关，见 [`deployment.md`](deployment.md) 0.3 第 4 条 |

### 可选替代：只写 stdout

如果部署环境有日志采集（systemd journal / docker logs / Loki），可以让监听
进程只保留 `StreamHandler`，彻底不写文件。这是容器化最佳实践。

---

## 8. FAQ

**Q1：worker 里还有日志文件 handler 吗？**
没有。多进程模式下 worker 的 root / `workflow.nodes` 上只挂 `QueueHandler`。

**Q2：为什么非要 gunicorn？uvicorn `--workers` 不行吗？**
uvicorn 用 spawn 启动 worker，`multiprocessing.Queue` 传不过去。gunicorn 用
fork，队列随 fork 继承。详见 4.5。

**Q3：监听进程挂了对业务有什么影响？**
记录积压在队列里（内存增长），worker 侧仍不阻塞。应监控队列长度或监听进程
存活状态。

**Q4：主进程还没调用 `bootstrap_logging()` 就打日志怎么办？**
`setup_logging()` 检测不到共享队列会退化为单进程模式（起线程监听）。所以
务必在 gunicorn `when_ready` 里调用，且不要在 app 导入期打日志。

**Q5：`%(threadName)s` 显示的还是 MainThread？**
记录在调用线程创建时快照线程名，写入发生在监听进程。要确认"谁在写"需插桩。

**Q6：如何确认当前是哪种模式？**
`python -c "from app.core.logging_config import _SHARED_QUEUE; print(_SHARED_QUEUE is not None)"`，
或直接看进程列表里有没有 `simplejob-logger`。

**Q7：多进程下日志顺序还有意义吗？**
单进程内有序；跨进程只能保证不丢不重。需要全局时序时建议在格式里加
`%(process)d`。

---

## 9. 变更清单

| 文件 | 变更 |
|---|---|
| `app/core/logging_config.py` | 新增多进程模式：`bootstrap_logging()` / `logging_listener_main()` / `stop_logging_server()` / `_install_shared_queue_handler()`；`setup_logging()` 按模式自动选择；单进程回退保留 |
| `app/main.py` | lifespan 中接入 `stop_logging()`（[main.py:33-48](app/main.py#L33-L48)） |
| `gunicorn.conf.py` | 新增：`when_ready` 里 bootstrap，`on_exit` 里关闭监听进程 |
| `pyproject.toml` / `uv.lock` | 新增 `langgraph-checkpoint-postgres>=3.2.0`（`app/core/checkpointer.py` 直接 import，此前未声明，`uv sync` 后暴露） |
