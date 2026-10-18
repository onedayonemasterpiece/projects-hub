#!/usr/bin/env python3
"""Native canary plus a bounded audit of successful client WebSocket writes.

Accepts the existing devcoveer_native_voice_canary.py arguments. Use a fresh
data directory and the same intact Android original. Full-phrase, source,
replay and cleanup gates are unchanged. This is not a recognition ACK.
No caption observer or second ASR lease is enabled. Only the exact primary
provider endpoint is wrapped; the loopback native client remains untouched.
The receipt stores hashes/counts/times, never a URL, key, setup or raw audio.
"""
from __future__ import annotations

import base64
import hashlib
import json
import sys
import time

import devcoveer_native_voice_canary as canary

MAX_ACTIVITIES = 8
MAX_PCM_BYTES = 2 * 90 * 16000 * 2


class ProviderSendAudit:
    def __init__(self):
        self.started = time.monotonic()
        self.connections = 0
        self.activities = []
        self.active = {}
        self.invalid = set()
        self.failed_writes = 0
        self.pcm_bytes = 0

    def elapsed_ms(self):
        return round((time.monotonic() - self.started) * 1000, 3)

    def sent(self, connection, message):
        """Called only after the unchanged inner send has returned successfully."""
        # Provider.run serializes single-key payloads with json.dumps. Ignore
        # setup/tool/history bodies without parsing them or retaining a preview.
        if not isinstance(message, str) or not message.startswith('{"realtimeInput":'):
            return
        try:
            realtime = json.loads(message)["realtimeInput"]
            if not isinstance(realtime, dict):
                raise ValueError
            keys = set(realtime)
            now = self.elapsed_ms()
            if keys == {"activityStart"}:
                if connection in self.active:
                    self.invalid.add("overlapping_activity_start")
                    return
                if len(self.activities) >= MAX_ACTIVITIES:
                    self.invalid.add("activity_bound")
                    return
                item = {"provider_connection_index": connection,
                        "activity_index": len(self.activities) + 1,
                        "activity_start_send_completed_ms": now,
                        "activity_end_send_completed_ms": None,
                        "first_audio_send_completed_ms": None,
                        "last_audio_send_completed_ms": None,
                        "packets": 0, "samples": 0, "pcm_bytes": 0,
                        "_pcm": hashlib.sha256(), "_packets": hashlib.sha256()}
                self.activities.append(item)
                self.active[connection] = item
            elif keys == {"audio"}:
                item = self.active.get(connection)
                if item is None:
                    self.invalid.add("audio_without_activity_start")
                    return
                audio = realtime["audio"]
                raw = audio.get("data")
                if (audio.get("mimeType") != "audio/pcm;rate=16000" or not isinstance(raw, str)
                        or len(raw) > 90000):
                    raise ValueError
                pcm = base64.b64decode(raw, validate=True)
                if not pcm or len(pcm) % 2:
                    raise ValueError
                if self.pcm_bytes + len(pcm) > MAX_PCM_BYTES:
                    self.invalid.add("pcm_bound")
                    return
                self.pcm_bytes += len(pcm)
                item["_pcm"].update(pcm)
                # Same ordered chain convention as encoded_voice_projections:
                # SHA256 of concatenated lowercase per-packet SHA256 hex values.
                item["_packets"].update(hashlib.sha256(pcm).hexdigest().encode("ascii"))
                item["packets"] += 1
                item["samples"] += len(pcm) // 2
                item["pcm_bytes"] += len(pcm)
                if item["first_audio_send_completed_ms"] is None:
                    item["first_audio_send_completed_ms"] = now
                item["last_audio_send_completed_ms"] = now
            elif keys == {"activityEnd"}:
                item = self.active.pop(connection, None)
                if item is None:
                    self.invalid.add("activity_end_without_start")
                    return
                item["activity_end_send_completed_ms"] = now
                if not item["packets"]:
                    self.invalid.add("empty_activity")
            else:
                self.invalid.add("unexpected_realtime_input_shape")
        except Exception:
            # Diagnostics never rewrite or block a primary provider operation.
            self.invalid.add("unreadable_realtime_input")

    def result(self):
        reasons = sorted(self.invalid | ({"unfinished_activity"} if self.active else set()))
        if not self.activities:
            reasons.append("no_activities_observed")
        return {"schema": "projects-hub-primary-provider-send-audit", "version": 1,
                "claim": "Successful client WebSocket writes; not provider recognition acknowledgements",
                "valid": not reasons, "invalid_reasons": reasons,
                "provider_connection_count": self.connections,
                "failed_write_count": self.failed_writes,
                "captured_at_elapsed_ms": self.elapsed_ms(),
                "activities": [{**{k: v for k, v in item.items() if not k.startswith("_")},
                    "pcm_sha256": item["_pcm"].hexdigest(),
                    "pcm_chunk_hashes_sha256": item["_packets"].hexdigest()}
                    for item in self.activities]}

    def connect_factory(self, original, endpoint):
        audit = self

        class Socket:
            def __init__(self, inner, connection):
                self.inner, self.connection = inner, connection

            def __getattr__(self, name):
                return getattr(self.inner, name)

            async def send(self, message, *args, **kwargs):
                try:
                    result = await self.inner.send(message, *args, **kwargs)
                except BaseException:
                    audit.failed_writes += 1
                    audit.invalid.add("write_not_completed")
                    raise
                audit.sent(self.connection, message)
                return result

        class Connection:
            def __init__(self, inner):
                self.inner = inner

            async def __aenter__(self):
                inner = await self.inner.__aenter__()
                audit.connections += 1
                return Socket(inner, audit.connections)

            async def __aexit__(self, *args):
                return await self.inner.__aexit__(*args)

        def connect(*args, **kwargs):
            inner = original(*args, **kwargs)
            uri = args[0] if args else kwargs.get("uri")
            return (Connection(inner) if isinstance(uri, str) and
                    (uri == endpoint or uri.startswith(endpoint + "?")) else inner)

        return connect


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if "--qualify-captions" in arguments:
        raise SystemExit("Primary send probe does not enable a caption observer or a second ASR lease")
    original_run = canary.run_provider

    async def audited_run(args, recording, report):
        # The native parser also accepts flag abbreviations. Enforce the parsed
        # value before original_run can acquire any primary or caption lease.
        if args.qualify_captions:
            raise ValueError("CANARY_PRIMARY_SEND_PROBE_CAPTIONS_UNSUPPORTED")
        import websockets
        from live_interaction.provider import ENDPOINT
        audit = ProviderSendAudit()
        original_connect = websockets.connect
        report["provider_send_audit"] = audit.result()
        websockets.connect = audit.connect_factory(original_connect, ENDPOINT)
        try:
            return await original_run(args, recording, report)
        finally:
            websockets.connect = original_connect
            report["provider_send_audit"] = audit.result()

    canary.run_provider = audited_run
    try:
        return canary.main(arguments)
    finally:
        canary.run_provider = original_run


if __name__ == "__main__":
    raise SystemExit(main())
