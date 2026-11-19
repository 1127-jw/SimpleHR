# 监控 / 告警 / 健康检查 —— 具体实施方案

> 适用范围：`simplejob`（FastAPI + gunicorn 单 worker + LangGraph + PostgreSQL + Redis + 邮件机器人 + OCR/AI 外部调用）。
>
> 本文只给出「要新增什么、加在哪、怎么配」，**不改动现有业务代码**。所有代码块都是待新增文件 / 待插入片段，落地时按需复制。
>
> 与 `docs/ops_plan.md` §4 的关系：本文是它的**可执行展开版**（补全代码、配置、告警规则、看板与验收）。
>
> **配套入门教程**：[总览篇](./监控告警教程/00-监控告警健康检查-总览.md) —— 0 基础版，讲清楚「为什么要这么做」，含 5 张图解与 PromQL / LogQL 速查。

---

## 0. 方案总览

### 0.1 总体架构

```
                    ┌────────────────────── 采集层 ──────────────────────┐
  FastAPI ──/metrics──► Prometheus ──► Alertmanager ──► 钉钉 / 企微 / 电话 / 邮件
  日志文件 ──Promtail──► Loki      ──► Grafana（看板 + 日志检索）
  Trace    ──OTel─────► Tempo/Jaeger ──► Grafana（链路下钻）
  合成拨测 ──Cron──────► Pushgateway ──► Prometheus
                    └───────────────────────────────────────────────────┘
```

### 0.2 组件清单

| 层 | 组件 | 作用 | 本项目中的采集对象 |
|---|---|---|---|
| 健康检查 | FastAPI 路由 + Docker/K8s 探针 | 进程/启动/依赖三级探活 | `/healthz` `/startupz` `/readyz` |
| 指标 | Prometheus + Exporter | RED、依赖、业务指标 | `/metrics`、PG/Redis/Node/Blackbox exporter |
| 日志 | Loki + Promtail | 日志集中、检索、日志告警 | `logs/workflow.log`、`error.log`、`nodes.log`、`audit.log` |
| 追踪 | OpenTelemetry + Tempo | 请求链路、节点耗时 | FastAPI / SQLAlchemy / Redis / httpx / LangGraph 节点 |
| 告警 | Alertmanager | 分级、路由、抑制、静默 | 见 §5.2 规则表 |
| 看板 | Grafana | 总览/依赖/业务/后台任务 | 见 §7 |
| 合成监控 | Cron + Pushgateway | 外部视角拨测 | 复用 `scripts/ci/smoke.py` |

### 0.3 与现有能力的复用

| 现有能力 | 复用方式 |
|---|---|
| `request_id`（`app/core/request_context.py` + 中间件） | 作为日志、指标、追踪的关联键 |
| `logs/audit.log`（JSON Lines） | 安全告警与合规审计数据源（登录、简历下载、删除） |
| `logs/error.log` | 日志告警规则落点（ERROR 突增） |
| `gunicorn` 单 worker（`gunicorn.conf.py`） | 探针与告警按「单点」设计，后台任务与 API 同进程 |
| `TimeoutSweepManager` / `MailManager` | 新增心跳指标（§2.5），避免「进程活着但任务死了」的静默故障 |
| CI `scripts/ci/smoke.py` | 改造为定时合成监控（§9） |

---

## 1. 健康检查

### 1.1 三个端点的语义

| 端点 | 语义 | 检查内容 | 失败处置 |
|---|---|---|---|
| `GET /healthz` | 存活（liveness） | 进程在、事件循环未卡死 | 重启容器/进程 |
| `GET /startupz` | 启动完成（startup） | `lifespan` 中 Redis/checkpointer/MailManager/巡检初始化完成 | 就绪前不接流量 |
| `GET /readyz` | 就绪（readiness） | PostgreSQL、Redis（结果缓存 5s） | 从负载均衡摘除，**不重启** |

> **为什么必须分开**：用 readiness 做 liveness，DB 抖动会导致容器被反复重启，放大故障。

### 1.2 新增文件：`app/routers/health_router.py`

```python
"""健康检查路由：liveness / startup / readiness。

- /healthz   只证明进程活着（不查依赖）       → liveness 探针
- /startupz  应用初始化完成                    → startup 探针
- /readyz    依赖（PG / Redis）可用，结果缓存  → readiness 探针
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sqlalchemy import text

from app.core.db import AsyncSessionFactory
from app.core.redis_client import get_redis

router = APIRouter(tags=["health"])

# readiness 结果缓存：探针每 10s 一次，不能每次真打 DB/Redis
_READY_CACHE: dict = {"at": 0.0, "body": None, "status": 503}
_READY_TTL_SECONDS = 5.0


@router.get("/healthz", summary="存活探针")
async def healthz():
    """只证明进程活着。依赖挂了不应触发容器重启。"""
    return {"status": "ok"}


@router.get("/startupz", summary="启动探针")
async def startupz():
    """lifespan 初始化完成后才返回 200（见 app/core/lifespan.py）。"""
    from app.core.lifespan import is_started

    if not is_started():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="starting"
        )
    return {"status": "ok"}


@router.get("/readyz", summary="就绪探针")
async def readyz(r: Redis = Depends(get_redis)):
    """依赖可用才返回 200；结果缓存 5 秒，避免探针打爆依赖。"""
    now = time.monotonic()
    if now - _READY_CACHE["at"] < _READY_TTL_SECONDS and _READY_CACHE["body"] is not None:
        return JSONResponse(status_code=_READY_CACHE["status"], content=_READY_CACHE["body"])

    checks: dict[str, str] = {}

    try:
        async with AsyncSessionFactory() as session:
            await session.execute(text("SELECT 1"))
        checks["postgres"] = "ok"
    except Exception as e:  # noqa: BLE001 - 探针要兜住任何异常
        checks["postgres"] = f"error: {type(e).__name__}"

    try:
        await r.ping()
        checks["redis"] = "ok"
    except Exception as e:  # noqa: BLE001
        checks["redis"] = f"error: {type(e).__name__}"

    healthy = all(v == "ok" for v in checks.values())
    body = {"status": "ok" if healthy else "degraded", "checks": checks}
    code = status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE

    _READY_CACHE.update({"at": now, "body": body, "status": code})
    return JSONResponse(status_code=code, content=body)
```

### 1.3 修改点：`app/routers/__init__.py` 注册路由

```python
from app.routers.health_router import router as health_router

def setup_routers(app: FastAPI) -> None:
    app.include_router(health_router)          # 健康检查（无业务前缀）
    app.include_router(user_router)
    app.include_router(position_router)
    app.include_router(candidate_router)
    app.include_router(mail_submission_router)
```

### 1.4 修改点：`app/core/lifespan.py` 暴露启动完成标记

```python
# 模块级标记：startup 探针读取
_started = False


def is_started() -> bool:
    return _started


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _started
    from app.core.checkpointer import init_checkpointer, close_checkpointer
    from app.tasks.timeout_tasks import TimeoutSweepManager
    from app.tasks.mail_submission_tasks import MailSubmissionSweepManager
    from utils.emailbot.mail_manager import MailManager

    setup_logging(level="INFO")
    await init_redis()
    await init_checkpointer()
    await MailManager.init(...)
    await TimeoutSweepManager.init()
    await MailSubmissionSweepManager.init()

    _started = True   # ← 新增：初始化全部完成

    yield

    _started = False
    await MailSubmissionSweepManager.close()
    await TimeoutSweepManager.close()
    await MailManager.close()
    await close_redis()
    await close_http_client()
    await close_checkpointer()
    stop_logging()
```

### 1.5 容器探针

**Dockerfile `HEALTHCHECK`**（只判 liveness，`ops_plan.md` 已约定）：

```dockerfile
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1
```

**docker-compose**：

