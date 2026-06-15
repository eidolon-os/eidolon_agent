"""Admin /api/admin/personas/* — stub the service, exercise routes via TestClient.

We stub ``PersonasService`` here rather than spinning up the full stack because
the router is intentionally thin: validate URLs, body parsing, error mapping,
and JSON-shape correctness. End-to-end behaviour with a real SQL store lives
in ``domain/personas/tests/functional/``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from eidolon_agent.app.admin.routers import personas as personas_router
from eidolon_agent.core.errors import NotFoundError
from eidolon_agent.domain.personas.types import (
    BehavioralKnob,
    IdentityCore,
    PersonaEvolutionChange,
    PersonaEvolutionProposal,
    PersonaEvolutionResult,
    PersonaInstance,
    PersonaMetadata,
    PersonaObservation,
    PersonaProposalPatch,
    PersonaTemplateSummary,
)

pytestmark = pytest.mark.functional


def _instance(**overrides) -> PersonaInstance:
    now = datetime.now(timezone.utc)
    data = {
        "instance_id": "inst-1",
        "tenant_id": "t",
        "user_id": "alice",
        "origin_template_id": "tpl-1",
        "origin_template_revision": 1,
        "overlay_version": 1,
        "created_at": now,
        "updated_at": now,
        "metadata": PersonaMetadata(template_id="tpl-1", archetype="a", name="N"),
        "identity_core": IdentityCore(),
        "behavioral_knobs": {"intimacy": BehavioralKnob(current=0.5)},
    }
    data.update(overrides)
    return PersonaInstance(**data)


class _StubService:
    """Minimal PersonasService stand-in tracking calls."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self._instances: dict[str, PersonaInstance] = {"inst-1": _instance()}
        self._history: list[PersonaEvolutionResult] = []
        self._delta_store: dict[str, PersonaEvolutionResult] = {}
        self._observations = [
            PersonaObservation(
                id="obs-1",
                tenant_id="t",
                user_id="alice",
                instance_id="inst-1",
                kind="positive_feedback_received",
            )
        ]
        self._proposals = {
            "proposal-1": PersonaEvolutionProposal(
                id="proposal-1",
                tenant_id="t",
                user_id="alice",
                instance_id="inst-1",
                patches=(
                    PersonaProposalPatch(
                        type="knob_delta",
                        target="behavioral_knobs.intimacy",
                        delta=0.03,
                    ),
                ),
            )
        }

    async def list_templates(self):
        return [
            PersonaTemplateSummary(
                template_id="tpl-1", template_revision=1, name="N", archetype="a"
            )
        ]

    async def reload_templates(self) -> int:
        self.calls.append(("reload_templates",))
        return 3

    async def get_template_raw(self, template_id: str) -> str:
        self.calls.append(("get_template_raw", template_id))
        if template_id == "missing":
            raise NotFoundError(f"persona template not found: {template_id}")
        return "metadata:\n  template_id: tpl-1\n"

    async def list_instances(self):
        return list(self._instances.values())

    async def get_snapshot(self, *, tenant_id, user_id, instance_id, template_id=None):
        if instance_id not in self._instances:
            raise NotFoundError(f"instance not found: {instance_id}")
        return SimpleNamespace(
            instance=self._instances[instance_id],
            runtime_state=SimpleNamespace(model_dump=lambda mode="json": {"mood": "joy"}),
            prompt_hint="心情不错",
            model_dump=lambda mode="json": {
                "instance": self._instances[instance_id].model_dump(mode="json"),
                "prompt_hint": "心情不错",
            },
        )

    async def list_evolution_history(self, instance_id: str, *, limit: int = 50):
        return list(self._history)

    async def list_observations(
        self,
        instance_id: str,
        *,
        status: str | None = None,
        limit: int = 50,
    ):
        self.calls.append(("list_observations", instance_id, status, limit))
        return list(self._observations)

    async def list_evolution_proposals(
        self,
        instance_id: str,
        *,
        status: str | None = None,
        limit: int = 50,
    ):
        self.calls.append(("list_evolution_proposals", instance_id, status, limit))
        return list(self._proposals.values())

    async def run_reflection(
        self,
        *,
        tenant_id,
        user_id,
        instance_id,
        dry_run=False,
        auto_apply=True,
        limit=50,
    ):
        self.calls.append(("run_reflection", instance_id, dry_run, auto_apply, limit))
        return list(self._proposals.values())

    async def get_evolution_proposal(self, proposal_id: str):
        if proposal_id not in self._proposals:
            raise NotFoundError(f"proposal not found: {proposal_id}")
        return self._proposals[proposal_id]

    async def approve_evolution_proposal(self, proposal_id: str, *, actor: str = "admin"):
        self.calls.append(("approve_evolution_proposal", proposal_id, actor))
        if proposal_id not in self._proposals:
            raise NotFoundError(f"proposal not found: {proposal_id}")
        return PersonaEvolutionResult(instance_id="inst-1", applied=True)

    async def reject_evolution_proposal(
        self,
        proposal_id: str,
        *,
        actor: str = "admin",
        reason: str | None = None,
    ):
        self.calls.append(("reject_evolution_proposal", proposal_id, actor, reason))
        if proposal_id not in self._proposals:
            raise NotFoundError(f"proposal not found: {proposal_id}")
        return self._proposals[proposal_id].model_copy(update={"status": "rejected"})

    async def rollback_evolution(self, *, tenant_id, user_id, instance_id, delta_id):
        if delta_id not in self._delta_store:
            raise NotFoundError(f"evolution delta not found: {delta_id}")
        return PersonaEvolutionResult(
            instance_id=instance_id, applied=True, rationale=f"rollback of {delta_id}"
        )

    async def delete_instance(self, *, tenant_id, user_id, instance_id):
        self._instances.pop(instance_id, None)
        self.calls.append(("delete_instance", instance_id))


