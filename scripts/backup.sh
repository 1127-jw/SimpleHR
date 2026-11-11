#!/usr/bin/env bash
# SimpleJob 数据备份脚本（由 crontab 每日调用，也可手动执行）
#
# 备份内容：
#   1) PostgreSQL 逻辑备份（custom 格式，可用 pg_restore 恢复）
#   2) 文件备份：简历 upload/、邮件附件 attachments/、邮件机器人游标 mailbot_state.json
#   3) 自动清理 30 天前的备份
#
# 用法：bash /opt/simplejob/scripts/backup.sh
set -euo pipefail

APP_DIR="/opt/simplejob"
BACKUP_DIR="${HOME}/backups"

mkdir -p "${BACKUP_DIR}"

# 加载 .env 中的 DB_* 配置（cron 环境不会自动加载 .env）
set -a
# shellcheck disable=SC1090
source "${APP_DIR}/.env"
set +a

DATE="$(date +%F)"
DB_DUMP="${BACKUP_DIR}/hr_system_${DATE}.dump"
FILES_ARCHIVE="${BACKUP_DIR}/simplejob_files_${DATE}.tar.gz"

log() { echo "[$(date '+%F %T')] $*"; }

log "开始备份..."

# 1) 数据库逻辑备份
PGPASSWORD="${DB_PASSWORD}" pg_dump \
    -h "${DB_HOST:-127.0.0.1}" -p "${DB_PORT:-5432}" \
    -U "${DB_USERNAME:-simplejob}" -d "${DB_NAME:-hr_system}" \
    -Fc -f "${DB_DUMP}"
log "数据库备份完成：${DB_DUMP}"

# 2) 文件备份
tar -czf "${FILES_ARCHIVE}" -C "${APP_DIR}" upload attachments mailbot_state.json
log "文件备份完成：${FILES_ARCHIVE}"

# 3) 清理 30 天前的备份
find "${BACKUP_DIR}" -name 'hr_system_*.dump' -mtime +30 -delete
find "${BACKUP_DIR}" -name 'simplejob_files_*.tar.gz' -mtime +30 -delete

log "备份完成"
