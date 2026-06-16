"""Process-level settings.

Loads ``config/settings.yaml`` and ``config/.env`` (or overrides via
``EIDOLON_AGENT_SETTINGS_YAML`` / ``EIDOLON_AGENT_ENV_FILE``).
Copy templates from ``config/settings.example.yaml`` and ``config/.env.example``
before first run (see README §10).
"""

from __future__ import annotations

import os
import re
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

_ENV_PLACEHOLDER_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def is_env_placeholder(value: str) -> bool:
    v = (value or "").strip()
    return bool(v) and _ENV_PLACEHOLDER_RE.fullmatch(v) is not None


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
    tcp_port: int = 45051  # outside LiveKit RTC range 50000-60000 (see eidolon_admin ports.yaml)
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
    port: int = 8180
    admin_port: int = 8081
    cors_origins: list[str] = Field(default_factory=list)


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
    ``claude-3-5-sonnet-latest``, ``ollama/llama3``, etc.

    API keys must live in ``config/.env`` (``EIDOLON_AGENT_LLM_API_KEY``), not yaml.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    api_base: str | None = None
    timeout_s: float = 30.0

    @model_validator(mode="before")
    @classmethod
    def _reject_inline_api_key(cls, data: Any) -> Any:
        if isinstance(data, dict) and (data.get("api_key") or "").strip():
            raise ValueError(
                "llm.models[].api_key is not allowed in yaml — set "
                "EIDOLON_AGENT_LLM_API_KEY in config/.env"
            )
        if isinstance(data, dict):
            data.pop("api_key", None)
        return data

    def resolved_api_key(self) -> str | None:
        key = os.environ.get("EIDOLON_AGENT_LLM_API_KEY", "").strip()
        return key or None


class LLMSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_key: str = ""  # yaml placeholder: EIDOLON_AGENT_LLM_API_KEY
    models: list[LLMModelConfig] = Field(default_factory=list)
    default_model: str = "gpt-4o-mini"
    fallback_models: list[str] = Field(default_factory=list)
    max_retries: int = 2
    startup_warm_enabled: bool = True
    startup_warm_timeout_s: float = 10.0
    shared_http_client: bool = True

    @model_validator(mode="before")
    @classmethod
    def _normalize_api_key_placeholder(cls, data: Any) -> Any:
        if isinstance(data, dict):
            val = (data.get("api_key") or "").strip()
            if val and val != "EIDOLON_AGENT_LLM_API_KEY":
                raise ValueError(
                    "llm.api_key must be empty or the placeholder "
                    "EIDOLON_AGENT_LLM_API_KEY; set the secret in config/.env"
                )
            data.pop("api_key", None)
        return data


class LongTaskSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transport: Literal["mementos_http", "disabled"] = "mementos_http"
    mementos_base_url: str = "http://127.0.0.1:18765"
    queue_size: int = 256
    worker_poll_interval_s: float = 1.0
    worker_task_timeout_s: float = 1800.0
    worker_http_timeout_s: float = 30.0
    worker_lease_s: float = 3600.0


class PersonaSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    templates_dir: Path = Path("eidolon_agent/domain/personas/templates")
    # ``instances_dir`` is kept as a migration source for the YAML→SQLite
    # script and as a fallback when ``storage`` is set to "yaml".
    instances_dir: Path = Path("~/eidolon/personas/instances")
    # Production default. "yaml" is preserved for ops scenarios where reading
    # the raw on-disk overlay is easier than poking SQLite.
    storage: Literal["sqlite", "yaml"] = "sqlite"
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

    jwt_secret: str = ""  # yaml placeholder: PAIRING_JWT_SECRET
    jwt_algorithm: Literal["HS256", "RS256"] = "HS256"

    @model_validator(mode="before")
    @classmethod
    def _normalize_jwt_secret_placeholder(cls, data: Any) -> Any:
        if isinstance(data, dict):
            val = (data.get("jwt_secret") or "").strip()
            if val and val != "PAIRING_JWT_SECRET":
                raise ValueError(
                    "pairing.jwt_secret must be empty or the placeholder "
                    "PAIRING_JWT_SECRET; set the secret in config/.env"
                )
            if val == "PAIRING_JWT_SECRET":
                data["jwt_secret"] = ""
        return data
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

    context_budget_mode: Literal["enabled", "shadow", "disabled"] = "enabled"
    memory_write_mode: Literal["enabled", "shadow", "disabled"] = "enabled"
    tool_schema_strict: bool = True
    require_idempotency_for_side_effect_tools: bool = False
    tool_batch_timeout_s: float | None = None
    compile_soft_timeout_ms: int = 100
    memory_recall_soft_timeout_ms: int = 200
    first_delta_slo_p50_ms: int = 200
    first_delta_slo_p95_ms: int = 300
    history_context_window: int = 4
    max_tool_iters: int = 4
    max_token_budget: int = 6000
    tool_schema_budget_tokens: int = 800
    output_reserve_tokens: int = 500
    enable_filler_phrases: bool = True