```yaml
  app:
    healthcheck:
      test: ["CMD-SHELL", "curl -fsS http://127.0.0.1:8000/readyz || exit 1"]
      interval: 15s
      timeout: 3s
      retries: 3
      start_period: 40s      # 邮件机器人 IMAP 连接较慢
```

**Kubernetes**：

```yaml
startupProbe:
  httpGet: { path: /startupz, port: 8000 }
  failureThreshold: 30
  periodSeconds: 2
livenessProbe:
  httpGet: { path: /healthz, port: 8000 }
  periodSeconds: 10
  failureThreshold: 3
readinessProbe:
  httpGet: { path: /readyz, port: 8000 }
  periodSeconds: 10
  failureThreshold: 2
```

> ⚠️ 本项目 `gunicorn` 默认 `workers=1`（邮件机器人长连接约束）：**readiness 失败 = 整体不可用**，告警级别要更高；liveness 必须包含后台任务心跳（§2.5），不能只看进程。

---

## 2. 指标监控（Prometheus）

### 2.1 依赖

```bash
uv add prometheus-fastapi-instrumentator   # 自带 HTTP RED 指标
uv add prometheus-client                   # 自定义业务指标
# 可选：链路追踪
uv add opentelemetry-api opentelemetry-sdk opentelemetry-exporter-otlp \
       opentelemetry-instrumentation-fastapi opentelemetry-instrumentation-sqlalchemy \
       opentelemetry-instrumentation-redis opentelemetry-instrumentation-httpx
```

### 2.2 新增文件：`app/core/metrics.py`

```python
"""Prometheus 指标定义（进程级单例，import 即注册到默认 Registry）。

命名规范：simplejob_<域>_<指标>_[单位]
标签规范：只允许低基数标签（status/node/service/result/reason），
          **禁止** candidate_id / email / user_id 等高基数值。
"""
from __future__ import annotations

import time

from prometheus_client import Counter, Gauge, Histogram
from prometheus_client.core import GaugeMetricFamily

# ==================== 登录 / 鉴权 ====================
LOGIN_FAILED = Counter(
    "simplejob_login_failed_total", "登录失败次数", ["reason"]
)
LOGIN_RATE_LIMITED = Counter(
    "simplejob_login_rate_limited_total", "登录限流触发次数"
)
TOKEN_REFRESH_FAILED = Counter(
    "simplejob_token_refresh_failed_total", "刷新令牌失败次数"
)

# ==================== 简历 ====================
RESUME_UPLOAD_TOTAL = Counter("simplejob_resume_upload_total", "简历上传次数")
RESUME_PARSE_FAIL_TOTAL = Counter("simplejob_resume_parse_fail_total", "简历解析失败次数")
RESUME_PARSE_DURATION = Histogram(
    "simplejob_resume_parse_duration_seconds",
    "简历解析耗时（秒）",
    buckets=[1, 5, 10, 30, 60, 120, 300],
)
RESUME_DOWNLOAD_TOTAL = Counter(
    "simplejob_resume_download_total", "简历下载次数", ["result"]   # ok / denied / missing
)

# ==================== 工作流（LangGraph） ====================
WORKFLOW_STAGE_TOTAL = Counter(
    "simplejob_workflow_stage_total", "工作流阶段完成次数", ["stage", "result"]
)
WORKFLOW_NODE_DURATION = Histogram(
    "simplejob_workflow_node_duration_seconds",
    "工作流节点耗时（秒）",
    ["node"],
    buckets=[0.1, 0.5, 1, 2, 5, 10, 30, 60],
)
WORKFLOW_NODE_FAIL_TOTAL = Counter(
    "simplejob_workflow_node_fail_total", "工作流节点失败次数", ["node"]
)

# ==================== 外部服务（OCR / AI / 钉钉 / 邮件） ====================
EXT_REQUEST_TOTAL = Counter(
    "simplejob_external_request_total", "外部服务调用次数", ["service", "result"]
)
EXT_REQUEST_DURATION = Histogram(
    "simplejob_external_request_duration_seconds",
    "外部服务耗时（秒）",
    ["service"],
    buckets=[0.1, 0.5, 1, 2, 5, 10, 30, 600],
)

# ==================== 后台任务调度健康 ====================
# ⚠️ 不要用固定静默阈值：生产环境 PeriodicTaskManager 走 daily_times
#    （默认 08:00,12:00,17:00,24:00），最大间隔 8 小时。
#    阈值必须由任务自己的调度配置推导，见 §2.5。
BG_TASK_LAST_RUN = Gauge(
    "simplejob_bg_task_last_run_seconds",
    "后台任务最后一次【成功】执行的时间戳（失败不更新）",
    ["job"],
)
BG_TASK_EXPECTED_INTERVAL = Gauge(
    "simplejob_bg_task_expected_interval_seconds",
    "后台任务的期望执行间隔（由调度配置推导）",
    ["job"],
)
BG_TASK_CONSECUTIVE_FAILURES = Gauge(
    "simplejob_bg_task_consecutive_failures",
    "后台任务连续失败次数",
    ["job"],
)

_BG_LAST_RUN: dict[str, float] = {}


def heartbeat(job: str, expected_interval_seconds: float, ok: bool = True) -> None:
    """后台任务每轮调用一次。

    Args:
        expected_interval_seconds: 相邻两次执行的最大间隔（秒）。
            interval 模式 = 配置间隔 × 60；daily_times 模式 = 时刻表最大间隔。
        ok: 本轮是否成功。False 时只更新期望间隔，不刷新成功时间。
    """
    BG_TASK_EXPECTED_INTERVAL.labels(job=job).set(expected_interval_seconds)
    if ok:
        now = time.time()
        _BG_LAST_RUN[job] = now
        BG_TASK_LAST_RUN.labels(job=job).set(now)


# ==================== 日志队列（项目特有） ====================
LOG_QUEUE_SIZE = Gauge("simplejob_log_queue_size", "日志队列积压条数")
LOG_LISTENER_ALIVE = Gauge("simplejob_log_listener_alive", "日志监听进程是否存活")

# ==================== 业务分布（看板/告警用） ====================
CANDIDATE_STATUS = Gauge(
    "simplejob_candidate_status_count", "候选人状态分布", ["status"]
)
```

### 2.3 修改点：`app/main.py` 接入 Instrumentator

```python
from prometheus_fastapi_instrumentator import Instrumentator

def create_app() -> FastAPI:
    app = FastAPI(...)

    setup_middlewares(app)
    setup_exception_handlers(app)
    setup_routers(app)

    # HTTP RED 指标 + /metrics 端点
    Instrumentator(excluded_urls="/metrics,/healthz,/readyz,/startupz").instrument(
        app
    ).expose(app, endpoint="/metrics", include_in_schema=False)

    @app.get("/")
    async def root():
        return {"message": "Hello World"}

    return app
```

### 2.4 埋点位置（只列插入点，不改逻辑）

| 指标 | 插入位置 |
|---|---|
| `LOGIN_FAILED` | `app/routers/user_router.py` 登录 `except` 分支 |
| `LOGIN_RATE_LIMITED` | `app/core/rate_limit.py` 抛 429 前 |
| `TOKEN_REFRESH_FAILED` | `app/core/auth.py` `refresh_access_token` 失败分支 |
| `RESUME_DOWNLOAD_TOTAL` | `candidate_router.download_candidate_resume`（成功/403/404 分别打标签） |
| `WORKFLOW_STAGE_TOTAL` / `WORKFLOW_NODE_FAIL_TOTAL` | `app/workflow/error_handler.py` 的 `node_error_wrapper` 捕获点 |
| `WORKFLOW_NODE_DURATION` | 各节点函数入口/出口计时（可用装饰器统一处理） |
| `EXT_REQUEST_TOTAL` / `EXT_REQUEST_DURATION` | `app/core/http_client.py` 统一出口（OCR / DashScope / 钉钉 / 邮件） |
| `CANDIDATE_STATUS` | 定时任务（每 15 分钟）复用 `/candidate/candidate/distribution` 口径写 Gauge |

