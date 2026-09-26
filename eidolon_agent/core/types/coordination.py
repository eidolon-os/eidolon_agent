"""Attributed public context for a reply scheduled by the application layer.

Never reconstructed from arbitrary StartTurn metadata. Runtime identity remains
on TurnContext; a public message's author cannot choose the answering runtime.
"""

from dataclasses import dataclass

from eidolon_sdk.biz.participation import Context, Message


@dataclass(frozen=True, slots=True)
class CoordinatedInput:
    context_ref: str
    trigger: Message
    public_context: Context

    @property
    def user_text(self) -> str:
        return self.trigger.text if self.trigger.author_kind == "user" else ""
