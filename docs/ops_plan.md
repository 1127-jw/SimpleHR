# 部署 / CI / 集成测试 / 监控 落地实施方案

> 目标：把项目从「能本地跑 + 有 278 个纯逻辑测试」推进到 **可上线、可持续交付、可观测**。
> 对应评分提升：4.3 → **4.6~4.7**

---

## 0. 现状盘点

| 能力 | 现状 | 缺口 |
|---|---|---|
| 部署制品 | 仅 `gunicorn.conf.py` | **Dockerfile / compose / .dockerignore 全部缺失**，文档描述 ≠ 可运行 |
| CI | **无** | 278 个测试无人自动跑 |
| 测试 | 278 个纯逻辑用例（路由/异常/Token/日志分流） | **DB、Redis、HTTP 层 0 覆盖** |
| 健康检查 | 仅 `GET /` 返回 Hello World | 无 liveness / readiness 区分 |
| 指标 | 无 | 无 `/metrics`，无业务指标 |
| 日志 | 已完善（4 个文件 + request_id + 审计） | 部署侧轮转/采集未配置 |
| 告警 | 无 | 故障只能靠翻日志 |

**关键结论**：部署文档写了 1200 行，但一个制品都没落地。所以第一优先级是**把文档里的东西真的建出来并跑通**。

### 0.1 需要新增的依赖

| 依赖 | 用途 | 何时加 |
|---|---|---|
| `ruff`（dev） | lint + format | P0（CI 依赖它） |
| `prometheus-fastapi-instrumentator` | HTTP 指标 | P2 |
| `prometheus-client` | 自定义业务指标（上面那个包会带） | P2 |
| `sentry-sdk`（可选） | 错误追踪 | P3 |

```bash
uv add --dev ruff
uv add prometheus-fastapi-instrumentator      # P2 时再加
```

---

## 1. 部署

### 1.1 目标拓扑（单机版）

```
公网
  │
  ├─ Caddy :443         自动 HTTPS
  │    └─ reverse_proxy → app:8000
  │
  └─ docker compose 内网
       ├─ app         gunicorn(1 worker) + 独立日志监听进程
       ├─ postgres    PG16
       └─ redis       Redis7（AOF）
```

**为什么单机 compose 而不是 K8s**：当前是单实例、无状态服务（除邮件机器人游标），
K8s 的复杂度远大于收益。等有多机/滚动发布需求再演进。

### 1.2 需要新增的制品

| 文件 | 作用 |
|---|---|
| `docker/Dockerfile.backend` | 多阶段构建，`uv sync --frozen --no-dev` |
| `docker-compose.yml` | postgres + redis + app + caddy |
| `.dockerignore` | 排除 `.venv` / `node_modules` / `data` |
| `Caddyfile` | 自动 HTTPS |
| `docker-compose.test.yml` | 集成测试用 PG + Redis（不映射公网端口） |
| `scripts/backup.sh` | pg_dump + data 打包 |
| `scripts/healthcheck.sh` | 容器外探活（可选） |

### 1.3 Dockerfile.backend（要点）

```dockerfile
# ---------- 构建阶段 ----------
FROM python:3.13-slim AS builder
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN pip install --no-cache-dir uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev          # 锁文件必须与 pyproject 同步
COPY . .

# ---------- 运行阶段 ----------
FROM python:3.13-slim AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
        libreoffice-writer-nogui curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /app/.venv /app/.venv
COPY app utils alembic templates ./
COPY alembic.ini gunicorn.conf.py main.py init_data.py check_column.py ./

RUN mkdir -p /app/logs /app/upload /app/attachments

ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 TZ=Asia/Shanghai
WORKDIR /app
EXPOSE 8000

# liveness 探针（不依赖 DB/Redis，见 §4.2）
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1

# 走 gunicorn：日志由 master 统一接管（when_ready 在 fork 之前）
CMD ["gunicorn", "app.main:app", "-c", "gunicorn.conf.py"]
```

**三个容易踩的坑**：

1. `uv sync --frozen` 要求 `uv.lock` 与 `pyproject.toml` 完全同步，
   改了依赖必须提交锁文件。
2. `gunicorn` / `uvicorn-worker` 带 `sys_platform != 'win32'` 标记，
   **在 Linux 镜像里才会装**；本地 Windows 跑不了多进程属预期。
