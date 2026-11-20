# Docker 部署方案

> 适用：生产环境容器化部署。编排 `后端 + 前端 + PostgreSQL + Redis`。

---

## 一、架构

```
                    ┌────────────────────────────┐
   HTTPS  ────────▶ │  Nginx (前端静态 + API 反代) │
                    └─────────────┬──────────────┘
                                  │
              ┌───────────────────┼───────────────────┐
              ▼                   ▼                   ▼
      ┌──────────────┐    ┌──────────────┐    ┌──────────────┐
      │  frontend    │    │  backend     │    │  backend     │
      │  (Nginx)     │    │  (FastAPI)   │    │  (worker)    │
      └──────────────┘    └──────┬───────┘    └──────────────┘
                                 │
                    ┌────────────┼────────────┐
                    ▼            ▼            ▼
              ┌──────────┐ ┌──────────┐ ┌──────────┐
              │ Postgres │ │  Redis   │ │ 卷: upload│
              └──────────┘ └──────────┘ └──────────┘
```

| 服务 | 镜像 | 端口 |
|------|------|------|
| frontend | `docker/Dockerfile.frontend` | 80 / 443 |
| backend | `docker/Dockerfile.backend` | 8000（仅内网） |
| postgres | `postgres:16-alpine` | 5432（仅内网） |
| redis | `redis:7-alpine` | 6379（仅内网） |

---

## 二、文件

```
deploy/
├── docker-compose.yml
├── docker/
│   ├── Dockerfile.backend
│   ├── Dockerfile.frontend
│   └── nginx.conf
├── .env                       # 从项目根 .env 复制
└── scripts/init-db.sh
```

---

## 三、后端镜像 `docker/Dockerfile.backend`

```dockerfile
FROM python:3.13-slim

WORKDIR /app

# 安装 uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# 先复制依赖清单，利用缓存
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# 复制源码
COPY app ./app
COPY utils ./utils
COPY alembic ./alembic
COPY alembic.ini gunicorn.conf.py init_data.py scripts/demo_seed.py ./

# 数据卷
VOLUME ["/app/upload", "/app/attachments", "/app/logs"]

ENV PYTHONUNBUFFERED=1
EXPOSE 8000

CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

---

## 四、前端镜像 `docker/Dockerfile.frontend`

```dockerfile
# 构建阶段
FROM node:24-alpine AS build
WORKDIR /app
COPY hr-frontend/package.json hr-frontend/package-lock.json* ./
RUN npm install
COPY hr-frontend/ ./
RUN npm run build

# 运行阶段
FROM nginx:1.27-alpine
COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80 443
```

---

## 五、编排 `deploy/docker-compose.yml`

```yaml
services:
  postgres:
    image: postgres:16-alpine
    restart: unless-stopped
    environment:
      POSTGRES_USER: ${DB_USERNAME}
      POSTGRES_PASSWORD: ${DB_PASSWORD}
      POSTGRES_DB: ${DB_NAME}
    volumes:
      - pg_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${DB_USERNAME}"]
      interval: 10s
      timeout: 5s
      retries: 5
    networks: [internal]

  redis:
    image: redis:7-alpine
    restart: unless-stopped
    volumes:
      - redis_data:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      timeout: 3s
      retries: 5
    networks: [internal]

  backend:
    build:
      context: ..
      dockerfile: deploy/docker/Dockerfile.backend
    restart: unless-stopped
    env_file: .env
    depends_on:
      postgres: { condition: service_healthy }
      redis: { condition: service_healthy }
    volumes:
      - ../upload:/app/upload
      - ../attachments:/app/attachments
      - ../logs:/app/logs
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/"]
      interval: 30s
      timeout: 5s
      retries: 3
    networks: [internal]

  frontend:
    build:
      context: ..
      dockerfile: deploy/docker/Dockerfile.frontend
    restart: unless-stopped
    ports:
      - "80:80"
      - "443:443"
    depends_on:
      - backend
    networks: [internal]

volumes:
  pg_data:
  redis_data:

networks:
  internal:
    driver: bridge
```

---

## 六、部署步骤

```bash
# 1. 准备环境变量
cp .env deploy/.env
# 编辑 deploy/.env，确认 DB_* / REDIS_* / MAIL_* / DASHSCOPE_API_KEY 等

# 2. 构建并启动
cd deploy
docker compose up -d --build

# 3. 初始化数据库（首次）
docker compose exec backend uv run alembic upgrade head
docker compose exec backend uv run python init_data.py
docker compose exec backend uv run python -m scripts.demo_seed   # 可选演示数据

# 4. 查看状态
docker compose ps
docker compose logs -f backend
```

---

## 七、HTTPS（Caddy 自动证书）

可选：把 `frontend` 换成 Caddy 自动申请证书。

```yaml
  frontend:
    image: caddy:2-alpine
    ports: ["80:80", "443:443"]
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile
      - caddy_data:/data
```

`Caddyfile`：
```
your-domain.com {
    reverse_proxy backend:8000
    root * /usr/share/nginx/html
    file_server
}
```

---

## 八、数据持久化

| 卷 | 内容 |
|----|------|
| `pg_data` | PostgreSQL 数据 |
| `redis_data` | Redis 数据 |
| `../upload` | 简历文件 |
| `../attachments` | 邮件附件 |
| `../logs` | 日志 |

---

## 九、升级流程

```bash
cd deploy
docker compose pull
docker compose up -d --build
docker compose exec backend uv run alembic upgrade head
docker compose exec backend uv run python init_data.py
```

---

## 十、注意事项

1. **邮件机器人约束**：`MailManager` 在 lifespan 启动，**backend 不要多副本**（单 worker）。
2. **外部服务**：LLM / OCR / 钉钉 / 邮箱 需提前就绪。
3. **域名**：钉钉 OAuth 回调需公网 HTTPS。
4. **资源**：建议 ≥ 2C4G，Postgres 独立磁盘。