示例（HTTP 客户端统一埋点）：

```python
# app/core/http_client.py（片段）
import time
from app.core.metrics import EXT_REQUEST_TOTAL, EXT_REQUEST_DURATION

async def request(service: str, method: str, url: str, **kwargs):
    start = time.perf_counter()
    result = "ok"
    try:
        return await _client.request(method, url, **kwargs)
    except Exception:
        result = "error"
        raise
    finally:
        EXT_REQUEST_TOTAL.labels(service=service, result=result).inc()
        EXT_REQUEST_DURATION.labels(service=service).observe(time.perf_counter() - start)
```

### 2.5 后台任务健康（本项目重点）

`TimeoutSweepManager` 与 `MailManager` 与 API 同进程（gunicorn 单 worker），**进程活着 ≠ 任务在跑**。

#### 2.5.1 先看清调度模式（关键前提）

调度策略集中在 `app/tasks/scheduler.py`，`PeriodicTaskManager`（周期巡检）与
`MailPollTimer`（邮件轮询，`app/tasks/mail_poll_loop.py`）共用：

| 模式 | 行为 | 间隔 |
|---|---|---|
| `dev_interval` | `DEBUG=True` 时的固定短间隔 | `PERIODIC_TASK_DEV_INTERVAL_SECONDS`（默认 15 秒） |
| `daily_times` | `DEBUG=False` 时的每日时刻 | 默认 `08:00,12:00,17:00,24:00`，**最大间隔 8 小时** |
| `interval` | 仅邮件轮询使用 | `MAIL_POLL_INTERVAL_SECONDS`（默认 15 秒） |

> 调度模式不再做成手动三选一，改由配置层的 computed field 给出：
> `Settings.PERIODIC_TASK_MODE`（`DEBUG` → `dev_interval`，否则 `daily_times`）
> 与 `Settings.MAIL_POLL_MODE`（恒为 `interval`）。

> ⚠️ **不要用固定静默阈值（如 300 秒）判断存活**：生产环境按每日时刻执行，
> 最大间隔 8 小时（`24:00 → 次日 08:00`），固定阈值会让「存活」指标几乎恒为 0，
> 告警变成永久误报。
>
> ⚠️ **`_run_once()` 失败会被 `except` 吞掉并继续循环**，所以心跳照常刷新也**查不出任务失败**，
> 必须同时暴露连续失败次数。
>
> ⚠️ **首次执行不要等一整个周期**：循环的第一句等待改用
> `BACKGROUND_TASK_STARTUP_DELAY_SECONDS`（默认 2 分钟），否则 `daily_times` 模式下
> 首次巡检要等最长 **8 小时**，这段空窗期反而会误触发 `BgTaskNotRunning`。

#### 2.5.2 埋点（阈值由调度配置推导）

```python
# app/tasks/timeout_tasks.py —— TimeoutSweepManager._run_once
from app.core.metrics import heartbeat

@classmethod
async def _run_once(cls) -> None:
    expected = cls._max_interval_seconds()      # 由 _next_delay_seconds() 推导最大间隔
    try:
        swept = await sweep_stale_email_replies()
    except Exception:
        heartbeat("email-reply-timeout-sweep", expected, ok=False)
        raise
    heartbeat("email-reply-timeout-sweep", expected, ok=True)
    if swept:
        logger.info("邮件回复超时巡检：已推进 %s 个候选人", swept)
```

```python
# app/tasks/mail_submission_tasks.py —— MailSubmissionSweepManager._run_once
@classmethod
async def _run_once(cls) -> None:
    expected = cls._max_interval_seconds()
    try:
        expired = await expire_stale_submissions()
    except Exception:
        heartbeat("mail-submission-sweep", expected, ok=False)
        raise
    heartbeat("mail-submission-sweep", expected, ok=True)
    if expired:
        logger.info("待确认投递巡检：置为 EXPIRED %s 条", expired)
```

```python
# utils/emailbot/mail_manager.py —— MailManager._poll_once（只含业务动作）
@classmethod
async def _poll_once(cls) -> None:
    mgr = cls.get()
    try:
        await mgr._bot.poll_once()
    except Exception:
        heartbeat("mail-manager", MailPollTimer.max_interval_seconds(), ok=False)
        logger.exception("poll round crashed, will retry")
        return
    heartbeat("mail-manager", MailPollTimer.max_interval_seconds(), ok=True)
```

> **计时器已单独抽到 `app/tasks/mail_poll_loop.py`（`MailPollTimer`）**，
> 与 `PeriodicTaskManager` 共用 `app/tasks/scheduler.py` 的调度策略，
> 但邮件是**独立开关**：恒为固定间隔 `MAIL_POLL_INTERVAL_SECONDS`（默认 **15 秒**），
> 既不看 `DEBUG` 也不跟随全局 `PERIODIC_TASK_MODE`。
>
> `expected` 统一取 `MailPollTimer.max_interval_seconds()`，不要自己算——
> 邮件是固定 15 秒；周期巡检在 `daily_times` 下是 **8 小时**（`24:00 → 次日 08:00`）。

> `_max_interval_seconds()` 的实现已落地在 `PeriodicTaskManager`：
> `dev_interval` 模式直接返回配置值；`daily_times` 模式把 `PERIODIC_TASK_DAILY_TIMES`
> 排序后取相邻间隔的最大值（含首尾跨天），避免生产环境误报。

#### 2.5.3 失败重试 + 连续失败计数暴露成指标

**重试属于「一次执行」内部**：每次调度执行最多尝试 `1 + BACKGROUND_TASK_RETRY_TIMES`
次（默认 4 次），重试耗尽才记为一次「执行失败」。

```python
# app/tasks/retry.py
async def run_with_retry(fn, *, retries=3, base_delay=1.0, max_delay=30.0, on_retry=None):
    """失败后按指数退避重试；返回 None 表示成功，否则为耗尽时的最后一个异常。"""
```

```python
# app/tasks/periodic_task.py —— _loop 内
from app.tasks.retry import run_with_retry

        while True:
            last_exc = await run_with_retry(
                cls._run_once,
                retries=settings.BACKGROUND_TASK_RETRY_TIMES,
                base_delay=settings.BACKGROUND_TASK_RETRY_BASE_DELAY_SECONDS,
                max_delay=settings.BACKGROUND_TASK_RETRY_MAX_DELAY_SECONDS,
                on_retry=cls._on_retry,
            )
            if last_exc is None:
                cls._on_success()
            else:
                cls._on_failure(last_exc)      # 重试耗尽 → 记一次「执行失败」
            await asyncio.sleep(cls._next_delay_seconds())
```

**告警阈值**：`PERIODIC_TASK_FAILURE_ALERT_THRESHOLD` 默认 **3**
（即**容忍 2 次执行失败**，第 3 次才告警，恢复前只报一次）。

达到阈值时打一条带固定关键字的 ERROR 日志（供 Loki 日志告警匹配）：

```text
[bg_task_alert]邮件回复超时巡检连续 3 次执行失败（每次已重试 3 次，阈值 3），需人工介入；恢复前不再重复告警
```

把计数同步成指标：

```python
# app/tasks/periodic_task.py —— _on_failure / _on_success 内
from app.core.metrics import BG_TASK_CONSECUTIVE_FAILURES
BG_TASK_CONSECUTIVE_FAILURES.labels(job=cls._task_name).set(cls._consecutive_failures)
```

#### 2.5.4 日志链路健康（项目特有）

```python
# 定时采样日志队列（可放在 TimeoutSweepManager 里，或独立 asyncio 任务）
from app.core.metrics import LOG_QUEUE_SIZE, LOG_LISTENER_ALIVE
from app.core.logging_config import _SHARED_QUEUE, _LISTENER_PROCESS

LOG_QUEUE_SIZE.set((_SHARED_QUEUE.qsize() if _SHARED_QUEUE else 0))
LOG_LISTENER_ALIVE.set(1 if (_LISTENER_PROCESS and _LISTENER_PROCESS.is_alive()) else 0)
```

