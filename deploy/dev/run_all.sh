#!/usr/bin/env bash
# Start / stop / status / reload / restart for eidolon-agent + admin web (Vite).
#
#   ./deploy/dev/run_all.sh           # foreground agent (Ctrl+C exits; starts admin web in bg if needed)
#   ./deploy/dev/run_all.sh start     # background (agent + admin web)
#   ./deploy/dev/run_all.sh stop
#   ./deploy/dev/run_all.sh restart
#   ./deploy/dev/run_all.sh status
#   ./deploy/dev/run_all.sh reload    # SIGHUP → re-read settings.yaml + personas (agent only)
#
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
unset VIRTUAL_ENV

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

AGENT_PID_FILE="${HOME}/eidolon/run/eidolon-agent.pid"
AGENT_LOG_FILE="${HOME}/eidolon/logs/eidolon-agent.log"
ADMIN_PID_FILE="${HOME}/eidolon/run/eidolon-admin-web.pid"
ADMIN_LOG_FILE="${HOME}/eidolon/logs/eidolon-admin-web.log"
ADMIN_WEB_DIR="$ROOT/admin_web"

mkdir -p "$(dirname "$AGENT_PID_FILE")" "$(dirname "$AGENT_LOG_FILE")" \
         "$(dirname "$ADMIN_PID_FILE")" "$(dirname "$ADMIN_LOG_FILE")"

CMD=(.venv/bin/eidolon-agent)
ADMIN_STARTED_HERE=false

agent_alive() { [[ -f "$AGENT_PID_FILE" ]] && kill -0 "$(cat "$AGENT_PID_FILE")" 2>/dev/null; }
admin_alive() { [[ -f "$ADMIN_PID_FILE" ]] && kill -0 "$(cat "$ADMIN_PID_FILE")" 2>/dev/null; }

ready_check() {
  curl -sf http://127.0.0.1:8222/varz >/dev/null 2>&1 || {
    warn "NATS @ :4222 not reachable; start it first with ./deploy/dev/run_nats.sh start"
    return 1
  }
  return 0
}

admin_ensure_deps() {
  command -v npm >/dev/null || { error "npm not installed"; exit 1; }
  [[ -d "$ADMIN_WEB_DIR/node_modules" ]] || {
    info "admin web first run — npm install"
    (cd "$ADMIN_WEB_DIR" && npm install >/dev/null)
  }
}

do_admin_start() {
  admin_ensure_deps
  if admin_alive; then
    info "admin web already running (PID $(cat "$ADMIN_PID_FILE"))"
    return 0
  fi
  info "starting admin web in background (log $ADMIN_LOG_FILE)"
  (
    cd "$ADMIN_WEB_DIR"
    nohup npm run dev -- --port 5281 --strictPort >>"$ADMIN_LOG_FILE" 2>&1 &
    echo $! >"$ADMIN_PID_FILE"
  )
  sleep 1
  if ! admin_alive; then
    error "admin web died immediately; tail $ADMIN_LOG_FILE :"
    tail -30 "$ADMIN_LOG_FILE"
    rm -f "$ADMIN_PID_FILE"
    exit 1
  fi
  ADMIN_STARTED_HERE=true
  info "admin web PID $(cat "$ADMIN_PID_FILE")  /  http://127.0.0.1:5281/"
}

do_admin_stop() {
  if ! admin_alive; then info "admin web not running"; rm -f "$ADMIN_PID_FILE"; return 0; fi
  PID="$(cat "$ADMIN_PID_FILE")"
  info "SIGTERM admin web $PID"
  kill -TERM "$PID"
  sleep 0.5
  admin_alive && kill -KILL "$PID" || true
  rm -f "$ADMIN_PID_FILE"
  info "admin web stopped"
}

do_admin_status() {
  echo -e "${CYAN}==== admin web ====${NC}"
  if admin_alive; then
    info "running PID $(cat "$ADMIN_PID_FILE")"
    echo "  UI:    http://127.0.0.1:5281/"
    echo "  Log:   $ADMIN_LOG_FILE"
  else
    info "not running"
    [[ -f "$ADMIN_PID_FILE" ]] && rm -f "$ADMIN_PID_FILE"
  fi
}

do_agent_foreground() {
  ready_check || true
  if ! admin_alive; then
    do_admin_start
    ADMIN_STARTED_HERE=true
  fi
  trap '[[ "$ADMIN_STARTED_HERE" == true ]] && do_admin_stop' EXIT INT TERM
  exec "${CMD[@]}"
}

do_agent_start() {
  if agent_alive; then error "eidolon-agent already running (PID $(cat "$AGENT_PID_FILE"))"; exit 1; fi
  ready_check || true
  info "starting eidolon-agent in background (log $AGENT_LOG_FILE)"
  nohup "${CMD[@]}" >>"$AGENT_LOG_FILE" 2>&1 &
  echo $! >"$AGENT_PID_FILE"
  sleep 0.5
  if ! agent_alive; then
    error "eidolon-agent died immediately; tail $AGENT_LOG_FILE :"
    tail -30 "$AGENT_LOG_FILE"
    rm -f "$AGENT_PID_FILE"
    exit 1
  fi
  info "eidolon-agent PID $(cat "$AGENT_PID_FILE")  /  http://127.0.0.1:8080/api/docs"
}

do_agent_stop() {
  if ! agent_alive; then info "eidolon-agent not running"; rm -f "$AGENT_PID_FILE"; return 0; fi
  PID="$(cat "$AGENT_PID_FILE")"
  info "SIGTERM eidolon-agent $PID"
  kill -TERM "$PID"
  for _ in $(seq 1 60); do agent_alive || break; sleep 0.5; done
  if agent_alive; then warn "SIGKILL eidolon-agent"; kill -KILL "$PID" || true; fi
  rm -f "$AGENT_PID_FILE"
  info "eidolon-agent stopped"
}

do_agent_reload() {
  if ! agent_alive; then error "eidolon-agent not running"; exit 1; fi
  info "SIGHUP $(cat "$AGENT_PID_FILE")"
  kill -HUP "$(cat "$AGENT_PID_FILE")"
}

do_agent_status() {
  echo -e "${CYAN}==== eidolon-agent ====${NC}"
  if agent_alive; then
    info "running PID $(cat "$AGENT_PID_FILE")"
    echo "  HTTP:  http://127.0.0.1:8080/api/docs"
    echo "  gRPC:  127.0.0.1:50051"
    echo "  Log:   $AGENT_LOG_FILE"
    curl -sf http://127.0.0.1:8080/readyz | sed 's/^/  /'
  else
    info "not running"
    [[ -f "$AGENT_PID_FILE" ]] && rm -f "$AGENT_PID_FILE"
  fi
}

do_start() {
  do_admin_start
  do_agent_start
}

do_stop() {
  do_agent_stop
  do_admin_stop
}

do_restart() {
  do_stop
  do_start
}

do_status() {
  do_agent_status
  echo
  do_admin_status
}

case "${1:-}" in
  start) do_start ;;
  stop) do_stop ;;
  restart) do_restart ;;
  reload) do_agent_reload ;;
  status) do_status ;;
  "") do_agent_foreground ;;
  *) error "usage: $0 [start|stop|restart|reload|status]"; exit 1 ;;
esac