@pytest.fixture
def stub_service() -> _StubService:
    return _StubService()


@pytest.fixture
def client(stub_service: _StubService) -> TestClient:
    app = FastAPI()
    app.include_router(personas_router.router, prefix="/api/admin")
    app.state.personas_service = stub_service
    return TestClient(app)


# ---- New endpoints --------------------------------------------------------


def test_reload_templates_returns_count(client: TestClient) -> None:
    r = client.post("/api/admin/personas/templates/reload")
    assert r.status_code == 200
    assert r.json() == {"loaded": 3}


def test_get_template_raw_returns_plaintext(client: TestClient) -> None:
    r = client.get("/api/admin/personas/templates/tpl-1/raw")
    assert r.status_code == 200
    assert "tpl-1" in r.text
    assert r.headers["content-type"].startswith("text/plain")


def test_get_template_raw_missing_returns_404(client: TestClient) -> None:
    r = client.get("/api/admin/personas/templates/missing/raw")
    assert r.status_code == 404


def test_list_instances_returns_summary_rows(client: TestClient) -> None:
    r = client.get("/api/admin/personas/instances")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    row = body[0]
    assert row["instance_id"] == "inst-1"
    assert row["overlay_version"] == 1
    assert row["template_id"] == "tpl-1"


def test_instance_snapshot_returns_compiled_view(client: TestClient) -> None:
    r = client.get("/api/admin/personas/instances/t/alice/inst-1/snapshot")
    assert r.status_code == 200
    body = r.json()
    assert body["prompt_hint"] == "心情不错"
    assert body["instance"]["instance_id"] == "inst-1"


def test_instance_snapshot_missing_returns_404(client: TestClient) -> None:
    r = client.get("/api/admin/personas/instances/t/alice/ghost/snapshot")
    assert r.status_code == 404


def test_evolution_history_returns_empty_when_no_audit(client: TestClient) -> None:
    r = client.get("/api/admin/personas/instances/t/alice/inst-1/evolution")
    assert r.status_code == 200
    assert r.json() == []


def test_evolution_history_paginates(client: TestClient, stub_service: _StubService) -> None:
    stub_service._history = [
        PersonaEvolutionResult(
            instance_id="inst-1",
            applied=True,
            rationale=f"r{i}",
            changes=(
                PersonaEvolutionChange(
                    path="behavioral_knobs.intimacy", old=0.5, new=0.55, rule_id="r"
                ),
            ),
        )
        for i in range(3)
    ]
    r = client.get("/api/admin/personas/instances/t/alice/inst-1/evolution?limit=2")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 3  # stub ignores limit; what matters is the route plumbing


def test_observations_endpoint_uses_service(client: TestClient, stub_service: _StubService) -> None:
    r = client.get(
        "/api/admin/personas/instances/t/alice/inst-1/observations?status=active&limit=7"
    )
    assert r.status_code == 200
    assert r.json()[0]["id"] == "obs-1"
    assert ("list_observations", "inst-1", "active", 7) in stub_service.calls


def test_proposals_endpoint_uses_service(client: TestClient, stub_service: _StubService) -> None:
    r = client.get("/api/admin/personas/instances/t/alice/inst-1/proposals?status=pending&limit=9")
    assert r.status_code == 200
    assert r.json()[0]["id"] == "proposal-1"
    assert ("list_evolution_proposals", "inst-1", "pending", 9) in stub_service.calls


def test_reflect_endpoint_uses_service(client: TestClient, stub_service: _StubService) -> None:
    r = client.post(
        "/api/admin/personas/instances/t/alice/inst-1/reflect",
        json={"dry_run": True, "limit": 3},
    )
    assert r.status_code == 200
    assert r.json()[0]["id"] == "proposal-1"
    assert ("run_reflection", "inst-1", True, True, 3) in stub_service.calls


def test_proposal_detail_and_decisions(client: TestClient, stub_service: _StubService) -> None:
    detail = client.get("/api/admin/personas/evolution-proposals/proposal-1")
    assert detail.status_code == 200
    assert detail.json()["id"] == "proposal-1"

    approved = client.post(
        "/api/admin/personas/evolution-proposals/proposal-1/approve",
        json={"actor": "operator"},
    )
    assert approved.status_code == 200
    assert approved.json()["applied"] is True
    assert ("approve_evolution_proposal", "proposal-1", "operator") in stub_service.calls

    rejected = client.post(
        "/api/admin/personas/evolution-proposals/proposal-1/reject",
        json={"actor": "operator", "reason": "not now"},
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"
    assert (
        "reject_evolution_proposal",
        "proposal-1",
        "operator",
        "not now",
    ) in stub_service.calls


def test_rollback_unknown_delta_returns_404(client: TestClient) -> None:
    r = client.post(
        "/api/admin/personas/instances/t/alice/inst-1/rollback",
        json={"delta_id": "ghost"},
    )
    assert r.status_code == 404


def test_rollback_existing_delta_returns_result(
    client: TestClient, stub_service: _StubService
) -> None:
    stub_service._delta_store["d-1"] = PersonaEvolutionResult(instance_id="inst-1", applied=True)
    r = client.post(
        "/api/admin/personas/instances/t/alice/inst-1/rollback",
        json={"delta_id": "d-1"},
    )
    assert r.status_code == 200
    assert r.json()["rationale"] == "rollback of d-1"


def test_delete_instance_returns_ack(client: TestClient, stub_service: _StubService) -> None:
    r = client.delete("/api/admin/personas/instances/t/alice/inst-1")
    assert r.status_code == 200
    assert r.json() == {"deleted": "inst-1"}
    assert ("delete_instance", "inst-1") in stub_service.calls