### 2.6 指标清单（ Prometheus 侧可见）

| 指标 | 类型 | 标签 | 用途 |
|---|---|---|---|
| `http_requests_total` | Counter | method, route, status | QPS / 错误率 |
| `http_request_duration_seconds_bucket` | Histogram | le, route | 延迟分位 |
| `simplejob_login_failed_total` | Counter | reason | 爆破检测 |
| `simplejob_login_rate_limited_total` | Counter | – | 限流 |
| `simplejob_resume_download_total` | Counter | result | 敏感操作 |
| `simplejob_workflow_node_duration_seconds` | Histogram | node | 流程瓶颈 |
| `simplejob_workflow_node_fail_total` | Counter | node | 节点失败 |
| `simplejob_external_request_total` / `_duration_seconds` | Counter/Histogram | service, result | OCR/AI/钉钉/邮件 |
| `simplejob_bg_task_last_run_seconds` / `_expected_interval_seconds` / `_consecutive_failures` | Gauge | job | 巡检/邮件机器人调度健康（见 §2.5） |
| `simplejob_log_listener_alive` / `simplejob_log_queue_size` | Gauge | – | 日志链路 |
| `simplejob_candidate_status_count` | Gauge | status | 业务漏斗 |
| `pg_up` / `pg_stat_activity_count` | Gauge | – | 数据库 |
| `redis_up` / `redis_connected_clients` | Gauge | – | 缓存 |

---

## 3. 日志采集（Loki + Promtail）

### 3.1 `deploy/loki/promtail-config.yml`

```yaml
server:
  http_listen_port: 3101

clients:
  - url: http://loki:3100/loki/api/v1/push

scrape_configs:
  # 业务日志（workflow.log / error.log / nodes.log）
  - job_name: simplejob
    static_configs:
      - targets: [localhost]
        labels:
          job: simplejob
          env: prod
          __path__: /var/log/simplejob
    pipeline_stages:
      # 多行合并：日志以 "YYYY-MM-DD HH:MM:SS |" 开头
      - multiline:
          firstline: '^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}'
          max_wait_time: 3s
      - regex:
          expression: >-
            ^(?P<ts>\S+ \S+)\s+\|\s+(?P<level>\w+)\s+\|\s+(?P<logger>[^|]+)\s+\|
            \s+(?P<thread>[^|]+)\s+\|\s+req=(?P<request_id>\S+)\s+\|\s+(?P<msg>.*)$
      - labels:
          level: level
          request_id: request_id
      - timestamp:
          source: ts
          format: "2006-01-02 15:04:05"

  # 审计日志（JSON Lines，单独标签便于告警）
  - job_name: simplejob-audit
    static_configs:
      - targets: [localhost]
        labels:
          job: simplejob-audit
          __path__: /var/log/simplejob/audit.log
    pipeline_stages:
      - json:
          expressions:
            ts: ts
            action: action
            request_id: request_id
      - labels:
          action: action
      - timestamp:
          source: ts
          format: RFC3339
```

### 3.2 Loki 告警（可选，替代「日志告警」）

用 Loki Ruler（或 Logcli + cron）对关键模式告警：

```yaml
# deploy/loki/loki-config.yml（片段：启用 ruler）
ruler:
  storage:
    type: local
    local:
      directory: /loki/rules
  rule_path: /loki/rules
  alertmanager_url: http://alertmanager:9093
  enable_alertmanager_v2: true
```

```yaml
# /loki/rules/simplejob.yaml
groups:
  - name: simplejob-logs
    rules:
      - alert: ResumeFileMissingSpike
        expr: |
          sum(count_over_time({job="simplejob"} |= "简历文件不存在" [10m])) > 20
        for: 10m
        labels: { severity: p2 }
        annotations:
          summary: "简历文件缺失日志激增（可能上传/存储链路异常）"

      - alert: WorkflowErrorSpike
        expr: |
          sum(count_over_time({job="simplejob"} |~ "ERROR|Exception" [5m])) > 30
        for: 5m
        labels: { severity: p1 }
```

---

## 4. 链路追踪（OpenTelemetry）

### 4.1 新增文件：`app/core/tracing.py`

```python
"""OpenTelemetry 初始化：FastAPI / SQLAlchemy / Redis / httpx 自动插桩。"""
from __future__ import annotations

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.redis import RedisInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from app.core.config import get_settings
from app.core.db import engine


def setup_tracing(app) -> None:
    settings = get_settings()
    if settings.environment == "development" and not settings.debug:
        return

    resource = Resource.create(
        {
            "service.name": settings.app_name,
            "service.version": settings.version,
            "deployment.environment": settings.environment,
        }
    )
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint="http://otel-collector:4317"))
    )
    trace.set_tracer_provider(provider)

    FastAPIInstrumentor.instrument_app(app)
    SQLAlchemyInstrumentor().instrument(engine=engine)
    RedisInstrumentor().instrument()
    HTTPXClientInstrumentor().instrument()


def get_tracer():
    return trace.get_tracer("simplejob")
```

### 4.2 修改点：`app/main.py` 调用

```python
from app.core.tracing import setup_tracing

def create_app() -> FastAPI:
    app = FastAPI(...)
    ...
    setup_tracing(app)      # ← 新增
    return app
```

### 4.3 日志注入 `trace_id`（让日志 ↔ 追踪互跳）

```python
# app/core/logging_config.py（片段）
from opentelemetry import trace

class TraceIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        ctx = trace.get_current_span().get_span_context()
        record.trace_id = format(ctx.trace_id, "032x") if ctx.is_valid else "-"
        return True

# LOG_FORMAT 末尾追加：... | trace=%(trace_id)s
LOG_FORMAT = (
    "%(asctime)s | %(levelname)-8s | %(name)s:%(funcName)s:%(lineno)d | "
    "%(threadName)s | req=%(request_id)s | trace=%(trace_id)s | %(message)s"
)

# _build_handlers() 里给每个 handler 增加：
#   handler.addFilter(TraceIdFilter())
```

> LangGraph 节点可用 `with get_tracer().start_as_current_span("node:scoring"):` 包裹，
> 让「邮件协商流程」的每一步都能在 Tempo 里下钻。

---

## 5. Prometheus 配置与告警规则

### 5.1 `deploy/prometheus/prometheus.yml`

```yaml
global:
  scrape_interval: 15s
  evaluation_interval: 15s
  external_labels:
    cluster: simplejob
    env: prod

rule_files:
  - /etc/prometheus/alerts.yml

alerting:
  alertmanagers:
    - static_configs:
        - targets: ["alertmanager:9093"]

scrape_configs:
  - job_name: simplejob
    metrics_path: /metrics
    static_configs:
      - targets: ["app:8000"]
        labels: { service: simplejob, env: prod }

  - job_name: postgres
    static_configs:
      - targets: ["postgres-exporter:9187"]
        labels: { service: postgres }

  - job_name: redis
    static_configs:
      - targets: ["redis-exporter:9121"]
        labels: { service: redis }

  - job_name: node
    static_configs:
      - targets: ["node-exporter:9100"]
        labels: { service: node }

  - job_name: pushgateway
    honor_labels: true
    static_configs:
      - targets: ["pushgateway:9091"]

  # 外部拨测（证书、可达性）
  - job_name: blackbox
    metrics_path: /probe
    params:
      module: [http_2xx]
    static_configs:
      - targets:
          - https://hr.example.com/healthz
          - https://hr.example.com/readyz
    relabel_configs:
      - source_labels: [__address__]
        target_label: __param_target
      - source_labels: [__param_target]
        target_label: instance
      - target_label: __address__
        replacement: blackbox-exporter:9115
```

### 5.2 `deploy/prometheus/alerts.yml`（完整规则）

