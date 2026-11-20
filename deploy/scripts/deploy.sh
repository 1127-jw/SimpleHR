#!/usr/bin/env bash
# SimpleJob 一键部署脚本
#
# 用法：
#   ./scripts/deploy.sh docker     # Docker Compose 部署（推荐）
#   ./scripts/deploy.sh bare       # 裸机 systemd 部署
#   ./scripts/deploy.sh init-db    # 仅初始化数据库
#
set -euo pipefail

MODE="${1:-docker}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

log()  { printf '\033[1;34m==> %s\033[0m\n' "$*"; }
fail() { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# ---------------- Docker 模式 ----------------
deploy_docker() {
  cd "$ROOT/deploy"
  [ -f .env ] || fail "缺少 deploy/.env，请先从项目根 .env 复制并编辑"

  log "1/6 构建镜像"
  docker compose build

  log "2/6 启动依赖（PostgreSQL + Redis）"
  docker compose up -d postgres redis
  sleep 5

  log "3/6 数据库迁移 + 种子数据"
  docker compose run --rm backend uv run alembic upgrade head
  docker compose run --rm backend uv run python init_data.py

  log "4/6 启动全部服务"
  docker compose up -d

  log "5/6 健康检查"
  sleep 5
  curl -fsS http://127.0.0.1:8000/ >/dev/null 2>&1 \
    && echo "✅ 后端健康" || echo "⚠️  后端未响应，查看日志：docker compose logs backend"

  log "6/6 服务状态"
  docker compose ps
  echo "✅ 部署完成"
}

# ---------------- 裸机模式 ----------------
deploy_bare() {
  [ -f "$ROOT/.env" ] || fail "缺少 .env"
  set -a; . "$ROOT/.env"; set +a

  log "1/5 安装后端依赖"
  uv sync --frozen

  log "2/5 构建前端"
  (cd hr-frontend && npm ci && npm run build-only)

  log "3/5 数据库迁移 + 种子数据"
  uv run alembic upgrade head
  uv run python init_data.py

  log "4/5 部署前端静态文件"
  sudo mkdir -p /var/www/simplejob
  sudo rsync -a --delete hr-frontend/dist/ /var/www/simplejob/

  log "5/5 重启后端服务"
  sudo systemctl restart simplejob-backend || fail "systemd 服务未配置，参考 deploy/one-click-deploy.md"
  sleep 3
  curl -fsS http://127.0.0.1:8000/ >/dev/null 2>&1 \
    && echo "✅ 后端健康" || echo "⚠️  后端未响应，查看日志：journalctl -u simplejob-backend"
  echo "✅ 部署完成"
}

# ---------------- 仅初始化数据库 ----------------
init_db() {
  log "初始化数据库"
  uv run alembic upgrade head
  uv run python init_data.py
  echo "✅ 完成"
}

case "$MODE" in
  docker) deploy_docker ;;
  bare)   deploy_bare ;;
  init-db) init_db ;;
  *) fail "未知模式：$MODE（可选 docker / bare / init-db）" ;;
esac