3. `gunicorn.conf.py` 里 `bind = "127.0.0.1:8000"`，**容器内必须改 `0.0.0.0:8000`**，
   否则容器外访问不到。

### 1.4 docker-compose.yml（要点）

```yaml
name: simplejob

services:
  postgres:
    image: postgres:16-alpine
    restart: unless-stopped
    environment:
      POSTGRES_DB: ${DB_NAME:-hr_system}
      POSTGRES_USER: ${DB_USERNAME}
      POSTGRES_PASSWORD: ${DB_PASSWORD:?必须在 .env 中设置}
    volumes: [pg_data:/var/lib/postgresql/data]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${DB_USERNAME} -d ${DB_NAME}"]
      interval: 5s
      timeout: 3s
      retries: 10

  redis:
    image: redis:7-alpine
    restart: unless-stopped
    command: redis-server --appendonly yes
    volumes: [redis_data:/data]
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 10

  app:
    build: { context: ., dockerfile: docker/Dockerfile.backend }
    restart: unless-stopped
    env_file: [.env]
    environment:
      DB_HOST: postgres
      REDIS_HOST: redis
      TZ: Asia/Shanghai
    volumes:
      - ./data/upload:/app/upload
      - ./data/attachments:/app/attachments
      - ./data/logs:/app/logs            # 日志必须出容器
      - ./data/mailbot_state.json:/app/mailbot_state.json
    depends_on:
      postgres: { condition: service_healthy }
      redis:    { condition: service_healthy }
    logging:                              # 防止 json-file 撑爆磁盘
      driver: json-file
      options: { max-size: "20m", max-file: "5" }

  caddy:
    image: caddy:2-alpine
    restart: unless-stopped
    ports: ["80:80", "443:443"]
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy_data:/data
    depends_on: [app]

volumes: { pg_data: , redis_data: , caddy_data: }
```

### 1.5 部署流程（每次都一样）

```bash
# 0) 前置：.env 就绪（chmod 600），data/ 目录存在
mkdir -p data/upload data/attachments data/logs
touch data/mailbot_state.json          # 必须是文件，不能是目录

# 1) 起基础设施
docker compose up -d postgres redis

# 2) 构建
docker compose build app

# 3) 迁移：单独跑，不要在容器启动时自动迁移
docker compose run --rm app uv run alembic upgrade head
docker compose run --rm app uv run python init_data.py   # 幂等，可重跑

# 4) 起应用
docker compose up -d app caddy

# 5) 验证
curl -fsS https://hr.example.com/healthz
curl -fsS https://hr.example.com/readyz
docker compose ps
docker compose exec app tail -n 50 logs/workflow.log
```

**为什么迁移不放进 entrypoint**：多副本时会让多个容器同时迁移；而且迁移失败
会导致容器反复重启。显式执行 + 失败可见更安全。

### 1.6 上线检查清单

- [ ] `.env` 权限 600，`ACCESS_SECRET` / `REFRESH_SECRET` ≥ 32 字符（启动会校验）
- [ ] `DEBUG=False`（生产）
- [ ] `db` / `redis` 端口**不映射**到宿主机
- [ ] `data/logs` 卷挂载生效，`audit.log` 有内容
- [ ] 备份脚本进 cron，且**实际恢复验证过一次**
- [ ] `/healthz` 返回 200，`/readyz` 在依赖健康时返回 200
- [ ] 重启策略生效：`docker kill app` 后能自动拉起
- [ ] 日志轮转生效：`logs/` 目录不会无限增长

---

## 2. CI

### 2.1 目标

每次 push / PR 自动执行：**lint → 类型检查 → 单元测试 → 构建镜像**。
不通过则禁止合并。

### 2.2 一个必须先解决的坑

`Settings` 有启动校验：`ACCESS_SECRET` / `REFRESH_SECRET` **不能为空**。
CI 环境没有 `.env`，`get_settings()` 会直接抛异常，**测试全挂**。

所以 workflow 里必须先注入 dummy 密钥：

```yaml
env:
  ACCESS_SECRET: ci-only-access-secret-0123456789abcdef
  REFRESH_SECRET: ci-only-refresh-secret-0123456789abcdef
  DEBUG: "True"        # 让 <32 字符的校验不生效（同时仍要求非空）
```

> 这两个值只用于 CI，不涉及任何真实环境。

### 2.3 `.github/workflows/ci.yml`

