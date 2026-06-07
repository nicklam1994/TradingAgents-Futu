#!/bin/bash
# 清缓存 + 重建前端
set -e

cd "$(dirname "$0")"

echo "🧹 清理 Python 缓存..."
find . -path "*/__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true

echo "🧹 清理前端构建缓存..."
rm -rf frontend/dist frontend/node_modules/.vite 2>/dev/null || true

echo "🔨 重建前端..."
cd frontend && npm run build

echo "✅ 前端构建完成 (frontend/dist/)"
