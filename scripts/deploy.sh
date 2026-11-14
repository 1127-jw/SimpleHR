#!/usr/bin/env bash
# SimpleJob 部署脚本（由 GitHub Actions 通过 SSH 触发，以 simplejob 身份运行）
#
# 流程：拉取最新代码 → 安装后端依赖 → 构建前端 → 重启后端服务
#
# 说明：
#   - 用 reset --hard 而不是 pull，规避 __pycache__/*.pyc 等本地改动导致的冲突
#   - 幂等：可重复执行；文件锁避免并发部署
#   - 前置：服务器已配置 /usr/local/bin/simplejob-restart 的免密 sudo
set -euo pipefail

PROJECT="/opt/simplejob/simple-hr"
LOG_DIR="$PROJECT/logs"
LOG="$LOG_DIR/deploy.log"

mkdir -p "$LOG_DIR"

# uv 可能在用户目录（非交互 shell 的 PATH 不含它），做兜底
UV="/home/simplejob/.local/bin/uv"
if [ ! -x "$UV" ]; then
    UV="$(command -v uv || true)"
fi

log() { echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }

# ---------- 防并发 ----------
exec 9>/tmp/simplejob-deploy.lock
flock -n 9 || { log "已有部署任务在运行，跳过"; exit 0; }

log "========== 开始部署 =========="

# ---------- 1) 拉取最新代码 ----------
cd "$PROJECT"
git fetch origin 2>&1 | tee -a "$LOG"
git reset --hard origin/master 2>&1 | tee -a "$LOG"
log "当前版本：$(git rev-parse --short HEAD)"

# ---------- 2) 后端依赖 ----------
if [ -n "$UV" ]; then
    "$UV" sync --all-groups 2>&1 | tee -a "$LOG"
else
    log "❌ 找不到 uv，跳过依赖安装"; exit 1
fi

# ---------- 3) 前端构建 ----------
cd "$PROJECT/hr-frontend"
rm -rf dist
npm ci 2>&1 | tee -a "$LOG"
VITE_API_BASE_URL='' npm run build 2>&1 | tee -a "$LOG"

# ---------- 4) 重启后端（systemd 守护进程）----------
sudo /usr/local/bin/simplejob-restart
sleep 3
if systemctl is-active --quiet simplejob; then
    log "✅ 部署完成，服务运行中"
else
    log "❌ 部署后服务未正常运行，请检查 journalctl -u simplejob"
    exit 1
fi
