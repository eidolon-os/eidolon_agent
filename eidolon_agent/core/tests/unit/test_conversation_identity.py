import pytest

from eidolon_agent.core.types.conversation import (
    CONVERSATION_ID_MAX_LENGTH,
    validate_conversation_id,
)


def test_a_transport_opaque_identity_is_valid() -> None:
    conversation_id = "livekit:" + "participant-identity-" * 5

    assert len(conversation_id) > 64
    assert validate_conversation_id(conversation_id) == conversation_id


@pytest.mark.parametrize("conversation_id", ["", "   "])
def test_an_empty_identity_is_not_a_conversation(conversation_id: str) -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        validate_conversation_id(conversation_id)


def test_an_unbounded_transport_value_is_rejected_before_persistence() -> None:
    with pytest.raises(ValueError, match="exceeds"):
        validate_conversation_id("c" * (CONVERSATION_ID_MAX_LENGTH + 1))
