"""Context provider backed by PersonasService."""

from __future__ import annotations

from eidolon_agent.core.ports.context import ProviderContext
from eidolon_agent.core.types.context import ContextSegment, SegmentType, SegmentWeight


class PersonasContextProvider:
    name = "personas"
    segment_type = SegmentType.PERSONA
    default_weight = SegmentWeight.CRITICAL
    soft_timeout_s = 0.25

    def __init__(self, *, personas_service, instance_locator) -> None:
        self._service = personas_service
        self._locator = instance_locator

    async def provide(self, ctx: ProviderContext) -> list[ContextSegment]:
        ti = ctx.turn_input
        instance_id, template_id = self._locator(
            ti.caller.tenant_id,
            ti.caller.user_id,
            ti.conversation_id,
        )
        compiled = await self._service.compile_prompt(
            tenant_id=ti.caller.tenant_id,
            user_id=ti.caller.user_id,
            instance_id=instance_id,
            template_id=template_id,
            user_text=ti.text or "",
        )
        return [
            ContextSegment(
                type=self.segment_type,
                weight=self.default_weight,
                content=compiled.system_prompt,
                tokens=max(1, len(compiled.system_prompt) // 3),
                source=self.name,
                tags=("persona", template_id),
                metadata={"debug_trace": list(compiled.debug_trace)},
            )
        ]

