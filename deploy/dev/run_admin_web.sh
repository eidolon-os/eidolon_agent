#!/usr/bin/env bash
# Admin web — Vite dev server.
# The admin HTTP API runs inside the main eidolon-agent process at :8080;
# this script only starts the Vue/Vite frontend on :5281.
#
#   ./deploy/dev/run_admin_web.sh         # foreground
#   ./deploy/dev/run_admin_web.sh start   # background
#   ./deploy/dev/run_admin_web.sh stop
#   ./deploy/dev/run_admin_web.sh status
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT/admin_web"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

PID_FILE="${HOME}/eidolon/run/eidolon-admin-web.pid"
LOG_FILE="${HOME}/eidolon/logs/eidolon-admin-web.log"
mkdir -p "$(dirname "$PID_FILE")" "$(dirname "$LOG_FILE")"

command -v npm >/dev/null || { error "npm not installed"; exit 1; }
[[ -d node_modules ]] || { info "first run — npm install"; npm install >/dev/null; }

alive() { [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; }

case "${1:-}" in
  start)
    if alive; then info "already running (PID $(cat "$PID_FILE"))"; exit 0; fi
    nohup npm run dev -- --port 5281 --strictPort >>"$LOG_FILE" 2>&1 &
    echo $! >"$PID_FILE"
    sleep 1
    info "PID $(cat "$PID_FILE")  /  http://127.0.0.1:5281/"
    ;;
  stop)
    if ! alive; then info "not running"; rm -f "$PID_FILE"; exit 0; fi
    kill -TERM "$(cat "$PID_FILE")"; sleep 0.5
    alive && kill -KILL "$(cat "$PID_FILE")" || true
    rm -f "$PID_FILE"
    ;;
  status)
    if alive; then info "running PID $(cat "$PID_FILE")  /  http://127.0.0.1:5281/"
    else info "not running"; [[ -f "$PID_FILE" ]] && rm -f "$PID_FILE"; fi
    ;;
  "") exec npm run dev -- --port 5281 --strictPort ;;
  *) error "usage: $0 [start|stop|status]"; exit 1 ;;
esac
