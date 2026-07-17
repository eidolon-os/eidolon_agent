#!/usr/bin/env bash
set -euo pipefail

agent_root="$(cd "$(dirname "$0")/.." && pwd)"
workspace_root="$(cd "${agent_root}/.." && pwd)"

"${agent_root}/.venv/bin/pytest" -q -p no:cacheprovider \
  "${agent_root}/tests/e2e/test_cross_companion_roll_call_e2e.py"

(
  cd "${workspace_root}/eidolon_hub"
  env PYTHONPATH=. .venv/bin/pytest -q -p no:cacheprovider \
    tests/test_discovery.py \
    tests/test_config.py \
    tests/test_runtime_commands.py \
    tests/test_admin_runtime.py \
    tests/test_control_bridge.py
)

(
  cd "${workspace_root}/eidolon_sdk"
  .venv/bin/pytest -q -p no:cacheprovider \
    tests/body/test_body_protocol.py \
    tests/contracts/test_wire_contracts.py \
    tests/contracts/test_esp32_contract_mirror.py
)

(
  cd "${workspace_root}/eidolon_data"
  .venv/bin/pytest -q -p no:cacheprovider tests/test_hub_registry_adapter.py
)

(
  cd "${workspace_root}/eidolon-client-esp32"
  bash tests/run_guard_runtime_tests.sh
)

echo "cross-Companion roll-call E2E and protocol contracts passed"
