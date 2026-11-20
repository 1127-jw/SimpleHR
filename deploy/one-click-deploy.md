# 一键部署方案

> 适用：单台服务器，一条命令完成部署 / 升级。

---

## 一、两种一键方式

| 方式 | 命令 | 适用 |
|------|------|------|
| **A. Docker Compose** | `./scripts/deploy.sh docker` | 推荐，环境隔离 |
| **B. 裸机 systemd** | `./scripts/deploy.sh bare` | 无 Docker 环境 |

---

## 二、方式 A：Docker Compose 一键部署

### 2.1 前置

```bash
# 服务器已安装 Docker + Compose
docker --version
docker compose version
```

### 2.2 一键命令

```bash
git clone <repo> /opt/simplejob
cd /opt/simplejob/deploy
cp ../.env .env          # 编辑配置
./scripts/deploy.sh docker
```

### 2.3 脚本 `scripts/deploy.sh`

```bash
#!/usr/bin/env bash
# 一键部署脚本（Docker 模式）
set -euo pipefail

MODE="${1:-docker}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/deploy"

echo "==> 1/6 加载环境变量"
[ -f .env ] || { echo "缺少 deploy/.env，请先从项目根 .env 复制"; exit 1; }
set -a; . ./.env; set +a

echo "==> 2/6 构建镜像"
docker compose build

echo "==> 3/6 启动依赖（PG + Redis）"
docker compose up -d postgres redis
sleep 5

echo "==> 4/6 数据库迁移 + 种子数据"
docker compose run --rm backend uv run alembic upgrade head
docker compose run --rm backend uv run python init_data.py

echo "==> 5/6 启动全部服务"
docker compose up -d

echo "==> 6/6 健康检查"
sleep 5
curl -fsS http://localhost:8000/health || curl -fsS http://127.0.0.1:8000/ || true
docker compose ps
echo "✅ 部署完成"
```

---

## 三、方式 B：裸机 systemd 一键部署

### 3.1 一键命令

```bash
./scripts/deploy.sh bare
```

### 3.2 脚本（bare 分支）

```bash
deploy_bare() {
  echo "==> 1/5 安装后端依赖"
  cd "$ROOT"
  uv sync --frozen

  echo "==> 2/5 构建前端"
  cd hr-frontend
  npm ci
  npm run build-only
  cd "$ROOT"

  echo "==> 3/5 数据库迁移"
  uv run alembic upgrade head
  uv run python init_data.py

  echo "==> 4/5 部署前端静态文件到 Nginx"
  sudo rsync -a --delete hr-frontend/dist/ /var/www/simplejob/

  echo "==> 5/5 重启后端服务"
  sudo systemctl restart simplejob-backend
  sleep 3
  curl -fsS http://127.0.0.1:8000/health || true
  echo "✅ 部署完成"
}
```

### 3.3 systemd 单元（`/etc/systemd/system/simplejob-backend.service`）

```ini
[Unit]
Description=SimpleJob Backend
After=network.target postgresql.service redis.service

[Service]
Type=simple
User=simplejob
WorkingDirectory=/opt/simplejob
EnvironmentFile=/opt/simplejob/.env
ExecStart=/opt/simplejob/.venv/bin/uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

---

## 四、升级

```bash
cd /opt/simplejob
git pull
cd deploy
./scripts/deploy.sh docker      # 或 bare
```

---

## 五、回滚

```bash
# Docker：回退到上一个镜像 tag
cd deploy
docker compose down
docker tag <registry>/simplejob-backend:v1.2.2 simplejob-backend:latest
docker compose up -d

# 裸机：git 回退 + 重启
git checkout v1.2.2
./scripts/deploy.sh bare
```

---

## 六、健康检查与排障

```bash
# 查看状态
docker compose ps
docker compose logs -f backend

# 裸机
systemctl status simplejob-backend
journalctl -u simplejob-backend -f

# 端口检查
ss -lntp | grep -E '8000|5432|6379'
```

---

## 七、一键脚本完整结构

```
deploy/scripts/
├── deploy.sh          # 主入口（docker / bare）
└── init-db.sh         # 仅初始化数据库
```

`init-db.sh`：

```bash
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
uv run alembic upgrade head
uv run python init_data.py
echo "✅ 数据库初始化完成"
```

---

## 八、前置检查清单

- [ ] 服务器时间同步（`timedatectl`）
- [ ] 端口 80/443/8000/5432/6379 可用
- [ ] `.env` 必填项齐全（DB / MAIL / JWT / LLM）
- [ ] 域名 + DNS 解析（钉钉回调）
- [ ] 防火墙放行（80/443，数据库不暴露公网）
