"""Long-task key helpers."""

from __future__ import annotations

from datetime import date

from eidolon_agent.core.types.long_task import (
    owner_id_from_safe_key,
    parse_session_key,
    safe_owner_key,
    session_key_for,
    task_key_for,
)


def test_session_key_is_short_daily_and_key_safe() -> None:
    assert session_key_for("manson", date(2026, 6, 14)) == "e.manson.20260614"
    assert session_key_for("user-01", "2026-06-14") == "e.user-01.20260614"


def test_non_key_safe_owner_id_is_reversible_base64url_segment() -> None:
    segment = safe_owner_key("用户/01")

    assert segment.startswith("b64_")
    assert "/" not in segment
    assert "." not in segment
    assert session_key_for("用户/01", date(2026, 6, 14)) == f"e.{segment}.20260614"
    assert owner_id_from_safe_key(segment) == "用户/01"


def test_b64_prefix_is_reserved_to_keep_owner_segments_unambiguous() -> None:
    assert safe_owner_key("b64_alice").startswith("b64_")
    assert safe_owner_key("b64_alice") != "b64_alice"
    assert owner_id_from_safe_key(safe_owner_key("b64_alice")) == "b64_alice"


def test_session_key_can_be_parsed_back_to_user_and_date() -> None:
    session_key = session_key_for("用户/01", date(2026, 6, 14))

    assert parse_session_key(session_key) == ("用户/01", "20260614")


def test_task_key_embeds_daily_session_and_short_task_prefix() -> None:
    session_key = "e.manson.20260614"

    assert task_key_for(session_key, "abcdef1234567890") == "e.manson.20260614.abcdef123456"
