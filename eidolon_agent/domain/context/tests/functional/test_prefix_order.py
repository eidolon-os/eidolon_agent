"""The assembled prompt's order, held to the volatility classes it declares.

A prefix is reusable only up to its first changed byte, so a volatile segment
costs every token after it as well as itself. That makes the order a
correctness-adjacent property rather than an editorial one: with `realtime`
(whose only change between two turns can be a two-decimal float) in front of
the `append_only` history block, RK3588 re-read 113 tokens — 5.65 s. With
history in front of it, 27 tokens — 1.69 s.

The taxonomy and the per-kind classification already existed in `types.py`;
what did not exist was anything making the assembly use them. These tests are
that.
"""

from __future__ import annotations

import pytest

from eidolon_agent.domain.context.types import (
    SEGMENT_ORDER,
    ContextSegmentKind,
    segment_volatility,
    volatility_rank,
)


def test_every_kind_is_classified() -> None:
    """An unclassified kind falls back to `volatile`, which is the safe default
    for correctness and the worst one for cost — so it must be deliberate."""

    for kind in ContextSegmentKind:
        assert segment_volatility(kind) in SEGMENT_ORDER, kind


def test_the_reusable_classes_come_before_the_volatile_ones() -> None:
    """This is the whole mechanism: anything whose bytes survive between turns
    has to precede anything that does not."""

    assert volatility_rank(ContextSegmentKind.PERSONA) < volatility_rank(
        ContextSegmentKind.HISTORY
    )
    assert volatility_rank(ContextSegmentKind.HISTORY) < volatility_rank(
        ContextSegmentKind.REALTIME
    )
    assert volatility_rank(ContextSegmentKind.HISTORY) < volatility_rank(
        ContextSegmentKind.MEMORY
    )
    # The question is last: an answer must not be predicted from stale context
    # sitting after it.
    for kind in ContextSegmentKind:
        if kind is not ContextSegmentKind.CURRENT_USER:
            assert volatility_rank(kind) <= volatility_rank(ContextSegmentKind.CURRENT_USER)


def test_history_is_append_only_and_persona_state_is_not() -> None:
    """The two classifications that carry the cost.

    History grows at the tail, so its older entries are byte-identical and
    belong in the reusable prefix. Per-turn persona state (mood, energy) does
    not — which is why it was split out of PERSONA in the first place, and why
    putting it back would silently undo that split.
    """

    assert segment_volatility(ContextSegmentKind.HISTORY) == "append_only"
    assert segment_volatility(ContextSegmentKind.PERSONA) == "stable"
    assert segment_volatility(ContextSegmentKind.PERSONA_STATE) == "volatile"


def test_the_order_is_declared_once() -> None:
    """`SEGMENT_ORDER` is where the cache-versus-recency trade-off is decided.
    A second copy of it is a second thing to keep true."""

    import inspect

    from eidolon_agent.domain.context import compiler

    source = inspect.getsource(compiler)
    # The compiler sorts by the rank, and does not spell the classes out.
    assert "volatility_rank" in source
    for name in SEGMENT_ORDER:
        assert f'"{name}"' not in source, (
            f"the compiler names the volatility class {name!r} itself; it should "
            "ask volatility_rank instead"
        )


async def test_the_assembled_prompt_puts_history_before_the_volatile_blocks() -> None:
    """The property as the model sees it, not as the table declares it.

    Read off the emitted system message: a prefix cache keys on these bytes,
    and the table would be worth nothing if the assembly ignored it. Built with
    the same scaffolding as the compiler's own tests so it exercises the real
    assembly rather than a paraphrase of it.
    """

    import uuid
    from datetime import UTC, datetime

    from eidolon_agent.core.types.messages import ChatMessage, MessageRole
    from eidolon_agent.domain.context.compiler import ContextCompiler
    from eidolon_agent.domain.context.tests.functional.test_compiler import (
        _StubPersonas,
        _locator,
    )
    from tests.helpers import make_turn_input
    from eidolon_agent.domain.history.manager import HistoryManager

    history = HistoryManager()
    for role, text in ((MessageRole.USER, "earlier-q"), (MessageRole.ASSISTANT, "earlier-a")):
        await history.append(
            conversation_id="c1",
            message=ChatMessage(
                id=uuid.uuid4().hex,
                role=role,
                content=text,
                created_at=datetime.now(UTC),
            ),
        )

    compiler = ContextCompiler(
        personas_service=_StubPersonas("[PERSONA]\nhi"),
        instance_locator=_locator,
        history_manager=history,
        memory_port=None,
    )
    system = (await compiler.compile(make_turn_input("当前问题")))[0].content

    # The block headers in the order the model reads them. Asserted on the
    # structure rather than on where some substring of the persona's text
    # happens to land — that is what a prefix cache keys on.
    blocks = [
        part.split("\n", 1)[0]
        for part in system.split("\n\n")
        if part.startswith("[")
    ]

    assert blocks[-1] == "[CURRENT REQUEST]", blocks
    background = blocks.index("[BACKGROUND CONTEXT]")
    # Everything before history is `stable`; nothing volatile may precede it.
    assert all(b == "[SYSTEM INSTRUCTIONS]" for b in blocks[:background]), blocks
    # And history is not last-but-one by accident: it is ahead of the request,
    # which is where the volatile blocks sort when they are present.
    assert background < len(blocks) - 1, blocks
