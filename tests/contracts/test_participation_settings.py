"""A generic Laya endpoint must never be mistaken for participation v2."""

import pytest
from pydantic import ValidationError

from eidolon_agent.config.settings import ParticipationSettings


def test_participation_endpoint_is_explicit_and_versioned():
    assert ParticipationSettings().url == ""
    assert ParticipationSettings(url="http://127.0.0.1:8772/v1/participation/decide").url


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8771/v1/systemone",
    "http://127.0.0.1:8771/readyz",
    "http://127.0.0.1:8771/v1/participation/decide?task=home",
    "http://user:password@127.0.0.1:8771/v1/participation/decide",
])
def test_other_model_routes_cannot_enable_ip_team(url):
    with pytest.raises(ValidationError, match="participation v2 endpoint"):
        ParticipationSettings(url=url)
