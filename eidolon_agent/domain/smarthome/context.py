"""Short-lived home conversation state, separate from Companion memory."""

from __future__ import annotations

import time
from dataclasses import dataclass

from eidolon_sdk.biz.interpretation import Proposal


@dataclass(frozen=True)
class HomeClarification:
    question: str


@dataclass(frozen=True)
class HomeCancellation:
    pass


HomeUnderstanding = Proposal | HomeClarification | HomeCancellation | None


@dataclass
class HomeContext:
    utterance: str = ""
    proposal: Proposal | None = None
    question: str | None = None
    pending: bool = False
    expires_at: float = 0
    active: bool = True

    def clear(self) -> None:
        self.utterance = ""
        self.proposal = None
        self.question = None
        self.pending = False
        self.expires_at = 0

    def remember(self, utterance: str, proposal: Proposal | None, *, question: str | None = None) -> None:
        self.utterance = utterance
        self.proposal = proposal
        self.question = question
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
        }
