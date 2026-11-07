#!/bin/sh
set -e
# 首次运行没有配置时，用示例配置兜底（记得改 upstream/client key）
if [ ! -f /app/config.yaml ]; then
  cp /app/config.example.yaml /app/config.yaml
  echo "[entrypoint] config.yaml not found - copied from config.example.yaml"
fi
cd /app
exec uv run --no-dev python -m app.main
