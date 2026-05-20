#!/usr/bin/env bash
# One-shot dev initialization for eidolon-agent.
#
# Idempotent. Safe to re-run. Performs:
#   - uv sync --extra dev
#   - regenerate gRPC stubs from .proto
#   - alembic upgrade head (SQLite at settings.sqlite.path)
#   - ensure runtime/log dirs exist
#
#   ./deploy/dev/init.sh
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
unset VIRTUAL_ENV

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*" >&2; }

command -v uv >/dev/null || { error "uv not installed (https://docs.astral.sh/uv/)"; exit 1; }

info "sync python deps"
uv sync --extra dev >/dev/null

info "regenerate gRPC stubs"
.venv/bin/python -m grpc_tools.protoc \
  -I eidolon_agent/transport/grpc/proto \
  --python_out=eidolon_agent/transport/grpc/proto \
  --grpc_python_out=eidolon_agent/transport/grpc/proto \
  --pyi_out=eidolon_agent/transport/grpc/proto \
  eidolon_agent/transport/grpc/proto/eidolon.proto
# patch generated import to be package-relative
sed -i.bak 's/^import eidolon_pb2 as eidolon__pb2/from . import eidolon_pb2 as eidolon__pb2/' \
  eidolon_agent/transport/grpc/proto/eidolon_pb2_grpc.py
rm -f eidolon_agent/transport/grpc/proto/eidolon_pb2_grpc.py.bak

info "alembic upgrade head"
.venv/bin/alembic upgrade head >/dev/null

# Config file
if [ ! -f config/config.yaml ]; then
  cp config/config.yaml.example config/config.yaml
  info "created config/config.yaml from template — edit as needed"
fi

# Runtime dirs
mkdir -p "${HOME}/eidolon/run" "${HOME}/eidolon/logs" "${HOME}/eidolon/debug" "${HOME}/eidolon/history"

info "done. Next:"
echo "  ./deploy/dev/run_nats.sh start"
echo "  ./deploy/dev/run_all.sh start"