```yaml
name: ci

on:
  push: { branches: [main, master] }
  pull_request:

concurrency:
  group: ci-${{ github.ref }}
  cancel-in-progress: true

env:
  ACCESS_SECRET: ci-only-access-secret-0123456789abcdef
  REFRESH_SECRET: ci-only-refresh-secret-0123456789abcdef
  DEBUG: "True"

jobs:
  lint-and-test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: astral-sh/setup-uv@v5
        with: { enable-cache: true }

      - run: uv sync --frozen

      - name: Lint
        run: uv run ruff check .

      - name: Format check
        run: uv run ruff format --check .

      - name: Unit tests
        run: uv run pytest -q

  integration-test:
    runs-on: ubuntu-latest
    needs: lint-and-test          # 单测过了再跑慢的
    services:
      postgres:
        image: postgres:16-alpine
        env:
          POSTGRES_USER: test
          POSTGRES_PASSWORD: test
          POSTGRES_DB: test_db
        ports: ["5432:5432"]
        options: >-
          --health-cmd "pg_isready -U test"
          --health-interval 5s --health-timeout 3s --health-retries 10
      redis:
        image: redis:7-alpine
        ports: ["6379:6379"]
        options: >-
          --health-cmd "redis-cli ping"
          --health-interval 5s --health-timeout 3s --health-retries 10
    env:
      DB_HOST: 127.0.0.1
      DB_PORT: "5432"
      DB_USERNAME: test
      DB_PASSWORD: test
      DB_NAME: test_db
      REDIS_HOST: 127.0.0.1
      REDIS_PORT: "6379"
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
        with: { enable-cache: true }
      - run: uv sync --frozen
      - run: uv run alembic upgrade head
      - run: uv run pytest -q -m integration

  docker-build:
    runs-on: ubuntu-latest
    needs: lint-and-test
    steps:
      - uses: actions/checkout@v4
      - uses: docker/setup-buildx-action@v3
      - name: Build image
        uses: docker/build-push-action@v6
        with:
          context: .
          file: docker/Dockerfile.backend
          push: false
          cache-from: type=gha
          cache-to: type=gha,mode=max
```

### 2.4 分阶段落地

| 阶段 | 内容 | 耗时 | 收益 |
|---|---|---|---|
| **P0** | 只加 `lint-and-test`（ruff + pytest） | 半天 | 278 个测试立刻有了守门人 |
| P1 | 加 `docker-build` | 半天 | 镜像构建失败提前暴露 |
| P2 | 加 `integration-test` | 依赖 §3 | 覆盖 DB/Redis 层 |
| P3 | 加 `ruff format --check`、`mypy` | 半天 | 风格与类型一致性 |

> **不要一开始就上 mypy**：这个项目有大量 `Any` 和未标注函数，硬上会一次爆几百个错误。
> 建议先 `ruff`，稳定后再逐个模块补类型标注。

### 2.5 本地等价命令

```bash
uv run ruff check .
uv run pytest
uv run pytest -m integration      # 需要先起 docker-compose.test.yml
```

---

## 3. 集成测试

### 3.1 为什么必须做

当前 278 个测试覆盖的是**纯函数**。以下三个最易错的点完全没覆盖：

| 场景 | 风险 |
|---|---|
| 列表分页 `total` | 之前就出过「双重切片」bug，纯逻辑测不出来 |
| 候选人可见性 | superuser / hr / 普通成员三套 SQL 条件，`_visible_stmt` 写错就是越权 |
| `init_data` 幂等 | 重复执行会插重复数据，只能靠真跑发现 |

### 3.2 技术选型

| 方案 | 优点 | 缺点 | 结论 |
|---|---|---|---|
| **真实 PG + Redis（service container）** | 与生产同构，无方言差异 | 需要容器 | ✅ **采用** |
| SQLite + aiosqlite | 快、无依赖 | 方言差异（`func.date`、`IN` 语义、并发） | ❌ 测试会「假绿」 |
| testcontainers-python | 本地零配置 | 多一个依赖，CI 里仍是起容器 | 可选 |

**决定**：用 `docker-compose.test.yml` 提供 PG + Redis，CI 用 `services:` 块，
两处环境变量完全一致。

```yaml
# docker-compose.test.yml
name: simplejob-test
services:
  postgres-test:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: test
      POSTGRES_PASSWORD: test
      POSTGRES_DB: test_db
    ports: ["5433:5432"]
  redis-test:
    image: redis:7-alpine
    ports: ["6380:6379"]
```