```yaml
groups:
  # ==================== 服务可用性 ====================
  - name: simplejob-availability
    interval: 30s
    rules:
      - alert: SimpleJobDown
        expr: up{job="simplejob"} == 0
        for: 1m
        labels: { severity: p0, team: hr }
        annotations:
          summary: "SimpleJob 服务不可达"
          description: "实例 {{ $labels.instance }} 已 1 分钟无指标"
          runbook: "https://git.example.com/simplejob/docs/runbook.md#simplejobdown"

      - alert: SimpleJobReadyFailing
        expr: probe_success{job="blackbox", module="http_2xx", instance=~".*readyz.*"} == 0
        for: 2m
        labels: { severity: p0 }
        annotations:
          summary: "就绪探针失败（依赖不可用或应用未就绪）"

      - alert: PostgresDown
        expr: pg_up == 0
        for: 1m
        labels: { severity: p0 }
        annotations: { summary: "PostgreSQL 不可用" }

      - alert: RedisDown
        expr: redis_up == 0
        for: 1m
        labels: { severity: p0 }
        annotations: { summary: "Redis 不可用（登录/限流/巡检锁都会受影响）" }

      - alert: MailBotDead
        expr: |
          (time() - simplejob_bg_task_last_run_seconds{job="mail-manager"})
            > simplejob_bg_task_expected_interval_seconds{job="mail-manager"} * 3
        for: 15m
        labels: { severity: p0 }
        annotations:
          summary: "邮件机器人超过 3 个周期没有成功轮询，面试协商邮件已停摆"

      - alert: BgTaskNotRunning
        expr: |
          (time() - simplejob_bg_task_last_run_seconds)
            > simplejob_bg_task_expected_interval_seconds * 3
        for: 10m
        labels: { severity: p1 }
        annotations:
          summary: "后台任务 {{ $labels.job }} 超过 3 个周期没有成功执行"

      - alert: BgTaskFailing
        # 「执行失败」= 重试耗尽；阈值 3 = 容忍 2 次执行失败
        expr: simplejob_bg_task_consecutive_failures >= 3
        for: 15m
        labels: { severity: p1 }
        annotations:
          summary: "后台任务 {{ $labels.job }} 已连续 3 次执行失败（每次均已重试）"

# 日志告警（无需 prometheus-client，Loki Ruler 直接匹配告警关键字）
#   - alert: BgTaskRetryExhausted
#     expr: sum(count_over_time({job="simplejob"} |= "[bg_task_alert]" [5m])) > 0
#     labels: { severity: p1 }
#     annotations: { summary: "后台任务重试耗尽，连续执行失败已达阈值" }

  # ==================== 错误与延迟 ====================
  - name: simplejob-performance
    interval: 30s
    rules:
      - alert: HighErrorRate5xx
        expr: |
          sum(rate(http_requests_total{status=~"5.."}[5m]))
          / sum(rate(http_requests_total[5m])) > 0.01
        for: 5m
        labels: { severity: p0 }
        annotations: { summary: "5xx 比例 > 1%，持续 5 分钟" }

      - alert: HighErrorRate4xx
        expr: |
          sum(rate(http_requests_total{status=~"4.."}[5m]))
          / sum(rate(http_requests_total[5m])) > 0.10
        for: 10m
        labels: { severity: p2 }
        annotations: { summary: "4xx 比例 > 10%（可能是接口变更/前端异常）" }

      - alert: P95LatencyHigh
        expr: |
          histogram_quantile(0.95,
            sum(rate(http_request_duration_seconds_bucket[5m])) by (le, route)) > 2
        for: 5m
        labels: { severity: p1 }
        annotations: { summary: "路由 P95 > 2s" }

      - alert: SlowQueries
        expr: |
          rate(simplejob_external_request_duration_seconds_sum{service=~"dashscope|paddleocr"}[5m])
          / rate(simplejob_external_request_duration_seconds_count{service=~"dashscope|paddleocr"}[5m]) > 30
        for: 10m
        labels: { severity: p1 }
        annotations: { summary: "OCR/AI 平均耗时 > 30s" }

  # ==================== 外部依赖 / 业务 ====================
  - name: simplejob-dependencies
    interval: 30s
    rules:
      - alert: AiOcrFailureRate
        expr: |
          sum(rate(simplejob_external_request_total{service=~"dashscope|paddleocr", result="error"}[10m]))
          / sum(rate(simplejob_external_request_total{service=~"dashscope|paddleocr"}[10m])) > 0.05
        for: 10m
        labels: { severity: p1 }
        annotations: { summary: "OCR/AI 调用失败率 > 5%" }

      - alert: LoginBruteForce
        expr: sum(increase(simplejob_login_failed_total[5m])) > 50
        for: 0m
        labels: { severity: p1 }
        annotations: { summary: "5 分钟内登录失败 > 50 次，疑似撞库" }

      - alert: ResumeDownloadAnomaly
        expr: |
          sum(increase(simplejob_resume_download_total{result="denied"}[1h])) > 20
        for: 10m
        labels: { severity: p2 }
        annotations: { summary: "简历下载被拒次数异常（越权尝试？）" }

      - alert: CandidateStuck
        expr: |
          simplejob_candidate_status_count{status="处理异常"} > 10
        for: 1h
        labels: { severity: p2 }
        annotations: { summary: "处理异常候选人积压 > 10" }

  # ==================== 日志链路 ====================
  - name: simplejob-logging
    interval: 30s
    rules:
      - alert: LogListenerDead
        expr: simplejob_log_listener_alive == 0
        for: 2m
        labels: { severity: p1 }
        annotations: { summary: "日志监听进程已退出，日志只会堆在队列里" }

      - alert: LogQueueBacklog
        expr: simplejob_log_queue_size > 10000
        for: 5m
        labels: { severity: p1 }
        annotations: { summary: "日志队列积压 > 1 万条（磁盘 IO 或监听进程异常）" }

  # ==================== 资源 / 证书 ====================
  - name: simplejob-resources
    interval: 60s
    rules:
      - alert: DiskUsageHigh
        expr: |
          (1 - node_filesystem_avail_bytes{mountpoint="/"}
          / node_filesystem_size_bytes{mountpoint="/"}) > 0.85
        for: 10m
        labels: { severity: p2 }
        annotations: { summary: "根分区使用率 > 85%" }

      - alert: CertExpiringSoon
        expr: (probe_ssl_earliest_cert_expiry{job="blackbox"} - time()) / 86400 < 7
        for: 1h
        labels: { severity: p2 }
        annotations: { summary: "TLS 证书 7 天内到期" }

      - alert: BackupFailed
        expr: increase(simplejob_backup_fail_total[24h]) > 0
        for: 0m
        labels: { severity: p2 }
        annotations: { summary: "最近 24 小时备份失败" }
```

---

## 6. Alertmanager 配置

### 6.1 `deploy/alertmanager/alertmanager.yml`

