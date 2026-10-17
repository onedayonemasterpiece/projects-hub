"""Pair the installed native APK's immutable Start payload with the real API.

The original Android CI verifies NativeSessionApi.startRequest against this exact
fixture through NativeSessionRequestTest. Its source, Git blob, APK receipt and
framework pin are kept beside the copied fixture. This backend-only delivery does
not claim that the unchanged main-branch Android source is the installed candidate.
The full producer/source consistency check remains in native PR #175.
"""

import hashlib
import json
from pathlib import Path

import pytest

from projects_hub.app import LiveStart


@pytest.mark.parametrize("profile", ["normal", "recovery"])
def test_native_start_payload_is_accepted_by_http_request_model(profile):
    fixtures = Path(__file__).resolve().parent / "fixtures"
    pairing = json.loads((fixtures / "native-v2-installed-candidate.json").read_text())
    origin = pairing["start_fixture"]
    raw = (fixtures / origin["file"]).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == origin["sha256"]
    assert hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest() == origin["git_blob_sha"]
    assert origin["source_ref"] == pairing["consumer_source_head"]
    payload = json.loads(raw)[profile]
    accepted = LiveStart.model_validate(payload)
    assert accepted.model_dump(exclude_unset=True) == payload
