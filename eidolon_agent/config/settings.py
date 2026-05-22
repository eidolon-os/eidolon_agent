"""Process-level settings.

Loads ``config/config.yaml`` (or the path in ``$EIDOLON_AGENT_SETTINGS_YAML``).
Copy ``config/config.yaml.example`` to ``config/config.yaml`` before first run.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)


def _expand_all_paths(obj: object) -> None:
    """Recursively expand ``~`` in all Path fields of a pydantic model tree."""
    if not isinstance(obj, BaseModel):
        return
    for name in type(obj).model_fields:
        v = getattr(obj, name, None)
        if isinstance(v, Path):
            object.__setattr__(obj, name, v.expanduser())
        elif isinstance(v, BaseModel):
            _expand_all_paths(v)

# ---------------------------------------------------------------------------
# Section models
# ---------------------------------------------------------------------------


class GrpcSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tcp_host: str = "127.0.0.1"
    tcp_port: int = 50051
    uds_path: Path | None = None  # if set AND same host, auto-prefer UDS
    tls_cert_path: Path | None = None
    tls_key_path: Path | None = None
    mtls_ca_path: Path | None = None
    keepalive_time_s: int = 20
    keepalive_timeout_s: int = 5
    max_connection_idle_s: int = 600
    max_concurrent_streams_per_connection: int = 64


class HttpSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = "127.0.0.1"
    port: int = 8080
    admin_port: int = 8081
    cors_origins: list[str] = Field(default_factory=lambda: ["http://127.0.0.1:5281"])
    serve_admin_web_dist: bool = True
    admin_web_dist_path: Path = Path("admin_web/dist")


class NatsSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = "nats://127.0.0.1:4222"
    creds_path: Path | None = None
    connect_timeout_s: float = 5.0
    reconnect_max_attempts: int = -1  # infinite
    kv_buckets: list[str] = Field(
        default_factory=lambda: [
            "EIDOLON_CACHE",
            "EIDOLON_SESSION",
            "EIDOLON_HISTORY_WINDOW",
            "EIDOLON_RATELIMIT",
            "EIDOLON_CONFIG",
            "EIDOLON_FLAGS",
            "EIDOLON_EXP",
            "PAIRING_CODES",
            "DEVICE_REVOCATIONS",
            "EIDOLON_TOOL_IDEMP",
        ]
    )


class MemoryEndpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str
    mcp_url: str  # e.g. http://127.0.0.1:8030/mcp
    bearer_token: str | None = None


class MemorySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Prefer runtime discovery from eidolon-memory Discovery HTTP. Static endpoints
    # remain as a local fallback and for tests; the agent never reads memory
    # service config files.
    discovery_url: str | None = None
    discovery_token_env: str = ""
    discovery_refresh_s: int = 30
    discovery_timeout_s: float = 2.0
    endpoints: list[MemoryEndpoint] = Field(default_factory=list)
    recall_timeout_s: float = 0.2  # eidolon-memory has a 300ms hard budget


class SqliteSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: Path = Path("eidolon_agent.sqlite3")
    enable_wal: bool = True
    busy_timeout_ms: int = 5000
    journal_synchronous: Literal["NORMAL", "FULL"] = "NORMAL"


class LLMModelConfig(BaseModel):
    """One model entry. ``name`` uses LiteLLM convention: ``gpt-4o-mini``,
    ``claude-3-5-sonnet-latest``, ``ollama/llama3``, etc."""

    model_config = ConfigDict(extra="forbid")

    name: str
    api_key: str | None = None
    api_base: str | None = None
    timeout_s: float = 30.0


class LLMSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    models: list[LLMModelConfig] = Field(default_factory=list)
    default_model: str = "gpt-4o-mini"


class WorkstationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transport: Literal["nats", "grpc", "disabled"] = "nats"
    nats_submit_subject: str = "agent.workstation.task.submit"
    nats_progress_subject_prefix: str = "agent.workstation.task.progress."
    grpc_endpoint: str | None = None
    request_timeout_s: float = 5.0


class PersonaSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    templates_dir: Path = Path("eidolon_agent/domain/personas/templates")
    instances_dir: Path = Path("~/eidolon/personas/instances")
    watch_enabled: bool = True
    auto_evolution_enabled: bool = True


class ObservabilitySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_json: bool = True
    log_dir: Path = Path("logs")
    metrics_enabled: bool = True
    metrics_path: str = "/metrics"
    otel_enabled: bool = False
    otel_endpoint: str | None = None  # e.g. "http://localhost:4317"
    otel_service_name: str = "eidolon-agent"
    debug_snapshot_sample_rate: float = 0.01  # 1% turn snapshots dumped to debug/


class PairingSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    jwt_secret: str = ""
    jwt_algorithm: Literal["HS256", "RS256"] = "HS256"
    pairing_code_ttl_s: int = 600
    pairing_code_length: int = 8
    device_token_ttl_days: int = 30
    trusted_mtls_cn_whitelist: list[str] = Field(default_factory=list)


class RuntimeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    log_dir: Path = Path("~/eidolon/logs")
    run_dir: Path = Path("~/eidolon/run")
    debug_dir: Path = Path("~/eidolon/debug")
    warmup_enabled: bool = True
    recover_active_instances: bool = True
    drain_timeout_s: int = 30


class TurnSettings(BaseModel):
    """Per-turn budgets / SLOs."""

    model_config = ConfigDict(extra="forbid")

    compile_soft_timeout_ms: int = 100
    memory_recall_soft_timeout_ms: int = 200
    first_delta_slo_p50_ms: int = 200
    first_delta_slo_p95_ms: int = 300
    max_tool_iters: int = 4
    max_token_budget: int = 6000
    enable_filler_phrases: bool = True


# ---------------------------------------------------------------------------
# Root settings
# ---------------------------------------------------------------------------


class _YamlConfigSource(PydanticBaseSettingsSource):
    """Loads settings from a YAML file (path resolved per-call).

    Has lower precedence than env vars but higher than field defaults.
    """

    def __init__(self, settings_cls: type[BaseSettings], yaml_path: Path | None) -> None:
        super().__init__(settings_cls)
        self._yaml_path = yaml_path
        self._cache: dict[str, Any] | None = None

    def _load(self) -> dict[str, Any]:
        if self._cache is None:
            if self._yaml_path is None or not self._yaml_path.exists():
                self._cache = {}
            else:
                with self._yaml_path.open("r", encoding="utf-8") as f:
                    self._cache = yaml.safe_load(f) or {}
        return self._cache

    def get_field_value(self, field, field_name):
        data = self._load()
        if field_name in data:
            return data[field_name], field_name, False
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        return self._load()


class Settings(BaseSettings):
    """Process-level settings. One instance per process (cached singleton)."""

    model_config = SettingsConfigDict(extra="ignore")

    env: Literal["dev", "test", "prod"] = "dev"

    grpc: GrpcSettings = Field(default_factory=GrpcSettings)
    http: HttpSettings = Field(default_factory=HttpSettings)
    nats: NatsSettings = Field(default_factory=NatsSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    sqlite: SqliteSettings = Field(default_factory=SqliteSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    workstation: WorkstationSettings = Field(default_factory=WorkstationSettings)
    persona: PersonaSettings = Field(default_factory=PersonaSettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)
    pairing: PairingSettings = Field(default_factory=PairingSettings)
    runtime: RuntimeSettings = Field(default_factory=RuntimeSettings)
    turn: TurnSettings = Field(default_factory=TurnSettings)

    @model_validator(mode="after")
    def _expand_tilde_paths(self) -> Settings:
        _expand_all_paths(self)
        return self

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        """Precedence: init kwargs > YAML > defaults. No env var overrides."""
        yaml_path = _resolve_yaml_path()
        return (
            init_settings,
            _YamlConfigSource(settings_cls, yaml_path),
        )


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

_CONFIG_PATH = Path("config/config.yaml")


def _resolve_yaml_path() -> Path | None:
    explicit = os.environ.get("EIDOLON_AGENT_SETTINGS_YAML")
    if explicit:
        p = Path(explicit).expanduser()
        if not p.exists():
            raise FileNotFoundError(f"EIDOLON_AGENT_SETTINGS_YAML points to missing file: {p}")
        return p
    if _CONFIG_PATH.exists():
        return _CONFIG_PATH
    return None


def load_settings(*, yaml_path: Path | None = None) -> Settings:
    """Construct a Settings instance.

    Loads ``config/config.yaml`` by default.  Tests can override by passing
    ``yaml_path`` or by setting ``$EIDOLON_AGENT_SETTINGS_YAML``.
    """
    if yaml_path is not None:
        os.environ["EIDOLON_AGENT_SETTINGS_YAML"] = str(yaml_path)
    return Settings()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached singleton accessor. Reset via ``get_settings.cache_clear()`` in tests."""
    return load_settings()
