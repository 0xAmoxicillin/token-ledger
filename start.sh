#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8765}"
HOST="${HOST:-127.0.0.1}"

if ! [[ "$PORT" =~ ^[0-9]{1,5}$ ]]; then
  echo "PORT 必须是 1-65535 的整数（当前：${PORT}）" >&2
  exit 2
fi
PORT_NUM=$((10#$PORT))
if (( PORT_NUM < 1 || PORT_NUM > 65535 )); then
  echo "PORT 必须是 1-65535 的整数（当前：${PORT}）" >&2
  exit 2
fi

echo "[1/2] 重扫本机 agent 日志并幂等导入新增 token 用量..."
python3 collector.py

echo "[2/2] 启动后台管理服务: http://${HOST}:${PORT}/"
exec python3 server.py --host "$HOST" --port "$PORT"
