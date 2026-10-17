import json
import logging
from pathlib import Path
import sys

from projects_hub.logging_config import JsonFormatter


def test_json_formatter_keeps_voice_output_audio_turn_telemetry():
    record = logging.LogRecord(
        name="projects_hub.live",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="live provider event",
        args=(),
        exc_info=None,
    )
    record.event = "live_provider_event"
    record.kind = "turn_complete"
    record.turn_output_audio_events = 7
    record.turn_output_audio_bytes = 22400
    record.turn_first_output_audio_provider_at = 123456789
    record.audio_chunks = 42
    record.max_stdin_delay_ms = 17
    record.max_ws_send_ms = 9
    record.audio_stream_end_sent_at = 123456999

    payload = json.loads(JsonFormatter().format(record))

    assert payload["kind"] == "turn_complete"
    assert payload["turn_output_audio_events"] == 7
    assert payload["turn_output_audio_bytes"] == 22400
    assert payload["turn_first_output_audio_provider_at"] == 123456789
    assert payload["audio_chunks"] == 42
    assert payload["max_stdin_delay_ms"] == 17
    assert payload["max_ws_send_ms"] == 9
    assert payload["audio_stream_end_sent_at"] == 123456999



def test_device_receipt_diagnostics_keep_ids_not_private_payloads():
    record = logging.LogRecord("projects_hub.api", logging.WARNING, __file__, 1,
                               "device command receipt rejected", (), None)
    record.command_id = "cmd_test"
    record.device_id = "dev_test"
    record.capability = "calendar.create_event"
    record.retry_after_ms = 3000
    record.code = "INVALID_ARGUMENT"
    record.result_payload = {"title": "private event", "claim_token": "secret"}
    payload = json.loads(JsonFormatter().format(record))
    assert payload["command_id"] == "cmd_test"
    assert payload["retry_after_ms"] == 3000
    assert "result_payload" not in payload
    assert "private event" not in json.dumps(payload)


def test_json_formatter_keeps_exception_coordinates_without_private_values():
    private = "private-token-DO-NOT-LOG"
    namespace = {"private": private}
    exec(compile(
        "def provider_failure():\n"
        "    headers = {'Authorization': private}\n"
        "    raise TimeoutError(private) from ValueError(private)\n",
        f"/private/{private}/provider.py", "exec",
    ), namespace)
    try:
        namespace["provider_failure"]()
    except TimeoutError:
        record = logging.LogRecord(
            "asyncio", logging.ERROR, __file__, 1,
            "Fatal error on SSL protocol", (), sys.exc_info(),
        )
    # A previous handler may already have formatted the complete traceback.
    record.exc_text = private
    record.stack_info = private
    record.headers = {"Authorization": private}

    encoded = JsonFormatter().format(record)
    payload = json.loads(encoded)

    assert payload["exception_type"] == "TimeoutError"
    assert payload["exception_frames"][-1] == {
        "file": "provider.py", "function": "provider_failure", "line": 3,
    }
    assert all(set(frame) == {"file", "function", "line"}
               for frame in payload["exception_frames"])
    assert private not in encoded
    assert str(Path(__file__).parent) not in encoded
    assert "ValueError" not in encoded
    assert "headers" not in encoded


def test_json_formatter_bounds_exception_frames_and_keeps_failure_site():
    def descend(depth):
        if depth:
            descend(depth - 1)
        else:
            raise RuntimeError("private exception text")

    try:
        descend(20)
    except RuntimeError:
        record = logging.LogRecord(
            "asyncio", logging.ERROR, __file__, 1,
            "transport failed", (), sys.exc_info(),
        )

    payload = json.loads(JsonFormatter().format(record))

    assert payload["exception_type"] == "RuntimeError"
    assert len(payload["exception_frames"]) == 8
    assert payload["exception_frames"][-1]["function"] == "descend"
    assert payload["exception_frames"][-1]["line"] > 0
    assert "private exception text" not in json.dumps(payload)


def test_json_formatter_preserves_explicit_exception_and_voice_fields():
    try:
        raise TimeoutError("private provider endpoint")
    except TimeoutError:
        record = logging.LogRecord(
            "projects_hub.api", logging.WARNING, __file__, 1,
            "live start failed", (), sys.exc_info(),
        )
    record.exception_type = "LiveError"
    record.code = "LIVE_PROVIDER_TIMEOUT"
    record.attempt_id = "attempt-1"
    record.duration_ms = 15000
    record.retry_after_ms = 3000

    payload = json.loads(JsonFormatter().format(record))

    assert payload["exception_type"] == "LiveError"
    assert payload["code"] == "LIVE_PROVIDER_TIMEOUT"
    assert payload["attempt_id"] == "attempt-1"
    assert payload["duration_ms"] == 15000
    assert payload["retry_after_ms"] == 3000
    assert payload["exception_frames"]
    assert "private provider endpoint" not in json.dumps(payload)
