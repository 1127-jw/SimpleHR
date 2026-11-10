# FastAPI 接口限流与并发控制方案

## 一、总体架构

```
客户端请求
    │
    ▼
┌─────────────────────────────────────────────────────────┐
│  Nginx（网关层）                                         │
│  ├── 全局限流：每 IP 100 r/s                            │
│  ├── 并发限制：每 IP 50 并发连接                          │
│  └── 接口级限流：登录 1 r/s，注册 0.2 r/s                 │
└─────────────────────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────────────────────┐
│  Uvicorn（进程层）                                       │
│  ├── Workers: 4                                          │
│  ├── 每 Worker 并发限制: 100                             │
│  └── Worker 连接数: 1000                                 │
└─────────────────────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────────────────────┐
│  FastAPI（应用层）                                       │
│  ├── SlowAPI：按用户/接口限流                             │
│  ├── 并发控制中间件：全局信号量                           │
│  └── 每 IP 并发限制：10                                   │
└─────────────────────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────────────────────┐
│  数据库（资源层）                                        │
│  └── 连接池: 10 + 溢出 20 = 最大 30                      │
└─────────────────────────────────────────────────────────┘
```

---

## 二、Nginx 层限流（第一道防线）

### 2.1 配置

```nginx
# /etc/nginx/conf.d/app.conf

# 定义限流区域
limit_req_zone $binary_remote_addr zone=general:10m rate=100r/s;
limit_req_zone $binary_remote_addr zone=login:10m rate=1r/s;
limit_req_zone $binary_remote_addr zone=register:10m rate=0.2r/s;
limit_conn_zone $binary_remote_addr zone=addr:10m;

server {
    listen 80;
    server_name api.example.com;

    # 全局限流：每 IP 每秒 100 请求
    location /api/ {
        limit_req zone=general burst=200 nodelay;
        limit_conn addr 50;  # 每 IP 最多 50 并发连接
        proxy_pass http://127.0.0.1:8000;
    }

    # 登录接口：每 IP 每秒 1 请求
    location /api/user/token {
        limit_req zone=login burst=5 nodelay;
        proxy_pass http://127.0.0.1:8000;
    }

    # 注册接口：每 IP 每 5 秒 1 请求
    location /api/user/register {
        limit_req zone=register burst=3 nodelay;
        proxy_pass http://127.0.0.1:8000;
    }
}
```

### 2.2 参数说明

| 参数 | 说明 | 推荐值 |
|------|------|--------|
| `rate` | 每秒允许请求数 | 100 r/s |
| `burst` | 突发缓冲数量 | 200 |
| `nodelay` | 不延迟处理突发请求 | 启用 |
| `limit_conn` | 每 IP 并发连接数 | 50 |

---

## 三、Uvicorn 进程层限制

### 3.1 启动参数

```bash
# 多进程 + 并发限制
uvicorn app.main:app \
    --workers 4 \
    --limit-concurrency 100 \
    --backlog 2048 \
    --timeout-keep-alive 30
```

### 3.2 使用 Gunicorn（生产推荐）

```bash
gunicorn app.main:app \
    -w 4 \
    -k uvicorn.workers.UvicornWorker \
    --max-requests 1000 \
    --max-requests-jitter 100 \
    --worker-connections 1000 \
    --bind 0.0.0.0:8000
```

### 3.3 参数说明

| 参数 | 说明 | 推荐值 |
|------|------|--------|
| `--workers` | 工作进程数 | CPU 核数 × 2 |
| `--limit-concurrency` | 每 worker 并发连接上限 | 100 |
| `--worker-connections` | 每 worker 最大连接数 | 1000 |
| `--max-requests` | 进程自动重启阈值 | 1000 |

---

## 四、SlowAPI 应用层限流

### 4.1 安装

```bash
pip install slowapi
```

### 4.2 配置

```python
# app/core/rate_limit.py
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

def get_user_key(request):
    """已登录按用户 ID，未登录按 IP"""
    user = getattr(request.state, 'user', None)
    if user:
        return f"user:{user.id}"
    return get_remote_address(request)

limiter = Limiter(key_func=get_user_key)

def init_rate_limit(app):
    """初始化限流"""
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
```

### 4.3 使用

```python
# app/main.py
from app.core.rate_limit import limiter, init_rate_limit

app = FastAPI()
init_rate_limit(app)

# 登录接口：每分钟 5 次
@app.post("/user/token")
@limiter.limit("5/minute")
async def login(request: Request, data: LoginData):
    ...

# 注册接口：每小时 3 次
@app.post("/user/register")
@limiter.limit("3/hour")
async def register(request: Request, data: RegisterData):
    ...

# 通用接口：每秒 10 次
@app.get("/candidate/list")
@limiter.limit("10/second")
async def get_candidates(request: Request):
    ...

# 严格接口：每分钟 1 次
@app.post("/candidate/create")
@limiter.limit("1/minute")
async def create_candidate(request: Request):
    ...
```

---

## 五、自定义并发控制中间件

### 5.1 全局并发限制

