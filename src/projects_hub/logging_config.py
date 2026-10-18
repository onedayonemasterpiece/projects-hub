from __future__ import annotations

from collections import deque
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import sys
import time
from types import TracebackType
from typing import Any


def live_diagnostic_scalars(values: dict[str, Any]) -> dict[str, Any]:
    """Only bounded protocol identifiers and numeric transition/budget evidence."""
    result: dict[str, Any] = {}
    for key in ("transition_id", "from_capability", "to_capability", "input_type",
                "resumption_mode", "configuration_digest"):
        value = values.get(key)
        if isinstance(value, str) and len(value) <= 160:
            result[key] = value
    for key in ("retry", "retries", "wait_ms", "function_count", "schema_bytes",
                "configuration_bytes"):
        value = values.get(key)
        if type(value) in (int, float) and 0 <= value < 2**53 and math.isfinite(value):
            result[key] = value
    return result


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": round(time.time() * 1000),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in (
            "event",
            "event_id",
            "subscription_id",
            "project_id",
            "grant_id",
            "binding_id",
            "binding_revision",
            "response_id",
            "result_kind",
            "slack_state",
            "mira_state",
            "rpc_method",
            "mcp_protocol",
            "catalog_names",
            "catalog_count",
            "catalog_sha256",
            "delivery_attempt",
            "request_id",
            "command_id",
            "device_id",
            "capability",
            "retry_after_ms",
            "method",
            "path",
            "status",
            "duration_ms",
            "conversation_id",
            "source_id",
            "session_id",
            "tool",
            "result",
            "code",
            "kind",
            "event_count",
            "text_length",
            "provider_at",
            "attempt_id",
            "client_version",
            "backend_version",
            "backend_release_sha",
            "reason",
            "modality",
            "estimated_units",
            "requested_units",
            "granted_units",
            "connection_generation",
            "frame_seq",
            "pcm_bytes",
            "capture_age_ms",
            "latency_stage",
            "latency_alert",
            "turn_output_audio_events",
            "turn_output_audio_bytes",
            "turn_first_output_audio_provider_at",
            "audio_chunks",
            "max_stdin_delay_ms",
            "max_ws_send_ms",
            "audio_stream_end_sent_at",
            "activity_end_sent_at",
            "audio_turn_open",
            "exception_type",
        ):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        payload.update(live_diagnostic_scalars(vars(record)))
        if record.exc_info and record.exc_info[0] is not None:
            exception_type, _exception, traceback = record.exc_info
            payload.setdefault("exception_type", exception_type.__name__[:80])
            # Keep only source coordinates: exception text, chained exceptions,
            # source lines and frame locals can contain credentials or user data.
            # Reading code metadata also avoids loading source files while logging.
            frames: deque[dict[str, Any]] = deque(maxlen=8)
            while isinstance(traceback, TracebackType):
                code = traceback.tb_frame.f_code
                frames.append({
                    "file": Path(code.co_filename).name[:120],
                    "function": code.co_name[:80],
                    "line": traceback.tb_lineno,
                })
                traceback = traceback.tb_next
            if frames:
                payload["exception_frames"] = list(frames)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_logging(level: int = logging.INFO) -> None:
    formatter = JsonFormatter()
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    log_file = str(os.environ.get("PROJECTS_HUB_LOG_FILE") or "").strip()
    if log_file:
        path = Path(log_file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass
        handlers.append(
            RotatingFileHandler(
                path,
                maxBytes=10 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            )
        )
    for handler in handlers:
        handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers[:] = handlers
    root.setLevel(level)
