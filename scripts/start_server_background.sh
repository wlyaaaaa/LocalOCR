#!/usr/bin/env bash
# Start LocalOCR API in the background from inside WSL.
set -euo pipefail

HOST="${1:-127.0.0.1}"
PORT="${2:-8765}"
PROJ=/mnt/e/LocalOCR
SERVER_DIR="$PROJ/.server"
LOG="$SERVER_DIR/server.log"
PID_FILE="$SERVER_DIR/server.pid"

mkdir -p "$SERVER_DIR"
cd "$PROJ"

nohup scripts/run_in_wsl.sh -m localocr.server --host "$HOST" --port "$PORT" \
    > "$LOG" 2>&1 < /dev/null &

echo "$!" > "$PID_FILE"
echo "LocalOCR API starting with pid $(cat "$PID_FILE"), log: $LOG"