### 3.3 隔离策略

**每个测试用独立 schema，跑完 DROP**：

```python
# tests/integration/conftest.py
import os
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text

from app.core.db import Base
from app.core.config import get_settings

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def db_engine():
    """每次测试一个独立 schema，避免相互污染。"""
    settings = get_settings()
    engine = create_async_engine(settings.database_url)

    schema = f"test_{uuid4().hex[:8]}"
    async with engine.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))

    # 把 search_path 指到新 schema，建表只影响这里
    async with engine.begin() as conn:
        await conn.execute(text(f'SET search_path TO "{schema}"'))
        await conn.run_sync(Base.metadata.create_all)

    yield engine

    async with engine.begin() as conn:
        await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    await engine.dispose()
```

> 用 schema 隔离而不是「每次 drop 全库」：更快，且不会误伤同实例的其它库。

### 3.4 必须覆盖的场景

| # | 场景 | 断言 |
|---|---|---|
| 1 | 候选人列表分页 | `page=2` 能拿到数据；`total` 等于真实总数 |
| 2 | 员工列表分页 | 同上（这是修过的双重切片 bug） |
| 3 | 可见性：superuser | 能看到所有人的候选人 |
| 4 | 可见性：hr | 只能看到所负责部门的 |
| 5 | 可见性：普通成员 | 只能看到自己发布职位的 |
| 6 | hr 无负责部门 | 返回空列表且 `total=0`（不能 500） |
| 7 | 简历下载权限 | 越权返回 404/403，本人 200 |
| 8 | 登录限流 | 第 6 次请求返回 429 |
| 9 | `init_data` 幂等 | 连跑两次，部门/用户数量不变 |
| 10 | 审计日志 | 登录成功/失败各写一条 `audit.log` |

### 3.5 用例示例

```python
# tests/integration/test_pagination.py
async def test_candidate_list_second_page_is_not_empty(client, seed_candidates):
    """回归：曾经因为仓储已分页、router 又切一次，第 2 页恒为空。"""
    resp = await client.get("/candidate/list", params={"page": 2, "size": 10})
    body = resp.json()

    assert resp.status_code == 200
    assert len(body["candidates"]) > 0
    assert body["total"] == 25          # 种子数据总数


async def test_user_list_total_is_real_count(client, seed_users):
    resp = await client.get("/user/list", params={"page": 1, "size": 5})
    body = resp.json()

    assert len(body["items"]) == 5
    assert body["total"] == 12          # 不是当前页条数
```

```python
# tests/integration/test_permissions.py
async def test_hr_cannot_see_other_department_candidate(client_as_hr, other_dept_candidate):
    resp = await client_as_hr.get(f"/candidate/{other_dept_candidate.id}/resume")
    assert resp.status_code == 404      # 不暴露「存在但无权」
```

### 3.6 与单元测试的边界

| | 单元测试（现有 278 个） | 集成测试 |
|---|---|---|
| 依赖 | 无（纯内存） | PG + Redis |
| 速度 | 3 秒 | 30~60 秒 |
| 标记 | 默认 | `@pytest.mark.integration` |
| 运行 | `pytest` | `pytest -m integration` |

`pyproject.toml` 补：

```toml
[tool.pytest.ini_options]
markers = ["integration: 需要 PG + Redis 的集成测试"]
addopts = "-q -p no:cacheprovider -m 'not integration'"
```

> 默认**不跑**集成测试，保证 `pytest` 依然 3 秒；CI 用 `-m integration` 单独跑。

---

## 4. 监控

### 4.1 三层模型

```
日志（已有）  →  指标（要补）  →  告警（要补）
   │                │                │
排查细节         看趋势/容量       主动通知
```

### 4.2 健康检查（最基础的监控）

现在的 `GET /` 返回 `Hello World`，**不代表服务可用**。
需要拆成两个语义不同的端点：

