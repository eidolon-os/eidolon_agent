"""Agent's operations contract against Agent's own configuration."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
import yaml

_REPOSITORY = Path(__file__).resolve().parents[1]
_CONTRACT = _REPOSITORY / "ops/component.toml"
_STATE_ROOT = "/var/lib/eidolon"
_RUNTIME_ROOT = "/run/eidolon"


@pytest.fixture(scope="module")
def contract() -> dict:
    return tomllib.loads(_CONTRACT.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def settings() -> dict:
    return yaml.safe_load(
        (_REPOSITORY / "config/settings.yaml").read_text(encoding="utf-8")
    )


def _expand(value: str) -> str:
    return value.replace("$EIDOLON_STATE_ROOT", _STATE_ROOT).replace(
        "$EIDOLON_RUNTIME_ROOT", _RUNTIME_ROOT
    )


def test_the_declared_ports_are_the_ones_agent_listens_on(
    contract: dict, settings: dict
) -> None:
    http = settings["http"]

    assert contract["ports"]["agent_http"]["default"] == http["port"]
    assert contract["ports"]["agent_admin"]["default"] == http["admin_port"]
    # Both loopback: everything that reaches Agent comes through Channel or the
    # Local API, never from a device directly.
    assert http["host"] == "127.0.0.1"
    assert {entry["bind"] for entry in contract["ports"].values()} == {"loopback"}


def test_the_declared_store_is_the_one_agent_opens(
    contract: dict, settings: dict
) -> None:
    declared = {entry["path"] for entry in contract["state"]["authority"]}
    runtime_store = next(
        value["sqlite_path"]
        for value in settings.values()
        if isinstance(value, dict) and "sqlite_path" in value
    )

    assert _expand(runtime_store) in declared


def test_the_socket_agent_serves_on_is_under_declared_runtime(
    contract: dict, settings: dict
) -> None:
    socket = Path(_expand(settings["grpc"]["uds_path"]))
    runtime = [Path(item) for item in contract["state"]["runtime"]]

    assert any(socket.is_relative_to(root) for root in runtime)


def test_agent_depends_on_the_bus_it_is_configured_to_use(
    contract: dict, settings: dict
) -> None:
    # The URL says NATS; the unit ordering has to say so too, or Agent comes up
    # before the bus exists and spends its first seconds failing to publish.
    assert settings["nats"]["url"].startswith("nats://")
    unit = contract["units"][0]
    assert "eidolon-nats" in unit["requires"]


def test_agent_declares_no_schema_gate_and_has_no_revisions_to_run() -> None:
    contract = tomllib.loads(_CONTRACT.read_text(encoding="utf-8"))
    versions = _REPOSITORY / "eidolon_agent/infra/persistence/migrations/versions"

    # There is an alembic.ini in this repository, which reads like there is a
    # migration gate. There is not: the runtime store is created with
    # create_all. If revisions ever appear, the contract has to grow a gate,
    # and this is where that becomes visible rather than being discovered as a
    # Host running against a schema nobody upgraded.
    assert "schema" not in contract
    if versions.is_dir():
        assert list(versions.glob("[0-9]*.py")) == []


def test_a_factory_reset_removes_everything_agent_holds(contract: dict) -> None:
    removed = [Path(item) for item in contract["reset"]["factory"]]

    for entry in contract["state"]["authority"]:
        assert any(Path(entry["path"]).is_relative_to(root) for root in removed)