```yaml
global:
  resolve_timeout: 5m
  smtp_smarthost: 'smtp.163.com:465'
  smtp_from: 'alert@example.com'
  smtp_auth_username: 'alert@example.com'
  smtp_auth_password: '${SMTP_PASSWORD}'
  smtp_require_tls: false

# 钉钉 Markdown 模板
templates:
  - '/etc/alertmanager/templates/*.tmpl'

route:
  receiver: dingtalk-p1
  group_by: ['alertname', 'severity', 'instance']
  group_wait: 30s
  group_interval: 5m
  repeat_interval: 4h
  routes:
    - matchers: [ severity = p0 ]
      receiver: dingtalk-p0
      continue: true          # 同时发给 P0 与默认组，确保升级
    - matchers: [ severity = p1 ]
      receiver: dingtalk-p1
    - matchers: [ severity =~ "p2|p3" ]
      receiver: email

receivers:
  - name: dingtalk-p0
    webhook_configs:
      - url: 'http://alertmanager-dingtalk:8060/dingtalk/p0/send'
        send_resolved: true
    # 电话/短信：接第三方告警平台（如阿里云语音、PagerDuty）
    # pagerduty_configs:
    #   - service_key: '${PAGERDUTY_KEY}'

  - name: dingtalk-p1
    webhook_configs:
      - url: 'http://alertmanager-dingtalk:8060/dingtalk/p1/send'
        send_resolved: true

  - name: email
    email_configs:
      - to: 'ops@example.com'
        send_resolved: true

# 抑制：避免告警风暴
inhibit_rules:
  # PG 挂了 → 抑制依赖它的应用告警
  - source_matchers: [ alertname = PostgresDown ]
    target_matchers: [ alertname =~ "SimpleJobReadyFailing|HighErrorRate5xx|SlowQueries" ]
    equal: [ instance ]

  # 服务整体不可达 → 抑制该实例的全部其他告警
  - source_matchers: [ alertname = SimpleJobDown ]
    target_matchers: [ alertname =~ ".*" ]
    equal: [ instance ]

  # 维护期静默（示例：每周三 02:00-03:00）
  # - name: maintenance-window
  #   time_intervals:
  #     - times:
  #         - { start_time: '02:00', end_time: '03:00' }
  #       weekdays: ['wednesday']
```

### 6.2 钉钉模板 `templates/dingtalk.tmpl`

```gotemplate
{{ define "dingtalk.simplejob.text" }}
{{ range .Alerts }}
【{{ .Labels.severity | toUpper }}】{{ .Annotations.summary }}
实例：{{ .Labels.instance }}
详情：{{ .Annotations.description }}
时间：{{ .StartsAt.Format "2006-01-02 15:04:05" }}
Runbook：{{ .Annotations.runbook }}
{{ end }}
{{ end }}
```

> 生产建议：P0 必须**电话 + 钉钉**双通道；邮件系统本身故障时不能只靠邮件告警。

---

## 7. Grafana 看板

### 7.1 数据源 provisioning

```yaml
# deploy/grafana/provisioning/datasources/prometheus.yml
apiVersion: 1
datasources:
  - name: Prometheus
    type: prometheus
    uid: prometheus
    access: proxy
    url: http://prometheus:9090
    isDefault: true

  - name: Loki
    type: loki
    uid: loki
    access: proxy
    url: http://loki:3100

  - name: Tempo
    type: tempo
    uid: tempo
    access: proxy
    url: http://tempo:3200
```

```yaml
# deploy/grafana/provisioning/dashboards/default.yml
apiVersion: 1
providers:
  - name: simplejob
    folder: SimpleJob
    type: file
    options:
      path: /var/lib/grafana/dashboards
```

### 7.2 看板 JSON（`deploy/grafana/dashboards/simplejob-overview.json`）

```json
{
  "uid": "simplejob-overview",
  "title": "SimpleJob 总览",
  "tags": ["simplejob"],
  "timezone": "browser",
  "schemaVersion": 39,
  "version": 1,
  "time": { "from": "now-6h", "to": "now" },
  "refresh": "30s",
  "templating": { "list": [] },
  "annotations": { "list": [] },
  "panels": [
    {
      "id": 1, "type": "stat", "title": "QPS",
      "gridPos": { "h": 6, "w": 6, "x": 0, "y": 0 },
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "targets": [
        { "expr": "sum(rate(http_requests_total[5m]))", "legendFormat": "req/s" }
      ],
      "fieldConfig": { "defaults": { "unit": "reqps", "decimals": 2 }, "overrides": [] },
      "options": { "colorMode": "value", "graphMode": "area", "textMode": "value" }
    },
    {
      "id": 2, "type": "stat", "title": "5xx 比例",
      "gridPos": { "h": 6, "w": 6, "x": 6, "y": 0 },
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "targets": [
        {
          "expr": "sum(rate(http_requests_total{status=~\"5..\"}[5m])) / sum(rate(http_requests_total[5m]))",
          "legendFormat": "error ratio"
        }
      ],
      "fieldConfig": {
        "defaults": {
          "unit": "percentunit", "decimals": 2,
          "thresholds": {
            "mode": "absolute",
            "steps": [
              { "color": "green", "value": null },
              { "color": "orange", "value": 0.01 },
              { "color": "red", "value": 0.05 }
            ]
          }
        },
        "overrides": []
      },
      "options": { "colorMode": "background", "graphMode": "none", "textMode": "value" }
    },
    {
      "id": 3, "type": "timeseries", "title": "P95 延迟（按路由）",
      "gridPos": { "h": 8, "w": 12, "x": 12, "y": 0 },
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "targets": [
        {
          "expr": "histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket[5m])) by (le, route))",
          "legendFormat": "{{ route }}"
        }
      ],
      "fieldConfig": { "defaults": { "unit": "s" }, "overrides": [] }
    },
    {
      "id": 4, "type": "timeseries", "title": "QPS（按状态）",
      "gridPos": { "h": 8, "w": 12, "x": 0, "y": 8 },
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "targets": [
        {
          "expr": "sum by (status) (rate(http_requests_total[5m]))",
          "legendFormat": "{{ status }}"
        }
      ]
    },
    {
      "id": 5, "type": "piechart", "title": "候选人状态分布",
      "gridPos": { "h": 8, "w": 8, "x": 0, "y": 16 },
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "targets": [
        { "expr": "simplejob_candidate_status_count", "legendFormat": "{{ status }}" }
      ],
      "options": { "displayLabels": ["name", "value"], "legend": { "displayMode": "list", "placement": "right" } }
    },
    {
      "id": 6, "type": "stat", "title": "后台任务存活",
      "gridPos": { "h": 8, "w": 8, "x": 8, "y": 16 },
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "targets": [
        {
          "expr": "(time() - simplejob_bg_task_last_run_seconds) > simplejob_bg_task_expected_interval_seconds * 3",
          "legendFormat": "{{ job }}"
        }
      ],
      "fieldConfig": {
        "defaults": {
          "unit": "none",
          "thresholds": {
            "mode": "absolute",
            "steps": [
              { "color": "red", "value": null },
              { "color": "green", "value": 1 }
            ]
          }
        },
        "overrides": []
      },
      "options": { "colorMode": "background", "graphMode": "none" }
    },
    {
      "id": 7, "type": "timeseries", "title": "外部服务耗时（OCR/AI）",
      "gridPos": { "h": 8, "w": 8, "x": 16, "y": 16 },
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "targets": [
        {
          "expr": "rate(simplejob_external_request_duration_seconds_sum[5m]) / rate(simplejob_external_request_duration_seconds_count[5m])",
          "legendFormat": "{{ service }}"
        }
      ],
      "fieldConfig": { "defaults": { "unit": "s" }, "overrides": [] }
    },
    {
      "id": 8, "type": "timeseries", "title": "日志队列 / 监听进程",
      "gridPos": { "h": 8, "w": 24, "x": 0, "y": 24 },
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "targets": [
        { "expr": "simplejob_log_queue_size", "legendFormat": "queue size" },
        { "expr": "simplejob_log_listener_alive", "legendFormat": "listener alive" }
      ]
    }
  ]
}
```

> 建议再建「依赖」与「后台任务」两页，把 PG 连接池、Redis、邮件机器人轮询延迟放进去。

---

## 8. 监控栈编排

### 8.1 `docker-compose.monitoring.yml`

