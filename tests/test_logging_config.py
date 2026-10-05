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

    payload = json.loads(JsonFormatter().format(record))

    assert payload["kind"] == "turn_complete"
    assert payload["turn_output_audio_events"] == 7
    assert payload["turn_output_audio_bytes"] == 22400
    assert payload["turn_first_output_audio_provider_at"] == 123456789
