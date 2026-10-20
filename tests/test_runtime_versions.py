import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from projects_hub.app import LiveStart
from projects_hub.runtime_versions import loaded_live_sdk


def test_native_build_metadata_is_bounded_display_only():
    build = {"package_id": "com.kenigevents.projectshub", "version_code": 77,
             "source_sha": "b" * 40, "sdk_version": "0.4.0-rc.11", "sdk_sha": "c" * 40}
    payload = LiveStart.model_validate({"client_version": "0.1.62+77", "client_build": build})
    assert payload.client_build.model_dump() == build
    assert LiveStart.model_validate({"client_version": "0.1.62"}).client_build is None
    for bad in ({**build, "version_code": -1}, {**build, "source_sha": "latest"},
                {**build, "package_id": "another.app"}, {**build, "role": "owner"}):
        with pytest.raises(ValidationError):
            LiveStart.model_validate({"client_build": bad})


def test_loaded_sdk_identity_does_not_leak_url_or_secret():
    loaded_live_sdk.cache_clear()
    origin = {"url": "https://secret:credential@host/private", "vcs_info": {"commit_id": "d" * 40}}
    try:
        with patch("projects_hub.runtime_versions.importlib.import_module", return_value=SimpleNamespace(__version__="0.4.0-rc.11")), patch("projects_hub.runtime_versions.importlib.metadata.distribution", return_value=SimpleNamespace(read_text=lambda _: json.dumps(origin))):
            assert loaded_live_sdk() == {"version": "0.4.0-rc.11", "source_sha": "d" * 40}
            assert "credential" not in json.dumps(loaded_live_sdk())
    finally:
        loaded_live_sdk.cache_clear()


def test_native_http_and_ui_share_build_generated_identity():
    root = Path(__file__).resolve().parents[1]
    java = root / "android/app/src/main/java/com/kenigevents/projectshub"
    assert '.put("client_build", NativeBuildInfo.json())' in (java / "NativeSessionApi.java").read_text()
    assert (java / "MainActivity.java").read_text().count("NativeBuildInfo.querySuffix()") == 2
    for field in ("SOURCE_SHA", "LIVE_SDK_VERSION", "LIVE_SDK_SHA"):
        assert f'"{field}"' in (root / "android/app/build.gradle").read_text()
        assert f"BuildConfig.{field}" in (java / "NativeBuildInfo.java").read_text()
