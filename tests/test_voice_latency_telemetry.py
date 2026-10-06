import json
import logging
from pathlib import Path

from projects_hub.logging_config import JsonFormatter


ROOT = Path(__file__).resolve().parents[1]


def test_voice_latency_hops_have_explicit_thresholded_telemetry():
    runtime = (ROOT / "src/projects_hub/live_runtime.py").read_text(encoding="utf-8")
    adapter = (ROOT / "src/projects_hub/live_adapter.py").read_text(encoding="utf-8")

    assert '"latency_stage"] = "capture_to_server"' in runtime
    assert "capture_age >= 1000" in runtime
    assert "duration >= 250" in runtime
    assert "log.warning if latency_alert else log.info" in runtime

    assert '"latency_stage"] = "server_to_provider"' in adapter
    assert "stdin_delay >= 1000" in adapter
    assert "ws_send >= 500" in adapter
    assert "log.warning if latency_alert else log.info" in adapter


def test_json_logging_keeps_voice_latency_classification():
    record = logging.LogRecord(
        name="projects_hub.live",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="live voice latency alert",
        args=(),
        exc_info=None,
    )
    record.event = "socket_audio_accepted"
    record.capture_age_ms = 4831
    record.latency_stage = "capture_to_server"
    record.latency_alert = True

    payload = json.loads(JsonFormatter().format(record))

    assert payload["capture_age_ms"] == 4831
    assert payload["latency_stage"] == "capture_to_server"
    assert payload["latency_alert"] is True
