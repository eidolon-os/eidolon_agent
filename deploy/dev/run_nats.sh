#!/usr/bin/env bash
# Start / stop / status for a local NATS server with JetStream enabled.
#
#   ./deploy/dev/run_nats.sh start
#   ./deploy/dev/run_nats.sh stop
#   ./deploy/dev/run_nats.sh status
#
# Stores PID in ~/eidolon/run/nats.pid, logs to ~/eidolon/logs/nats.log.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

PID_FILE="${HOME}/eidolon/run/nats.pid"
LOG_FILE="${HOME}/eidolon/logs/nats.log"
DATA_DIR="${HOME}/eidolon/nats-jetstream"
mkdir -p "$(dirname "$PID_FILE")" "$(dirname "$LOG_FILE")" "$DATA_DIR"

if ! command -v nats-server >/dev/null; then
  error "nats-server not installed (brew install nats-server)"
  exit 1
fi

alive() {
  [[ -f "$PID_FILE" ]] || return 1
  kill -0 "$(cat "$PID_FILE")" 2>/dev/null
}

case "${1:-start}" in
  start)
    if alive; then
      info "nats-server already running (PID $(cat "$PID_FILE"))"
      exit 0
    fi
    info "starting nats-server -js  (log $LOG_FILE)"
    nohup nats-server -js -sd "$DATA_DIR" --port 4222 --http_port 8222 \
      >"$LOG_FILE" 2>&1 &
    echo $! >"$PID_FILE"
    sleep 0.5
    if ! alive; then
      error "nats-server died immediately; tail -20 $LOG_FILE :"
      tail -20 "$LOG_FILE"
      rm -f "$PID_FILE"
      exit 1
    fi
    info "PID $(cat "$PID_FILE")  /  monitoring http://127.0.0.1:8222"
    ;;
  stop)
    if ! alive; then info "nats-server not running."; rm -f "$PID_FILE"; exit 0; fi
    info "SIGTERM $(cat "$PID_FILE")"
    kill -TERM "$(cat "$PID_FILE")"
    for _ in $(seq 1 30); do alive || break; sleep 0.5; done
    if alive; then warn "SIGKILL"; kill -KILL "$(cat "$PID_FILE")" || true; fi
    rm -f "$PID_FILE"
    ;;
  status)
    if alive; then
      info "running PID $(cat "$PID_FILE")"
      command -v nats >/dev/null && nats --server nats://127.0.0.1:4222 stream ls 2>/dev/null || true
    else
      info "not running"
      [[ -f "$PID_FILE" ]] && rm -f "$PID_FILE"
    fi
    ;;
  restart) "$0" stop; "$0" start ;;
  *) error "usage: $0 [start|stop|status|restart]"; exit 1 ;;
esac