```python
# app/core/concurrency_limit.py
import asyncio
from fastapi import Request, HTTPException
from starlette.middleware.base import BaseHTTPMiddleware

class ConcurrencyLimitMiddleware(BaseHTTPMiddleware):
    """全局并发请求限制"""

    def __init__(self, app, max_concurrent: int = 100):
        super().__init__(app)
        self.semaphore = asyncio.Semaphore(max_concurrent)
        self.active_count = 0
        self.max_concurrent = max_concurrent

    async def dispatch(self, request: Request, call_next):
        if self.active_count >= self.max_concurrent:
            raise HTTPException(
                status_code=503,
                detail=f"服务器繁忙，当前并发：{self.active_count}/{self.max_concurrent}"
            )

        async with self.semaphore:
            self.active_count += 1
            try:
                response = await call_next(request)
                return response
            finally:
                self.active_count -= 1
```

### 5.2 每 IP 并发限制

```python
# app/core/ip_concurrency.py
import asyncio
from fastapi import Request, HTTPException
from starlette.middleware.base import BaseHTTPMiddleware

class PerIPLimitMiddleware(BaseHTTPMiddleware):
    """每 IP 并发请求限制"""

    def __init__(self, app, max_per_ip: int = 10):
        super().__init__(app)
        self.max_per_ip = max_per_ip
        self.ip_connections: dict[str, int] = {}
        self.lock = asyncio.Lock()

    async def dispatch(self, request: Request, call_next):
        client_ip = request.client.host

        async with self.lock:
            current = self.ip_connections.get(client_ip, 0)
            if current >= self.max_per_ip:
                raise HTTPException(
                    status_code=429,
                    detail=f"IP {client_ip} 并发数超限：{current}/{self.max_per_ip}"
                )
            self.ip_connections[client_ip] = current + 1

        try:
            response = await call_next(request)
            return response
        finally:
            async with self.lock:
                self.ip_connections[client_ip] -= 1
                if self.ip_connections[client_ip] <= 0:
                    del self.ip_connections[client_ip]
```

### 5.3 注册中间件

```python
# app/main.py
from app.core.concurrency_limit import ConcurrencyLimitMiddleware
from app.core.ip_concurrency import PerIPLimitMiddleware

app = FastAPI()

# 全局并发限制：最多 100 个并发请求
app.add_middleware(ConcurrencyLimitMiddleware, max_concurrent=100)

# 每 IP 并发限制：每 IP 最多 10 个并发
app.add_middleware(PerIPLimitMiddleware, max_per_ip=10)
```

---

## 六、数据库连接池限制

```python
# app/core/db.py
engine = create_async_engine(
    settings.database_url,
    pool_size=10,           # 常驻连接数
    max_overflow=20,        # 最大溢出连接数
    pool_timeout=30,        # 获取连接超时（秒）
    pool_recycle=3600,      # 连接回收时间（秒）
    pool_pre_ping=True,     # 连接前检测
)
```

**最大并发数据库操作 = pool_size + max_overflow = 30**

---

## 七、完整配置汇总

| 层级 | 配置项 | 推荐值 | 说明 |
|------|--------|--------|------|
| **Nginx** | `rate` | 100 r/s | 每 IP 每秒请求数 |
| | `burst` | 200 | 突发缓冲 |
| | `limit_conn` | 50 | 每 IP 并发连接数 |
| **Uvicorn** | `--workers` | 4 | 工作进程数 |
| | `--limit-concurrency` | 100 | 每 worker 并发上限 |
| **SlowAPI** | 登录 | 5/minute | 按接口配置 |
| | 注册 | 3/hour | 按接口配置 |
| | 通用 | 10/second | 按接口配置 |
| **并发中间件** | 全局并发 | 100 | 信号量控制 |
| | 每 IP 并发 | 10 | 按 IP 控制 |
| **数据库** | 连接池 | 10 + 20 | 最大 30 连接 |

---

## 八、压力测试建议

### 8.1 测试工具

```bash
# 使用 wrk 进行压力测试
wrk -t12 -c400 -d30s http://localhost:8000/api/candidate/list

# 使用 locust 进行更复杂的测试
pip install locust
locust -f locustfile.py --host=http://localhost:8000
```

### 8.2 监控指标

```python
# 添加 Prometheus 监控
from prometheus_client import Counter, Histogram

request_count = Counter('http_requests_total', 'Total requests', ['method', 'endpoint', 'status'])
request_latency = Histogram('http_request_duration_seconds', 'Request latency')

@app.middleware("http")
async def monitor_requests(request: Request, call_next):
    start = time.time()
    response = await call_next(request)
    duration = time.time() - start
    
    request_count.labels(
        method=request.method,
        endpoint=request.url.path,
        status=response.status_code
    ).inc()
    request_latency.observe(duration)
    
    return response
```

---

## 九、降级策略

当触发限流时，返回友好的错误信息：

```python
# 429 Too Many Requests
{
    "detail": "请求过于频繁，请稍后再试",
    "retry_after": 60
}

# 503 Service Unavailable
{
    "detail": "服务器繁忙，请稍后再试",
    "current_load": "85/100"
}
```

---

## 十、实施建议

### 阶段 1：基础保护（立即实施）

1. 配置 Nginx 限流
2. 设置 Uvicorn workers 和并发限制
3. 配置数据库连接池

### 阶段 2：应用层限流（1-2 天）

1. 集成 SlowAPI
2. 为敏感接口添加限流装饰器
3. 添加自定义并发中间件

### 阶段 3：监控和优化（持续）

1. 添加 Prometheus 监控
2. 压力测试验证
3. 根据实际负载调整参数
