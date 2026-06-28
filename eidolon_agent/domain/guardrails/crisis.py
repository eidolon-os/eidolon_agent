"""Crisis-mode handler.

Activates on :data:`SafetyAction.ESCALATE`. Returns a pre-vetted empathetic
response, signals "privacy mode" (turn won't be fanned out to memory), and
emits an audit event.
"""

from __future__ import annotations

from dataclasses import dataclass

from eidolon_agent.core.types.event import Event


@dataclass(frozen=True, slots=True)
class CrisisResponse:
    text: str
    suppress_memory_write: bool = True
    crisis_resources: tuple[str, ...] = ()


# zh-CN crisis resources. en-US etc added via locale dispatch.
_RESOURCES_ZH = (
    "北京心理危机研究与干预中心：010-82951332 / 010-62716714（24h）",
    "全国心理援助热线：400-161-9995（24h）",
)

_DEFAULT_REPLY_ZH = (
    "我听到你了，谢谢你愿意告诉我。我很担心你现在的感受。"
    "你现在不需要一个人扛——可以拨打这些电话和专业的人聊聊。"
    "我会一直在这。"
)


class CrisisHandler:
    def __init__(self, *, event_bus=None) -> None:
        self._bus = event_bus

    async def handle(
        self,
        *,
        companion_id: str,
        owner_id: str,
        locale: str = "zh-CN",
    ) -> CrisisResponse:
        if self._bus is not None:
            await self._bus.publish(
                Event(
                    subject=f"agent.guardrail.crisis.{companion_id}",
                    payload={"owner_id": owner_id, "locale": locale},
                    source="guardrails.crisis",
                )
            )
        # We only ship zh-CN resources here; extend by locale dispatch later.
        return CrisisResponse(
            text=_DEFAULT_REPLY_ZH,
            suppress_memory_write=True,
            crisis_resources=_RESOURCES_ZH,
        )