```python
# app/routers/health_router.py
from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from sqlalchemy import text
from redis.asyncio import Redis

from app.core.db import AsyncSessionFactory
from app.core.redis_client import get_redis

router = APIRouter(tags=["health"])


@router.get("/healthz", summary="存活探针")
async def healthz():
    """只证明进程活着。**不查依赖**——依赖挂了不应触发容器重启。"""
    return {"status": "ok"}


@router.get("/readyz", summary="就绪探针")
async def readyz(r: Redis = Depends(get_redis)):
    """依赖可用才返回 200。用于流量接入判定。"""
    checks: dict[str, str] = {}

    try:
        async with AsyncSessionFactory() as session:
            await session.execute(text("SELECT 1"))
        checks["postgres"] = "ok"
    except Exception as e:
        checks["postgres"] = f"error: {type(e).__name__}"

    try:
        await r.ping()
        checks["redis"] = "ok"
    except Exception as e:
        checks["redis"] = f"error: {type(e).__name__}"

    healthy = all(v == "ok" for v in checks.values())
    return JSONResponse(
        status_code=status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"status": "ok" if healthy else "degraded", "checks": checks},
    )
```

**为什么必须分开**：如果用 readiness 做 liveness，DB 抖动会导致容器被反复重启，
反而放大故障。

### 4.3 指标（Prometheus）

引入 `prometheus-fastapi-instrumentator`（一个依赖，自带 HTTP 指标）：

```python
# app/main.py
from prometheus_fastapi_instrumentator import Instrumentator

Instrumentator().instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)
```

自带指标：请求总数、延迟分位、状态码分布、在途请求数。

**业务指标**（手写，价值高于 HTTP 指标）：

```python
# app/core/metrics.py
from prometheus_client import Counter, Gauge

LOGIN_FAILED = Counter("simplejob_login_failed_total", "登录失败次数")
RATE_LIMITED = Counter("simplejob_rate_limited_total", "限流触发次数")
RESUME_DOWNLOAD = Counter("simplejob_resume_download_total", "简历下载次数")
WORKFLOW_STAGE = Counter(
    "simplejob_workflow_stage_total", "工作流各阶段完成次数", ["stage"]
)

# 关键：日志队列积压（监听进程跟不上时会持续增长）
LOG_QUEUE_SIZE = Gauge("simplejob_log_queue_size", "日志队列积压条数")
LOG_LISTENER_ALIVE = Gauge("simplejob_log_listener_alive", "日志监听进程是否存活")
```

埋点位置：

| 指标 | 埋点 |
|---|---|
| `LOGIN_FAILED` | `user_router.login` 的 `except HTTPException` 分支 |
| `RATE_LIMITED` | `rate_limit.enforce` 抛 429 前 |
| `RESUME_DOWNLOAD` | 简历下载接口 |
| `WORKFLOW_STAGE` | `node_error_wrapper` / 各节点返回处 |
| `LOG_QUEUE_SIZE` | 定时任务每 15s 采样 `logging_config._LOG_QUEUE.qsize()` |
| `LOG_LISTENER_ALIVE` | 同上，检查 `_LISTENER_PROCESS.is_alive()` |

> `LOG_QUEUE_SIZE` 是这个项目**特有**的指标——日志监听进程挂掉时业务不会报错，
> 只会看到队列持续增长。没有这个指标，故障是静默的。

### 4.4 告警规则

```yaml
# deploy/prometheus/alerts.yml
groups:
  - name: simplejob
    rules:
      - alert: ServiceDown
        expr: up{job="simplejob"} == 0
        for: 1m
        labels: { severity: critical }
        annotations: { summary: "服务不可达" }

      - alert: HighErrorRate
        expr: |
          sum(rate(http_requests_total{status=~"5.."}[5m]))
          / sum(rate(http_requests_total[5m])) > 0.05
        for: 5m
        labels: { severity: warning }
        annotations: { summary: "5xx 比例超过 5%" }

      - alert: P95LatencyHigh
        expr: histogram_quantile(0.95, sum(rate(http_request_duration_seconds_bucket[5m])) by (le)) > 2
        for: 10m
        labels: { severity: warning }

      - alert: LogListenerDead
        expr: simplejob_log_listener_alive == 0
        for: 2m
        labels: { severity: critical }
        annotations: { summary: "日志监听进程已退出，日志只会堆在队列里" }

      - alert: LogQueueBacklog
        expr: simplejob_log_queue_size > 10000
        for: 5m
        labels: { severity: warning }
        annotations: { summary: "日志队列积压超过 1 万条，检查磁盘 IO" }

      - alert: LoginBruteForce
        expr: increase(simplejob_login_failed_total[5m]) > 50
        for: 0m
        labels: { severity: warning }
        annotations: { summary: "5 分钟内登录失败超过 50 次，疑似撞库" }

      - alert: DiskUsageHigh
        expr: (node_filesystem_avail_bytes{mountpoint="/"} / node_filesystem_size_bytes{mountpoint="/"}) < 0.15
        for: 10m
        labels: { severity: warning }
```