```yaml
name: simplejob-monitoring

services:
  prometheus:
    image: prom/prometheus:v2.54.0
    restart: unless-stopped
    volumes:
      - ./deploy/prometheus:/etc/prometheus:ro
      - prom_data:/prometheus
    command:
      - --config.file=/etc/prometheus/prometheus.yml
      - --storage.tsdb.retention.time=30d
      - --web.enable-lifecycle
    ports: ["9090:9090"]

  alertmanager:
    image: prom/alertmanager:v0.27.0
    restart: unless-stopped
    volumes: ["./deploy/alertmanager:/etc/alertmanager:ro"]
    command: ["--config.file=/etc/alertmanager/alertmanager.yml"]
    ports: ["9093:9093"]

  grafana:
    image: grafana/grafana:11.2.0
    restart: unless-stopped
    environment:
      GF_SECURITY_ADMIN_PASSWORD: ${GRAFANA_ADMIN_PASSWORD:?must set}
      GF_USERS_ALLOW_SIGN_UP: "false"
    volumes:
      - ./deploy/grafana/provisioning:/etc/grafana/provisioning:ro
      - ./deploy/grafana/dashboards:/var/lib/grafana/dashboards:ro
      - grafana_data:/var/lib/grafana
    ports: ["3000:3000"]
    depends_on: [prometheus, loki]

  loki:
    image: grafana/loki:3.2.0
    restart: unless-stopped
    command: -config.file=/etc/loki/loki-config.yml
    volumes: ["./deploy/loki:/etc/loki:ro", loki_data:/loki]
    ports: ["3100:3100"]

  promtail:
    image: grafana/promtail:3.2.0
    restart: unless-stopped
    volumes:
      - ./deploy/loki/promtail-config.yml:/etc/promtail/config.yml:ro
      - ./data/logs:/var/log/simplejob:ro
    depends_on: [loki]

  postgres-exporter:
    image: promcommunity/postgres-exporter:v0.16.0
    environment:
      DATA_SOURCE_NAME: "postgresql://${DB_USERNAME}:${DB_PASSWORD}@postgres:5432/${DB_NAME}?sslmode=disable"

  redis-exporter:
    image: oliver006/redis_exporter:v1.64.0
    command: ["--redis.addr=redis:6379"]

  node-exporter:
    image: prom/node-exporter:v1.8.2
    volumes: ["/:/host:ro"]
    command: ["--path.rootfs=/host"]

  blackbox-exporter:
    image: prom/blackbox-exporter:v0.25.0
    volumes: ["./deploy/blackbox/blackbox.yml:/etc/blackbox_exporter/config.yml:ro"]

  pushgateway:
    image: prom/pushgateway:v1.9.0
    restart: unless-stopped

  otel-collector:
    image: otel/opentelemetry-collector-contrib:0.108.0
    restart: unless-stopped
    volumes: ["./deploy/otel/otel-collector-config.yml:/etc/otelcol-contrib/config.yaml:ro"]
    ports: ["4317:4317", "4318:4318"]

  tempo:
    image: grafana/tempo:2.6.0
    command: ["-config.file=/etc/tempo/tempo-config.yml"]
    volumes: ["./deploy/tempo:/etc/tempo:ro"]

volumes:
  prom_data:
  grafana_data:
  loki_data:
```

### 8.2 Blackbox 配置 `deploy/blackbox/blackbox.yml`

```yaml
modules:
  http_2xx:
    prober: http
    timeout: 5s
    http:
      valid_status_codes: [200]
      method: GET
      preferred_ip_protocol: ip4
```

---

## 9. 合成监控（主动拨测）

### 9.1 新增 `scripts/ci/synthetics.py`

复用 `smoke.py` 的检查项，结果推 Pushgateway，供 Prometheus 告警：

```python
#!/usr/bin/env python3
"""合成监控：从外部视角拨测核心链路，指标推 Pushgateway。"""
from __future__ import annotations

import os
import time

import httpx
from prometheus_client import CollectorRegistry, Gauge, push_to_gateway

BASE = os.environ.get("SMOKE_BASE_URL", "https://hr.example.com")
EMAIL = os.environ.get("SMOKE_SEED_EMAIL", "boss@qq.com")
PASSWORD = os.environ.get("SMOKE_SEED_PASSWORD", "111111")
GATEWAY = os.environ.get("PUSHGATEWAY_URL", "http://pushgateway:9091")

registry = CollectorRegistry()
_success = Gauge("simplejob_synthetic_probe_success", "合成探测成功(1)/失败(0)", ["check"], registry=registry)
_duration = Gauge("simplejob_synthetic_probe_duration_seconds", "合成探测耗时", ["check"], registry=registry)

_failed: list[str] = []


def run(check: str, fn) -> None:
    start = time.perf_counter()
    ok = 0.0
    try:
        r = fn()
        if r.status_code < 500:
            ok = 1.0
    except Exception:
        ok = 0.0
    _success.labels(check).set(ok)
    _duration.labels(check).set(time.perf_counter() - start)
    if ok == 0.0:
        _failed.append(check)


def main() -> int:
    with httpx.Client(base_url=BASE, timeout=10.0) as c:
        run("healthz", lambda: c.get("/healthz"))
        run("readyz", lambda: c.get("/readyz"))

        def login():
            r = c.post("/user/token", json={"email": EMAIL, "password": PASSWORD})
            token = r.json().get("access_token")
            c.headers["Authorization"] = f"Bearer {token}"
            return r

        run("login", login)
        run("candidate_list", lambda: c.get("/candidate/list", params={"page": 1, "size": 10}))
        run("dashboard_distribution", lambda: c.get("/candidate/candidate/distribution"))

    push_to_gateway(GATEWAY, job="simplejob-synthetics", registry=registry)
    if _failed:
        print(f"合成探测失败：{_failed}")
        return 1
    print("合成探测全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

### 9.2 定时执行

```bash
# crontab（每 5 分钟）
*/5 * * * * cd /opt/simplejob && uv run python scripts/ci/synthetics.py >> logs/synthetics.log 2>&1
```

```yaml
# 或 Kubernetes CronJob
apiVersion: batch/v1
kind: CronJob
metadata: { name: simplejob-synthetics }
spec:
  schedule: "*/5 * * * *"
  jobTemplate:
    spec:
      template:
        spec:
          restartPolicy: Never
          containers:
            - name: synthetics
              image: simplejob-app:latest
              command: ["python", "scripts/ci/synthetics.py"]
```

对应告警：

```yaml
- alert: SyntheticProbeFailing
  expr: simplejob_synthetic_probe_success == 0
  for: 5m
  labels: { severity: p0 }
  annotations: { summary: "外部拨测失败：{{ $labels.check }}" }
```

---

## 10. 告警分级 / 值班 / Runbook

### 10.1 分级与路由

| 级别 | 判定 | 通道 | 响应 |
|---|---|---|---|
| **P0** | 服务不可用、依赖宕机、邮件链路中断 | 电话 + 钉钉 | 5 分钟内响应，15 分钟未确认升级 |
| **P1** | 错误率/延迟劣化、后台任务死亡、AI 异常、爆破 | 钉钉 + 邮件 | 30 分钟内响应 |
| **P2** | 资源水位、证书、备份、业务积压 | 邮件 + 钉钉（静默时段） | 工作日内处理 |
| **P3** | 业务量异常、趋势类 | 日报/看板 | 无需即时响应 |

### 10.2 Runbook 模板（每条告警一份）

```markdown
# Runbook: {{ alertname }}

## 现象
- 告警表达式：`{{ expr }}`
- 当前值 / 阈值：
- Grafana：<面板链接>

## 快速定位
1. 看总览：QPS / 错误率 / P95 是否同时异常？
2. 看依赖：PG / Redis / 邮件 / OCR 是否同时告警？
3. 看日志（Loki）：
   `sum(count_over_time({job="simplejob"} |= "{{ 关键字 }}" [10m]))`
4. 看链路（Tempo）：按 trace_id 下钻。

## 处置
- 单点：`docker compose restart app`
- 依赖：切备用/降级（关闭 `GRAPH_OPEN` / `MAIL_BOT_ENABLED`）
- 回滚：`git revert` + 重新构建镜像

## 升级
- 5 分钟未恢复 → 二级值班；15 分钟 → 负责人