# ---------------------------------------------------------------------------
# Root settings
# ---------------------------------------------------------------------------


class _YamlConfigSource(PydanticBaseSettingsSource):
    """Loads settings from a YAML file (path resolved per-call).

    Has lower precedence than env vars but higher than field defaults.
    """

    def __init__(self, settings_cls: type[BaseSettings], yaml_path: Path) -> None:
        super().__init__(settings_cls)
        self._yaml_path = yaml_path
        self._cache: dict[str, Any] | None = None

    def _load(self) -> dict[str, Any]:
        if self._cache is None:
            with self._yaml_path.open("r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            _reject_inline_secrets(data)
            self._cache = data
        return self._cache

    def get_field_value(self, field, field_name):
        data = self._load()
        if field_name in data:
            return data[field_name], field_name, False
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        return self._load()


_REPO_ROOT = Path(__file__).resolve().parents[2]
_SETTINGS_YAML = _REPO_ROOT / "config" / "settings.yaml"
_DEFAULT_ENV = _REPO_ROOT / "config" / ".env"


class Settings(BaseSettings):
    """Process-level settings. One instance per process (cached singleton)."""

    model_config = SettingsConfigDict(
        extra="ignore",
        env_file_encoding="utf-8",
    )

    env: Literal["dev", "test", "prod"] = "dev"

    grpc: GrpcSettings = Field(default_factory=GrpcSettings)
    http: HttpSettings = Field(default_factory=HttpSettings)
    nats: NatsSettings = Field(default_factory=NatsSettings)
    memory: MemorySettings = Field(default_factory=MemorySettings)
    sqlite: SqliteSettings = Field(default_factory=SqliteSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    long_task: LongTaskSettings = Field(default_factory=LongTaskSettings)
    persona: PersonaSettings = Field(default_factory=PersonaSettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)
    pairing: PairingSettings = Field(default_factory=PairingSettings)
    runtime: RuntimeSettings = Field(default_factory=RuntimeSettings)
    turn: TurnSettings = Field(default_factory=TurnSettings)

    @model_validator(mode="after")
    def _expand_tilde_paths(self) -> Settings:
        _expand_all_paths(self)
        return self

    @model_validator(mode="after")
    def _pairing_secret_from_env(self) -> Settings:
        env_secret = os.environ.get("PAIRING_JWT_SECRET", "").strip()
        if env_secret and not self.pairing.jwt_secret:
            return self.model_copy(
                update={
                    "pairing": self.pairing.model_copy(update={"jwt_secret": env_secret})
                }
            )
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
        """Precedence: init > shell env > .env > yaml > defaults."""
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            _YamlConfigSource(settings_cls, _resolve_yaml_path()),
        )


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------


def _reject_inline_secrets(obj: Any, *, path: str = "") -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else k
            if (
                k.lower() in ("api_key", "secret", "token")
                and isinstance(v, str)
                and v.strip()
                and not is_env_placeholder(v)
            ):
                raise ValueError(
                    f"inline secret not allowed at {p}; use config/.env "
                    f"(yaml placeholder = env var name)"
                )
            _reject_inline_secrets(v, path=p)
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            _reject_inline_secrets(item, path=f"{path}[{i}]")


def _resolve_yaml_path() -> Path:
    explicit = os.environ.get("EIDOLON_AGENT_SETTINGS_YAML", "").strip()
    if explicit:
        p = Path(explicit).expanduser()
        if not p.is_file():
            raise FileNotFoundError(f"EIDOLON_AGENT_SETTINGS_YAML points to missing file: {p}")
        return p.resolve()
    if _SETTINGS_YAML.is_file():
        return _SETTINGS_YAML
    raise FileNotFoundError(
        f"settings file not found: {_SETTINGS_YAML}. "
        f"Copy config/settings.example.yaml to {_SETTINGS_YAML} (see README §10)"
    )


def _resolve_env_path() -> Path:
    explicit = os.environ.get("EIDOLON_AGENT_ENV_FILE", "").strip()
    if explicit:
        p = Path(explicit).expanduser()
        if not p.is_file():
            raise FileNotFoundError(f"EIDOLON_AGENT_ENV_FILE points to missing file: {p}")
        return p.resolve()
    p = _DEFAULT_ENV
    if not p.is_file():
        raise FileNotFoundError(
            f"env file not found: {p}. Copy config/.env.example to config/.env"
        )
    return p.resolve()


def load_settings(*, yaml_path: Path | None = None) -> Settings:
    """Construct a Settings instance.

    Loads ``config/settings.yaml`` by default.  Tests can override by passing
    ``yaml_path`` or by setting ``$EIDOLON_AGENT_SETTINGS_YAML``.
    """
    if yaml_path is not None:
        os.environ["EIDOLON_AGENT_SETTINGS_YAML"] = str(yaml_path)
    from dotenv import load_dotenv

    load_dotenv(_resolve_env_path(), override=False)
    return Settings()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached singleton accessor. Reset via ``get_settings.cache_clear()`` in tests."""
    return load_settings()
