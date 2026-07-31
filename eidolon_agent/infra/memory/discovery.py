"""Runtime discovery for the external eidolon-memory service.

The agent only consumes memory's public control-plane contract. It does not
read memory config files, import memory code, or inspect palace paths.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from eidolon_memory_contracts import memory_space_subject_token
from pydantic import BaseModel, ConfigDict, Field

from eidolon_agent.config.settings import MemoryEndpoint, MemorySettings, NatsSettings

_log = logging.getLogger(__name__)


class DiscoveryMcpAuth(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: str = "none"
    token_env: str | None = None


class DiscoveryMemoryRealm(BaseModel):
    model_config = ConfigDict(extra="ignore")

    memory_space_id: str
    memory_realm_id: str | None = None
    owner_id: str | None = None
    companion_id: str | None = None
    enabled: bool = True
    mcp_http_url: str
    mcp_auth: DiscoveryMcpAuth | None = None
    agent_reachable: bool = True


class DiscoveryNats(BaseModel):
    model_config = ConfigDict(extra="ignore")

    url: str
    stream: str = "MEMORY_TURNS"
    turn_subject_template: str = "eidolon.memory.turn.{memory_space_token}"
    cmd_subject_template: str = "eidolon.memory.cmd.{memory_space_token}"


class DiscoveryResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    version: int = 1
    generated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    nats: DiscoveryNats
    memory_realms: list[DiscoveryMemoryRealm] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class MemoryRoute:
    memory_space_id: str
    mcp_url: str
    bearer_token: str | None = None
    enabled: bool = True
    reachable: bool = True


@dataclass(frozen=True, slots=True)
class MemoryNatsRoute:
    url: str
    stream: str
    turn_subject_template: str
    cmd_subject_template: str


class MemoryRoutingTable:
    """Concurrency-safe routing snapshot for MCP reads and NATS writes."""

    def __init__(
        self,
        *,
        nats: MemoryNatsRoute,
        routes: dict[str, MemoryRoute] | None = None,
        source: str = "static",
    ) -> None:
        self._nats = nats
        self._routes = routes or {}
        self._source = source
        self._lock = asyncio.Lock()

    @classmethod
    def from_static(
        cls,
        *,
        endpoints: list[MemoryEndpoint],
        nats: NatsSettings,
    ) -> MemoryRoutingTable:
        routes = {
            e.memory_space_id: MemoryRoute(
                memory_space_id=e.memory_space_id,
                mcp_url=e.mcp_url,
                bearer_token=e.bearer_token,
                enabled=True,
                reachable=True,
            )
            for e in endpoints
        }
        return cls(
            nats=MemoryNatsRoute(
                url=nats.url,
                stream="",
                turn_subject_template="eidolon.memory.turn.{memory_space_token}",
                cmd_subject_template="eidolon.memory.cmd.{memory_space_token}",
            ),
            routes=routes,
            source="static",
        )

    async def replace_from_discovery(self, discovery: DiscoveryResponse) -> None:
        routes: dict[str, MemoryRoute] = {}
        for realm in discovery.memory_realms:
            token: str | None = None
            auth = realm.mcp_auth
            if auth and auth.type.lower() == "bearer" and auth.token_env:
                token = os.environ.get(auth.token_env, "").strip() or None
            routes[realm.memory_space_id] = MemoryRoute(
                memory_space_id=realm.memory_space_id,
                mcp_url=realm.mcp_http_url,
                bearer_token=token,
                enabled=realm.enabled,
                reachable=realm.agent_reachable,
            )
        async with self._lock:
            self._nats = MemoryNatsRoute(
                url=discovery.nats.url,
                stream=discovery.nats.stream,
                turn_subject_template=discovery.nats.turn_subject_template,
                cmd_subject_template=discovery.nats.cmd_subject_template,
            )
            self._routes = routes
            self._source = "discovery"

    async def route_for(self, memory_space_id: str) -> MemoryRoute | None:
        route, _reason = await self.route_status_for(memory_space_id)
        return route

    async def route_status_for(self, memory_space_id: str) -> tuple[MemoryRoute | None, str | None]:
        async with self._lock:
            route = self._routes.get(memory_space_id)
            if route is None:
                return None, "no_memory_route"
            if not route.enabled:
                return None, "memory_route_disabled"
            if not route.reachable:
                return None, "memory_route_unreachable"
            return route, None

    async def endpoint_count(self) -> int:
        async with self._lock:
            return sum(1 for route in self._routes.values() if route.enabled and route.reachable)

    async def memory_space_ids(self) -> list[str]:
        async with self._lock:
            return [
                memory_space_id
                for memory_space_id, route in self._routes.items()
                if route.enabled and route.reachable
            ]

    async def nats_url(self) -> str:
        async with self._lock:
            return self._nats.url

    async def source(self) -> str:
        async with self._lock:
            return self._source

    async def render_turn_subject(self, memory_space_id: str) -> str:
        async with self._lock:
            template = self._nats.turn_subject_template
        return _render_subject(template, memory_space_id, "eidolon.memory.turn.{memory_space_token}")

    async def render_cmd_subject(self, memory_space_id: str) -> str:
        async with self._lock:
            template = self._nats.cmd_subject_template
        return _render_subject(template, memory_space_id, "eidolon.memory.cmd.{memory_space_token}")


class MemoryDiscoveryClient:
    def __init__(
        self,
        *,
        discovery_url: str,
        token_env: str = "EIDOLON_MEMORY_ADMIN_TOKEN",
        timeout_s: float = 2.0,
    ) -> None:
        self._url = discovery_url
        self._token_env = token_env
        self._timeout = timeout_s

    async def fetch(self) -> DiscoveryResponse:
        headers: dict[str, str] = {}
        token = os.environ.get(self._token_env, "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        async with httpx.AsyncClient(
            timeout=self._timeout,
            follow_redirects=True,
            # Discovery is an internal control-plane call, usually loopback.
            # In dev, shell HTTP_PROXY often points at a local proxy such as
            # :7890; letting httpx inherit that turns healthy localhost
            # discovery into proxy-sourced 502s.
            trust_env=False,
        ) as client:
            resp = await client.get(self._url, headers=headers or None)
            resp.raise_for_status()
            return DiscoveryResponse.model_validate(resp.json())


class MemoryDiscoveryRefresher:
    """Background updater for memory routing discovery."""

    def __init__(
        self,
        *,
        client: MemoryDiscoveryClient,
        routes: MemoryRoutingTable,
        interval_s: int,
        expected_nats_url: str,
    ) -> None:
        self._client = client
        self._routes = routes
        self._interval = max(5, interval_s)
        self._expected_nats_url = expected_nats_url
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="memory-discovery-refresh")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def refresh_once(self) -> bool:
        """Fetch discovery immediately and replace the routing snapshot.

        Pairing/onboarding uses this to avoid a stale-route race right after
        admin provisions a new memory user. Failures are best-effort and
        return False; the caller can still report the existing route status.
        """
        try:
            discovery = await self._client.fetch()
            if discovery.nats.url != self._expected_nats_url:
                _log.warning(
                    "memory discovery nats url changed from %s to %s; restart required",
                    self._expected_nats_url,
                    discovery.nats.url,
                )
            await self._routes.replace_from_discovery(discovery)
            return True
        except Exception:
            _log.exception("memory discovery refresh failed")
            return False

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
                return
            except asyncio.TimeoutError:
                pass
            await self.refresh_once()


async def build_initial_memory_routes(
    *,
    memory: MemorySettings,
    nats: NatsSettings,
    log_initial_fetch_exception: bool = True,
) -> tuple[MemoryRoutingTable, str, MemoryDiscoveryRefresher | None]:
    """Build routes, preferring discovery and falling back to static endpoints."""
    routes = MemoryRoutingTable.from_static(endpoints=memory.endpoints, nats=nats)
    effective_nats_url = nats.url
    refresher: MemoryDiscoveryRefresher | None = None
    if not memory.discovery_url:
        return routes, effective_nats_url, None

    client = MemoryDiscoveryClient(
        discovery_url=memory.discovery_url,
        token_env=memory.discovery_token_env,
        timeout_s=memory.discovery_timeout_s,
    )
    try:
        discovery = await client.fetch()
        await routes.replace_from_discovery(discovery)
        effective_nats_url = discovery.nats.url
    except Exception:
        if log_initial_fetch_exception:
            _log.exception("memory discovery initial fetch failed; using static endpoints")
        else:
            _log.debug("memory discovery initial fetch failed; using static endpoints")

    refresher = MemoryDiscoveryRefresher(
        client=client,
        routes=routes,
        interval_s=memory.discovery_refresh_s,
        expected_nats_url=effective_nats_url,
    )
    return routes, effective_nats_url, refresher


def _render_subject(template: str, memory_space_id: str, fallback_template: str) -> str:
    token = memory_space_subject_token(memory_space_id)
    try:
        rendered = template.format(
            memory_space_id=token,
            memory_space_token=token,
            raw_memory_space_id=memory_space_id,
        )
    except Exception:
        rendered = ""
    if rendered:
        return rendered
    _log.warning(
        "invalid memory subject template %r; using fallback %s", template, fallback_template
    )
    return fallback_template.format(memory_space_id=token, memory_space_token=token)