### 4.5 日志采集（可选，第二阶段）

当前日志落盘到 `data/logs/`。单机阶段足够；要检索/聚合时：

- **轻量**：Loki + Promtail（Grafana 全家桶，资源占用小）
- **重量**：ELK

MVP 不需要，先把 `/metrics` + 告警做起来。

### 4.6 错误追踪（可选）

Sentry SDK 10 行接入，能拿到完整堆栈 + request_id 关联：

```python
import sentry_sdk
sentry_sdk.init(dsn=os.getenv("SENTRY_DSN"), traces_sample_rate=0.1)
```

对「LLM 调用失败」「钉钉 API 异常」这类第三方故障排查帮助很大。

---

## 5. 排期与优先级

| 阶段 | 内容 | 工时 | 完成标准 |
|---|---|---:|---|
| **P0** | 部署制品（Dockerfile / compose / .dockerignore / Caddyfile） | 1 天 | `docker compose up -d` 后可访问 |
| **P0** | CI：`lint-and-test` job | 0.5 天 | PR 自动跑 ruff + pytest |
| **P1** | 健康检查 `/healthz` + `/readyz` | 0.5 天 | 探针能区分「进程活着」和「依赖可用」 |
| **P1** | 集成测试基建 + 10 个核心用例 | 1.5 天 | `pytest -m integration` 全绿 |
| **P1** | CI 加 `integration-test` + `docker-build` | 0.5 天 | 三个 job 全绿才允许合并 |
| **P2** | `/metrics` + 业务指标埋点 | 1 天 | `/metrics` 能抓到所有自定义指标 |
| **P2** | Prometheus + 4 条核心告警 | 0.5 天 | 拔掉 Redis 能收到 `ReadyDegraded` |
| **P3** | Loki 日志聚合 / Sentry | 1 天 | 可选 |

**合计约 6 人天**。如果只能先做两件：**P0 部署 + P0 CI**，一天半，收益最大。

---

## 6. 验收标准

### 部署
- [ ] 全新机器上按文档能在 30 分钟内跑起来
- [ ] `docker compose ps` 全部 healthy
- [ ] HTTPS 可访问，证书自动续期
- [ ] 杀掉 app 容器能自动恢复

### CI
- [ ] push 后 5 分钟内出结果
- [ ] 故意改坏一个断言，CI 能红
- [ ] 依赖变更未提交 `uv.lock` 时 CI 报错

### 集成测试
- [ ] `pytest` 默认仍 3 秒左右（不跑集成）
- [ ] `pytest -m integration` 全绿
- [ ] 故意把 `get_list` 的 `total` 改成 `len(rows)`，测试能红

### 监控
- [ ] `/healthz` 与 `/readyz` 语义分离
- [ ] `/metrics` 包含全部自定义指标
- [ ] 手工停掉 Redis，`/readyz` 返回 503 且触发告警
- [ ] 手工 kill 日志监听进程，`LogListenerDead` 告警能在 2 分钟内触发

---

## 7. 风险与应对

| 风险 | 影响 | 应对 |
|---|---|---|
| CI 无密钥导致测试全挂 | 首次接入就红 | 在 workflow `env` 注入 dummy 密钥（§2.2） |
| 集成测试拖慢 CI | 反馈变慢 | 拆 job + 只在 PR 触发 + 缓存 uv |
| 迁移时机错误 | 线上表结构不一致 | 迁移只走显式命令，不进 entrypoint |
| 健康检查拖垮服务 | DB 抖动引发雪崩 | liveness 不查依赖 |
| 集成测试污染开发库 | 数据丢失 | 独立 schema + 测试专用库名 |
| 指标基数爆炸 | Prometheus 内存暴涨 | 不给 `candidate_id` 这类高基数字段打标签 |

---

## 8. 一句话总结

**先做 §1 和 §2.3 的 `lint-and-test`** —— 一天半，把「能上线」和「有人守门」这两件事解决掉，
这是从 4.3 到 4.6 的关键两步；集成测试和监控按 P1/P2 继续推进即可到 4.7。
