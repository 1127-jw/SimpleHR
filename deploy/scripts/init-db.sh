#!/usr/bin/env bash
# 仅初始化 / 重置数据库（迁移 + 种子数据）
set -euo pipefail
cd "$(dirname "$0")/.."

echo "==> 执行数据库迁移"
uv run alembic upgrade head

echo "==> 写入种子数据（部门 + 测试账号）"
uv run python init_data.py

echo "✅ 数据库初始化完成"
