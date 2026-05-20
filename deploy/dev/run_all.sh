#!/usr/bin/env bash
# Start / stop / status / reload / restart for eidolon-agent (gRPC + HTTP in one process).
#
#   ./deploy/dev/run_all.sh           # foreground (Ctrl+C exits)
#   ./deploy/dev/run_all.sh start     # background
#   ./deploy/dev/run_all.sh stop
#   ./deploy/dev/run_all.sh status
#   ./deploy/dev/run_all.sh reload    # SIGHUP → re-read settings.yaml + personas
#
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
unset VIRTUAL_ENV

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

PID_FILE="${HOME}/eidolon/run/eidolon-agent.pid"
LOG_FILE="${HOME}/eidolon/logs/eidolon-agent.log"

mkdir -p "$(dirname "$PID_FILE")" "$(dirname "$LOG_FILE")"

CMD=(.venv/bin/eidolon-agent)

alive() { [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; }

ready_check() {
  curl -sf http://127.0.0.1:8222/varz >/dev/null 2>&1 || {
    warn "NATS @ :4222 not reachable; start it first with ./deploy/dev/run_nats.sh start"
    return 1
  }
  return 0
}

do_foreground() {
  ready_check || true
  exec "${CMD[@]}"
}

do_start() {
  if alive; then error "already running (PID $(cat "$PID_FILE"))"; exit 1; fi
  ready_check || true
  info "starting eidolon-agent in background (log $LOG_FILE)"
  nohup "${CMD[@]}" >>"$LOG_FILE" 2>&1 &
  echo $! >"$PID_FILE"
  sleep 0.5
  if ! alive; then error "died immediately; tail $LOG_FILE :"; tail -30 "$LOG_FILE"; rm -f "$PID_FILE"; exit 1; fi
  info "PID $(cat "$PID_FILE")  /  http://127.0.0.1:8080/api/docs"
}

do_stop() {
  if ! alive; then info "not running"; rm -f "$PID_FILE"; exit 0; fi
  PID="$(cat "$PID_FILE")"
  info "SIGTERM $PID"
  kill -TERM "$PID"
  for _ in $(seq 1 60); do alive || break; sleep 0.5; done
  if alive; then warn "SIGKILL"; kill -KILL "$PID" || true; fi
  rm -f "$PID_FILE"
  info "stopped"
}

do_reload() {
  if ! alive; then error "not running"; exit 1; fi
  info "SIGHUP $(cat "$PID_FILE")"
  kill -HUP "$(cat "$PID_FILE")"
}

do_status() {
  echo -e "${CYAN}==== eidolon-agent ====${NC}"
  if alive; then
    info "running PID $(cat "$PID_FILE")"
    echo "  HTTP:  http://127.0.0.1:8080/api/docs"
    echo "  gRPC:  127.0.0.1:50051"
    echo "  Log:   $LOG_FILE"
    curl -sf http://127.0.0.1:8080/readyz | sed 's/^/  /'
  else
    info "not running"
    [[ -f "$PID_FILE" ]] && rm -f "$PID_FILE"
  fi
}

case "${1:-}" in
  start) do_start ;;
  stop) do_stop ;;
  restart) do_stop; do_start ;;
  reload) do_reload ;;
  status) do_status ;;
  "") do_foreground ;;
  *) error "usage: $0 [start|stop|restart|reload|status]"; exit 1 ;;
esac
