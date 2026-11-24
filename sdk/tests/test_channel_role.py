import pytest
from pydantic import ValidationError

from throughline_sdk import Channel


def test_role_fields_are_optional_and_validated():
    plain = Channel(name="fake.chat", description="Invented")
    assert (plain.role, plain.orient_priority, plain.context_window_min) == (None, None, None)
    declared = Channel(
        name="fake.segments", description="Invented", role="transcript", context_window_min=6
    )
    assert declared.model_dump()["role"] == "transcript"
    with pytest.raises(ValidationError):
        Channel(name="fake.chat", description="Invented", role="Not a role")
    with pytest.raises(ValidationError):
        Channel(name="fake.chat", description="Invented", context_window_min=99)
