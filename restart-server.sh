#!/bin/bash
# 清缓存 + 重启后端服务器
set -e

cd "$(dirname "$0")"

echo "🧹 清理 Python 缓存..."
find . -path "*/__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true

echo "🔪 停止旧服务器..."
lsof -ti :8088 | xargs kill -9 2>/dev/null || true
sleep 1

echo "🚀 启动服务器 (port 8088)..."
FUTU_OPEND_HOST=127.0.0.1 .venv/bin/uvicorn api.main:app --host 0.0.0.0 --port 8088
