"""Shared HTTP examples bind the Android request producer to the real API schema.

NativeSessionRequestTest verifies that NativeSessionApi.startRequest produces
these exact payloads. Keep this check with the ordinary consumer Python suite;
it imports the actual FastAPI request model and makes no provider calls.
"""

import json
from pathlib import Path

import pytest

from projects_hub.app import LiveStart


@pytest.mark.parametrize("profile", ["normal", "recovery"])
def test_native_start_payload_is_accepted_by_http_request_model(profile):
    fixture = (
        Path(__file__).resolve().parents[1]
        / "android/app/src/test/resources/native-live-start.json"
    )
    payload = json.loads(fixture.read_text())[profile]
    accepted = LiveStart.model_validate(payload)
    assert accepted.model_dump(exclude_unset=True) == payload
