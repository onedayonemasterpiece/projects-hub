import json
import logging
from pathlib import Path
import sys

from projects_hub.logging_config import (
    JsonFormatter, VOICE_PERFORMANCE_FIELDS, voice_performance_scalars,
)


def test_voice_archive_performance_metrics_survive_both_safe_logging_filters():
    # Previously the SDK emitted actual archive/SQLite timings, but the
    # second JSON formatter filter lost them while retaining duration_ms
    # (which is PCM span, NOT time spent committing).
    observed = {
        "event_seq": 71, "queue_items": 12, "queue_bytes": 14000,
        "queue_duration_us": 850000, "admit_wait_ms": 126,
        "archive_wait_ms": 175, "process_ms": 244.5,
        "encode_ms": 27, "commit_ms": 209,
    }
    assert set(observed) == VOICE_PERFORMANCE_FIELDS
    record = logging.LogRecord(
        "projects_hub.live", logging.INFO, __file__, 1,
        "live socket diagnostic", (), None,
    )
    record.event = "archive_event_committed"
    record.session_id = "live_fixture"
    record.duration_ms = 320
    record.private_transcript = "PRIVATE NEVER LOG"
    record.private_pcm = "PRIVATE ORIGINAL"
    for key, value in voice_performance_scalars(observed).items():
        setattr(record, key, value)
    payload = json.loads(JsonFormatter().format(record))
    assert all(payload[name] == value for name, value in observed.items())
    assert payload["duration_ms"] == 320
    assert payload["session_id"] == "live_fixture"
    assert "PRIVATE" not in json.dumps(payload)

    rejected = {
        "queue_bytes": True, "commit_ms": "secret-token",
        "archive_wait_ms": float("nan"), "process_ms": -1,
        "event_seq": 10**100, "encode_ms": float("inf"),
        "user_text": "PRIVATE",
    }
    assert voice_performance_scalars(rejected) == {}
    # Pin the common admission-to-output function instead of maintaining
    # divergent manually duplicated safelists in two product layers.
    runtime = (Path(__file__).resolve().parents[1] / "src/projects_hub/live_runtime.py").read_text()
    assert "safe.update(voice_performance_scalars(record))" in runtime


def test_json_formatter_bounds_new_transition_resource_fields_to_safe_scalars():
    record = logging.LogRecord("projects_hub.live", logging.INFO, __file__, 1,
                               "live provider event", (), None)
    record.transition_id = "cap_fixture"
    record.from_capability = "core"
    record.to_capability = "collaboration"
    record.function_count = 9
    record.schema_bytes = 5000
    record.retry = 2
    record.wait_ms = 3000.0
    record.input_type = {"prompt": "PRIVATE nested content"}
    record.resumption_mode = "PRIVATE" * 100
    record.retries = True
    record.configuration_bytes = 10**400
    record.headers = {"Authorization": "PRIVATE token"}
    encoded = JsonFormatter().format(record)
    payload = json.loads(encoded)
    assert payload["transition_id"] == "cap_fixture"
    assert payload["from_capability"] == "core"
    assert payload["to_capability"] == "collaboration"
    assert payload["function_count"] == 9
    assert payload["schema_bytes"] == 5000
    assert payload["retry"] == 2
    assert payload["wait_ms"] == 3000.0
    assert not ({"input_type", "resumption_mode", "retries", "configuration_bytes", "headers"} & payload.keys())
    assert "PRIVATE" not in encoded


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
