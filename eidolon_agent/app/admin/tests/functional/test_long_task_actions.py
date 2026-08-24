"""Cancel and retry, and the four things they used to say that were not true.

These two are the only writes on this surface a person triggers directly, and the
Management projection above them reports whatever they return. So what they claim
has to be what happened:

- cancelling a **finished** task used to succeed, overwriting a succeeded record
  with ``cancelled`` while leaving its result in place — a task that was
  simultaneously cancelled and holding an answer;
- retrying a **running** task used to clear ``worker_id`` and ``lease_until`` out
  from under the worker still holding them;
- retrying a **succeeded** task used to promise a rerun of work whose result it
  would then have been sitting on;
- and retry never ran anything at all. Nothing polls the store for ``accepted``
  rows — the worker is fed by ``submit`` — so a retry that only wrote the row
  left the task at ``accepted`` for good while answering as if it were queued.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from eidolon_agent.app.admin.routers import long_tasks as long_tasks_router
from eidolon_agent.app.admin.tests.conftest import AUTHORITY_HEADERS
from eidolon_agent.core.ports.long_tasks import LongTaskQueueFullError
from eidolon_agent.core.types.long_task import LongTaskRecord, LongTaskStatus
from eidolon_agent.infra.persistence import AgentLongTaskStore, AgentRuntimeStore

from .test_router_long_tasks import _seed_task

pytestmark = pytest.mark.functional

OWNER = "manson"


class _Submitter:
    """The worker, as this route needs it: something that takes a task back."""

    def __init__(self, *, full: bool = False) -> None:
        self.submitted: list[LongTaskRecord] = []
        self.full = full

    async def submit(self, record: LongTaskRecord) -> None:
        if self.full:
            raise LongTaskQueueFullError("queue is full")
        self.submitted.append(record)


_MISSING = object()


@asynccontextmanager
async def _surface(tmp_path: Path, *, submitter=_MISSING):
    """One store, one app, one client, closed on the way out.

    A context manager rather than a returned triple: the store holds SQLite
    connections and this suite turns an unclosed one into an error. That is the
    right setting — a test that leaks a connection is a test whose teardown was
    never written — so the helper owns the closing instead of ten copies of a
    ``finally``.
    """

    if submitter is _MISSING:
        submitter = _Submitter()
    store = AgentRuntimeStore.open(tmp_path / "eidolon-agent.sqlite3")
    await store.init_schema()
    app = FastAPI()
    app.state.runtime_store = store
    app.state.long_task_submitter = submitter
    app.include_router(long_tasks_router.router, prefix="/api/admin")
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://t",
        headers=AUTHORITY_HEADERS,
    )
    try:
        yield client, store, submitter
    finally:
        await client.aclose()
        await store.close()


async def test_cancelling_a_running_task_stops_it(tmp_path) -> None:
    async with _surface(tmp_path) as (client, store, _submitter):
        await _seed_task(store, task_id="j-run", owner_id=OWNER)

        answered = await client.post(
            f"/api/admin/long-tasks/j-run/cancel?owner_id={OWNER}"
        )

        assert answered.status_code == 200
        assert answered.json()["status"] == "cancelled"


async def test_cancelling_twice_is_a_success(tmp_path) -> None:
    """The state asked for is the state it is in, and a client that never saw the
    first answer deserves the second."""

    async with _surface(tmp_path) as (client, store, _submitter):
        await _seed_task(store, task_id="j-twice", owner_id=OWNER)

        first = await client.post(f"/api/admin/long-tasks/j-twice/cancel?owner_id={OWNER}")
        second = await client.post(f"/api/admin/long-tasks/j-twice/cancel?owner_id={OWNER}")

        assert (first.status_code, second.status_code) == (200, 200)
        assert second.json()["status"] == "cancelled"


async def test_a_finished_task_is_not_cancelled_over(tmp_path) -> None:
    """It used to answer 200 and leave a record that was cancelled *and* had a
    result — which nothing downstream can interpret and a person reads as lost
    work."""

    async with _surface(tmp_path) as (client, store, _submitter):
        await _seed_task(
            store, task_id="j-done", owner_id=OWNER, status=LongTaskStatus.SUCCEEDED
        )

        answered = await client.post(
            f"/api/admin/long-tasks/j-done/cancel?owner_id={OWNER}"
        )

        assert answered.status_code == 409
        assert "succeeded" in answered.text
        kept = await AgentLongTaskStore(store).get("j-done")
        assert kept is not None
        assert kept.status is LongTaskStatus.SUCCEEDED
        assert kept.result_text == "done"


async def test_retrying_hands_the_task_back_to_the_worker(tmp_path) -> None:
    """The one that makes the word true. Nothing polls for accepted rows."""

    async with _surface(tmp_path) as (client, store, submitter):
        await _seed_task(
            store, task_id="j-fail", owner_id=OWNER, status=LongTaskStatus.FAILED
        )

        answered = await client.post(
            f"/api/admin/long-tasks/j-fail/retry?owner_id={OWNER}"
        )

        assert answered.status_code == 200
        assert [record.id for record in submitter.submitted] == ["j-fail"]


async def test_retrying_clears_the_previous_run(tmp_path) -> None:
    """A record at ``accepted`` still carrying the last attempt's error or result
    reads as starting and answers as finished."""

    async with _surface(tmp_path) as (client, store, _submitter):
        await _seed_task(
            store, task_id="j-clear", owner_id=OWNER, status=LongTaskStatus.FAILED
        )

        body = (
            await client.post(f"/api/admin/long-tasks/j-clear/retry?owner_id={OWNER}")
        ).json()

        assert body["error_code"] is None
        assert body["error_message"] is None
        assert body["error_payload"] is None
        assert body["result_text"] is None
        assert body["artifact_paths"] == []
        assert body["completed_at"] is None


async def test_a_running_task_is_not_retried_under_its_worker(tmp_path) -> None:
    async with _surface(tmp_path) as (client, store, submitter):
        await _seed_task(store, task_id="j-live", owner_id=OWNER)

        answered = await client.post(
            f"/api/admin/long-tasks/j-live/retry?owner_id={OWNER}"
        )

        assert answered.status_code == 409
        assert submitter.submitted == []


async def test_a_succeeded_task_is_not_retried(tmp_path) -> None:
    """Its result is not this route's to discard. Asking for the work again is a
    new task, not a second run of an old one."""

    async with _surface(tmp_path) as (client, store, submitter):
        await _seed_task(
            store, task_id="j-ok", owner_id=OWNER, status=LongTaskStatus.SUCCEEDED
        )

        answered = await client.post(
            f"/api/admin/long-tasks/j-ok/retry?owner_id={OWNER}"
        )

        assert answered.status_code == 409
        assert submitter.submitted == []


async def test_a_host_with_no_worker_refuses_rather_than_pretending(tmp_path) -> None:
    """This is what the route used to do on every Host: answer with a task at
    ``accepted`` that nothing would ever pick up."""

    async with _surface(tmp_path, submitter=None) as (client, store, _submitter):
        await _seed_task(
            store, task_id="j-nowhere", owner_id=OWNER, status=LongTaskStatus.FAILED
        )

        answered = await client.post(
            f"/api/admin/long-tasks/j-nowhere/retry?owner_id={OWNER}"
        )

        assert answered.status_code == 503
        kept = await AgentLongTaskStore(store).get("j-nowhere")
        assert kept is not None
        # Untouched: a refusal must not leave the record halfway.
        assert kept.status is LongTaskStatus.FAILED


async def test_a_full_queue_is_not_reported_as_queued(tmp_path) -> None:
    async with _surface(tmp_path, submitter=_Submitter(full=True)) as (
        client,
        store,
        _submitter,
    ):
        await _seed_task(
            store, task_id="j-busy", owner_id=OWNER, status=LongTaskStatus.FAILED
        )

        answered = await client.post(
            f"/api/admin/long-tasks/j-busy/retry?owner_id={OWNER}"
        )

        assert answered.status_code == 503


async def test_another_owners_task_is_neither_cancelled_nor_retried(tmp_path) -> None:
    async with _surface(tmp_path) as (client, store, submitter):
        await _seed_task(store, task_id="j-theirs", owner_id="someone-else")

        cancelled = await client.post(
            f"/api/admin/long-tasks/j-theirs/cancel?owner_id={OWNER}"
        )
        retried = await client.post(
            f"/api/admin/long-tasks/j-theirs/retry?owner_id={OWNER}"
        )

        # 404 both ways, deliberately indistinguishable from a task that is not
        # there: an id must not be probeable for existence.
        assert (cancelled.status_code, retried.status_code) == (404, 404)
        assert submitter.submitted == []