## 复盘
- 时间线 / 根因 / 改进项（落到 backlog）
```

**本项目必须覆盖的关键字**（写进 Runbook）：
`[timeout_sweep] 推进失败`、`node_error`、`简历文件不存在`、`Redis 未初始化`、`Token expired`、`登录失败`、`resume.download`。

### 10.3 告警自检（防「告警系统自己坏了」）

- **Dead man's switch**：合成监控每 5 分钟推一次心跳指标；若 10 分钟无数据 → 触发 `MonitoringItselfDown`。
- **通知渠道连通性**：每天发一条测试告警。
- **月度演练**：手动停 Redis、杀日志监听进程、停邮件机器人，验证告警可达且内容正确。

---

## 11. 前端监控（简述）

| 能力 | 方案 |
|---|---|
| JS 错误 / 性能 | Sentry（`@sentry/vue`）或前端 APM；DSN 走环境变量注入 |
| 关键操作 | 复用后端 `request_id` 上报，便于前后端串联 |
| 静态资源 | Nginx access log + Blackbox 拨测首页 |
| 白屏/接口 5xx | Sentry `beforeSend` 过滤；接口错误率进 Grafana |

> 前端告警与后端统一进同一个钉钉群，按 `service=hr-frontend` 区分。

---

## 12. 落地步骤与验收

### 12.1 分阶段

| 阶段 | 内容 | 产出 | 验收 |
|---|---|---|---|
| **P0（1 天）** | 健康检查三端点 + Docker/compose 探针 | `/healthz` `/startupz` `/readyz` | 停 Redis → `/readyz` 返回 503 且不重启容器 |
| **P1（2 天）** | `/metrics` + 核心业务指标 + 后台任务心跳 | Prometheus 能抓到全部自定义指标 | kill 日志监听进程 → `LogListenerDead` 告警 |
| **P1（1 天）** | Prometheus + Alertmanager + 4 条 P0/P1 规则 | 钉钉收到告警 | 手工停 PG 能收到 P0 |
| **P2（2 天）** | Loki + Promtail + Grafana 看板 | 能按 `request_id` 检索、能看到业务漏斗 | 1 分钟内定位一次模拟故障 |
| **P2（1 天）** | 合成监控（Cron + Pushgateway） | 外部拨测指标 | 断网时收到 `SyntheticProbeFailing` |
| **P3（2 天）** | OpenTelemetry + Tempo + 日志注入 trace_id | 全链路可下钻 | 一个请求能看到 FastAPI→PG→Redis→外部服务 |
| **持续** | Runbook / 值班 / 演练 / SLO 复盘 | 运维手册 | 月度演练有记录 |

### 12.2 验收清单

- [ ] `/healthz` 不查依赖、`/readyz` 查依赖且带缓存
- [ ] `/metrics` 含全部 `simplejob_*` 自定义指标，且**无高基数标签**
- [ ] `docker compose ps` 全部 healthy；杀 app 能自动拉起
- [ ] 停 PG/Redis → 告警 1 分钟内到达钉钉
- [ ] 杀日志监听进程 → `LogListenerDead` 触发
- [ ] 邮件机器人超过 3 个周期未成功轮询 → `MailBotDead` 触发
- [ ] 巡检任务停摆 / 连续失败 → `BgTaskNotRunning` / `BgTaskFailing` 触发
- [ ] `daily_times` 模式下**不会**因固定阈值误报（生产环境验证）
- [ ] Grafana 能同时看到 RED、依赖、业务分布、后台任务
- [ ] 合成监控断网/停服可告警
- [ ] 每条告警有 Runbook，关键字已覆盖

---

## 13. 风险与注意事项

| 风险 | 影响 | 应对 |
|---|---|---|
| gunicorn 单 worker | 单点故障；后台任务与 API 同生共死 | 告警按单点设计；liveness 含后台任务心跳；未来拆邮件机器人以支持多 worker |
| 指标高基数（candidate_id/email） | Prometheus 内存暴涨 | 标签白名单审查，CI 加 `promtool check metrics` |
| readiness 直接打依赖 | 探针打爆 DB/Redis | 结果缓存 5s；探针超时 1s；连续失败才判不健康 |
| 日志监听进程静默死亡 | 日志丢失且无感知 | `LOG_LISTENER_ALIVE` + `LOG_QUEUE_SIZE` 双指标 |
| 邮件系统故障时告警丢失 | 告警静默 | P0 走电话/钉钉，不依赖邮件 |
| 告警风暴 | 淹没真实故障 | `group_*` + `inhibit_rules` + `for` 收敛 |
| 外部服务（OCR/AI）限流 | 简历解析积压 | 失败率告警 + 队列/积压指标 + 降级开关 |
| 证书/域名到期 | 站点不可用 | Blackbox 证书到期告警（<7 天） |

---

## 附录 A：新增/修改文件清单

**新增（不改动现有业务逻辑）**

```
app/routers/health_router.py          # 健康检查
app/tasks/scheduler.py                # 调度策略（模式/延迟/最大间隔）
app/tasks/mail_poll_loop.py           # 邮件轮询计时器（替代 APScheduler）
app/core/metrics.py                   # 指标定义 + 后台任务调度健康
app/core/tracing.py                   # OpenTelemetry 初始化
deploy/prometheus/prometheus.yml
deploy/prometheus/alerts.yml
deploy/alertmanager/alertmanager.yml
deploy/alertmanager/templates/dingtalk.tmpl
deploy/grafana/provisioning/datasources/prometheus.yml
deploy/grafana/provisioning/dashboards/default.yml
deploy/grafana/dashboards/simplejob-overview.json
deploy/loki/loki-config.yml
deploy/loki/promtail-config.yml
deploy/blackbox/blackbox.yml
deploy/otel/otel-collector-config.yml
deploy/tempo/tempo-config.yml
docker-compose.monitoring.yml
scripts/ci/synthetics.py
docs/runbook/*.md                     # 每条告警一份
```

**修改（仅插入，不改逻辑）**

```
app/main.py                           # Instrumentator + setup_tracing
app/routers/__init__.py               # 注册 health_router
app/core/lifespan.py                  # _started 标记
app/core/logging_config.py            # TraceIdFilter + trace_id 字段
app/core/http_client.py               # 外部调用埋点
app/core/rate_limit.py                # 限流计数
app/routers/user_router.py            # 登录失败计数
app/workflow/error_handler.py         # 节点失败/耗时
app/tasks/timeout_tasks.py            # heartbeat("email-reply-timeout-sweep")
app/tasks/mail_submission_tasks.py    # heartbeat("mail-submission-sweep")
app/tasks/periodic_task.py            # 复用 scheduler，暴露 _consecutive_failures
app/core/config.py                    # MAIL_POLL_INTERVAL_SECONDS / PERIODIC_TASK_*
utils/emailbot/mail_manager.py        # heartbeat("mail-manager") + 启停计时器
docker/Dockerfile.backend             # HEALTHCHECK 指向 /healthz
docker-compose.yml                    # app.healthcheck
```

## 附录 B：常用排障 PromQL

```promql
# 实时 QPS（按状态）
sum by (status) (rate(http_requests_total[5m]))

# 错误率
sum(rate(http_requests_total{status=~"5.."}[5m]))
  / sum(rate(http_requests_total[5m]))

# 慢接口 Top10（P95）
topk(10, histogram_quantile(0.95,
  sum(rate(http_request_duration_seconds_bucket[5m])) by (le, route)))

# 后台任务是否停摆（阈值跟调度配置联动）
(time() - simplejob_bg_task_last_run_seconds)
  > simplejob_bg_task_expected_interval_seconds * 3

# 后台任务是否在失败
simplejob_bg_task_consecutive_failures

# 日志队列趋势
simplejob_log_queue_size

# 邮件/ OCR 失败率
sum(rate(simplejob_external_request_total{result="error"}[10m])) by (service)
  / sum(rate(simplejob_external_request_total[10m])) by (service)

# 处理异常候选人
simplejob_candidate_status_count{status="处理异常"}

# 合成探测可用性
simplejob_synthetic_probe_success
```
