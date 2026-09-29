"""Short-lived home conversation state, separate from Companion memory."""

from __future__ import annotations

import time
from dataclasses import dataclass

from eidolon_sdk.biz.interpretation import Action, Proposal


@dataclass(frozen=True)
class HomeClarification:
    question: str
    targets: tuple[str, ...] = ()
    action: Action | None = None


@dataclass(frozen=True)
class HomeCancellation:
    pass


HomeUnderstanding = Proposal | HomeClarification | HomeCancellation | None


@dataclass
class HomeContext:
    utterance: str = ""
    proposal: Proposal | None = None
    question: str | None = None
    clarification: HomeClarification | None = None
    response: str = ""
    pending_action: str = ""
    pending: bool = False
    expires_at: float = 0
    active: bool = True
    revision: int = 0

    def clear(self) -> None:
        self.utterance = ""
        self.proposal = None
        self.question = None
        self.clarification = None
        self.response = ""
        self.pending_action = ""
        self.pending = False
        self.expires_at = 0

    def remember(self, utterance: str, proposal: Proposal | None, *, question: str | None = None,
                 clarification: HomeClarification | None = None, response: str = "",
                 pending_action: str = "") -> None:
        self.utterance = utterance
        self.proposal = proposal
        self.question = question
        self.clarification = clarification
        self.response = response
        self.pending_action = pending_action
        self.pending = question is not None
        self.expires_at = time.monotonic() + 30

    def snapshot(self) -> dict | None:
        if not self.active or time.monotonic() >= self.expires_at:
            self.clear()
            return None
        return {
            "previous_utterance": self.utterance,
            "proposal": self.proposal.model_dump(mode="json") if self.proposal else None,
            "pending": self.pending,
            "question": self.question,
            "response": self.response,
            "pending_action": self.pending_action,
            "known_targets": list(self.clarification.targets) if self.clarification else [],
            "known_action": self.clarification.action.model_dump(mode="json")
                if self.clarification and self.clarification.action else None,
        }
