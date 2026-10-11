import json
import logging

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
