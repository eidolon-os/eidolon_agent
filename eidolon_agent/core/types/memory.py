"""Memory types — interface with the external eidolon-memory service.

eidolon-memory exposes MCP for synchronous reads and NATS JetStream for
asynchronous writes. We mirror its terminology where possible (wing/room/drawer
are eidolon-memory internals — we don't surface them here).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Literal


class MemoryScope(str, Enum):
    ALL = "all"
    WORKING = "working"  # current session (handled locally by HistoryManager)
    EPISODIC = "episodic"  # recent days
    SEMANTIC = "semantic"  # facts/preferences/promises
    PROMISE = "promise"  # pending promises only


class MemoryKind(str, Enum):
    FRAGMENT = "fragment"
    FACT = "fact"
    PREFERENCE = "preference"
    PROMISE = "promise"
    EPISODE = "episode"


@dataclass(frozen=True, slots=True)
class MemoryItem:
    """A memory record to be written. Equivalent to a memory-service fragment."""

    content: str
    kind: MemoryKind
    importance: int = 3  # 1..5
    confidence: float = 0.9
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MemoryHit:
    """A retrieved memory record returned by the memory service."""

    id: str
    content: str
    kind: MemoryKind
    similarity: float
    memory_time: datetime | None = None
    memory_time_source: str | None = None
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MemoryRecallResult:
    """Prompt-ready memory recall result.

    ``degraded_reason`` is for operator traces, not prompt injection.
    """

    context: str = ""
    hits: list[MemoryHit] = field(default_factory=list)
    degraded: bool = False
    degraded_reason: str | None = None
    kg_triples: list[dict] = field(default_factory=list)
    diagnostics: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ActiveCommitment:
    """One current Realm-bound commitment exposed to companion context."""

    commitment_id: str
    promisor: str
    predicate: Literal["promised", "committed_to", "planned_to"]
    action: str
    status: Literal["proposed", "confirmed"]
    beneficiaries: tuple[str, ...] = ()
    participants: tuple[str, ...] = ()
    condition: str | None = None
    due_at: str | None = None
    revision: int = 1
    updated_at: str = ""


@dataclass(frozen=True, slots=True)
class ActiveCommitmentReadResult:
    """Bounded product read result; terminal commitments are never included."""

    commitments: list[ActiveCommitment] = field(default_factory=list)
    total: int = 0
    truncated: bool = False
    degraded: bool = False
    degraded_reason: str | None = None


@dataclass(frozen=True, slots=True)
class MemoryForgetCandidate:
    id: str
    content: str
    score: float = 0.0


@dataclass(frozen=True, slots=True)
class MemoryForgetPreview:
    """Read-only resolution of a natural-language privacy request."""

    status: Literal["preview", "not_found", "too_broad", "unavailable", "failed"]
    target: str
    action: Literal["archive", "delete"]
    candidates: list[MemoryForgetCandidate] = field(default_factory=list)
    requires_explicit_confirmation: bool = False
    confirmation_token: str = ""
    expires_at: str = ""
    error: str = ""


@dataclass(frozen=True, slots=True)
class MemoryForgetOutcome:
    """Terminal or accepted result of an exact-ID privacy command."""

    status: Literal["accepted", "applied", "failed", "unavailable"]
    action: Literal["archive", "delete"]
    request_id: str = ""
    drawer_ids: list[str] = field(default_factory=list)
    error: str = ""


@dataclass(frozen=True, slots=True)
class MemoryQueryPlan:
    """How the strategy plans to query memory for a single turn."""

    working_k: int = 20
    episodic_query: str = ""
    episodic_k: int = 3
    semantic_query: str = ""
    semantic_k: int = 5
    promise_force: bool = True
    voice: bool = True  # honour memory-service 50ms KG sub-budget
    kg_subjects: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MemoryWritePolicy:
    write_on_turn: bool = True
    write_on_reflection: bool = True
    dedup_threshold: float = 0.92
    conflict_resolver: Literal["newer_wins", "llm_merge", "keep_both"] = "newer_wins"


class MemoryWriteDispositionKind(str, Enum):
    IGNORE = "ignore"
    EPISODIC_ONLY = "episodic_only"
    SEMANTIC_UPSERT = "semantic_upsert"
    PROMISE_CREATE = "promise_create"
    SENSITIVE_REQUIRES_CONSENT = "sensitive_requires_consent"


@dataclass(frozen=True, slots=True)
class MemoryWriteDisposition:
    kind: MemoryWriteDispositionKind
    reason: str

    def to_metadata(self) -> dict[str, str]:
        return {
            "memory_write_disposition": self.kind.value,
            "memory_write_reason": self.reason,
            "memory_policy_version": "agent_memory_policy.v1",
        }


@dataclass(frozen=True, slots=True)
class MemoryWriteOutcome:
    """Truthful durable outcome for one explicit memory write."""

    status: Literal["accepted", "retrying", "applied", "failed", "unknown"]
    request_id: str
    resource_id: str | None = None
    error: str | None = None

    @property
    def completed(self) -> bool:
        return self.status == "applied"


def classify_memory_write(
    *,
    user_text: str,
    assistant_text: str = "",
) -> MemoryWriteDisposition:
    # The user's utterance is the source of truth for write disposition.
    # Assistant replies often contain conversational words like "今天" or "吗",
    # which must not turn a stable preference/fact into an episodic event or
    # question. Keep assistant text only for future policy extensions, not for
    # today's deterministic signal extraction.
    del assistant_text
    text = user_text.lower()
    if any(k in text for k in ("身份证", "银行卡", "password", "密码", "住址")):
        return MemoryWriteDisposition(
            MemoryWriteDispositionKind.SENSITIVE_REQUIRES_CONSENT,
            "sensitive_personal_data",
        )
    if any(k in text for k in ("提醒我", "记得提醒", "remind me", "promise")):
        return MemoryWriteDisposition(
            MemoryWriteDispositionKind.PROMISE_CREATE,
            "explicit_promise_or_reminder",
        )
    is_question = any(k in text for k in ("?", "？", "吗", "哪里", "什么"))
    strong_preference_markers = (
        "我喜欢",
        "我不喜欢",
        "以后叫我",
        "以后请叫我",
        "请叫我",
        "call me",
        "i like",
        "i prefer",
    )
    weak_preference_markers = ("叫我",)
    residence_markers = (
        "我住在",
        "我现在住在",
        "现在住在",
        "住在",
    )
    stable_identity_markers = (
        "我目前在",
        "我现在在",
        "我大学",
        "我的大学",
        "大学是",
        "大学在",
        "学校是",
        "毕业于",
        "就读于",
        "读的",
        "工作",
        "上班",
        "公司",
        "i work",
        "i study",
        "my university",
        "graduated from",
    )
    if (
        any(k in text for k in strong_preference_markers)
        or (not is_question and any(k in text for k in weak_preference_markers))
    ) or (
        not is_question
        and (
            any(k in text for k in residence_markers)
            or any(k in text for k in stable_identity_markers)
        )
    ):
        return MemoryWriteDisposition(
            MemoryWriteDispositionKind.SEMANTIC_UPSERT,
            "stable_preference_or_identity",
        )
    if any(k in text for k in ("今天", "昨天", "刚才", "today", "yesterday", "this morning")):
        return MemoryWriteDisposition(
            MemoryWriteDispositionKind.EPISODIC_ONLY,
            "episodic_event",
        )
    return MemoryWriteDisposition(MemoryWriteDispositionKind.IGNORE, "low_signal_chitchat")
