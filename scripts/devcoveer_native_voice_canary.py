#!/usr/bin/env python3
"""Replay Android CI's original compressed audio through the real server boundary.

Run inspection first with --fixture-evidence DIR. A provider run additionally
requires --run-provider, one explicit input source ID and a NEW --data-dir.
The evidence directory contains audio-verification.json and fixture-data's
SQLite database. --voice-flow selects the exact UTP voice-e2e/voice-flow.json
from the same Android job artifact; it normally lies outside smoke-evidence.

Use the candidate release's Python and its existing on-host resource authority
environment. No credentials are accepted on this command line or sent to CI.
The local server uses create_app/build_live_host and ordinary loopback dev-login;
it owns a fresh actor/workspace/store and has no access to production user data.
No provider implementation, codec, transcript mapper or product tool is replaced.

This is a provider integration gate using native-captured input. Android device
capture/playback remains a separate gate. Receiving a durable output here is not
an AudioTrack playback claim. Caption offsets are observed, never enabled as a
verified production clock by this script. Normal runs preserve the production
unverified caption mode without starting a second ASR provider. --qualify-captions
explicitly enables the diagnostic observer and additionally requires the strict
caption qualification; primary_passed remains an independent result.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter, deque
from contextlib import closing
from dataclasses import dataclass
import hashlib
import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import secrets
import socket
import sqlite3
import struct
import time
from typing import Any

from devcoveer_wss_canary import HttpSession, login_and_conversation, start_live
from devcoveer_canary_support import (bounded, cleanup_step, record_error, run_reported,
                                    save_report, use_installed_provider_environment)

MAX_ENCODED_BYTES = 2 * 1024 * 1024
MAX_CAPTURE_US = 90_000_000
MAX_CHUNKS = 5000
SAMPLE_NS = 62_500  # The caption provider's PCM clock is 16 kHz.
DEFAULT_PHRASES = (
    "Мира, проверка голосового разговора, слышишь меня?",
    "Эта запись сделана без соединения и должна сохраниться",
    "Мира, второй вопрос после восстановления связи",
)


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def implementation_fingerprints() -> dict[str, str]:
    """Pin what executed even when several candidates share a prerelease version."""
    names = ("projects_hub.live_runtime", "projects_hub.live_transcription",
             "live_interaction.provider", "live_interaction.transcribe", "live_interaction.caption_provenance",
             "ai_resource_control.client", "ai_resource_control.live")
    return {name: digest(Path(importlib.import_module(name).__file__).read_bytes()) for name in names}


def normalized(text: str) -> str:
    return " ".join(re.findall(r"[\w]+", text.casefold().replace("ё", "е")))


def duration_ns(value: Any) -> int | None:
    """Parse exact protobuf Duration nanoseconds; never interpret a plain number."""
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"(\d{1,8})(?:\.(\d{1,9}))?s", value)
    if not match:
        return None
    return int(match[1]) * 1_000_000_000 + int((match[2] or "").ljust(9, "0"))


def read_json(path: Path) -> dict:
    if path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("CANARY_EVIDENCE_TOO_LARGE")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("CANARY_EVIDENCE_SHAPE")
    return value


def fixture_receipts(evidence: Path, voice_flow: Path, conversation_id: str | None = None) -> dict:
    paths = {"audio-verification.json": evidence / "audio-verification.json", "voice-flow.json": voice_flow}
    values = {name: read_json(path) for name, path in paths.items()}
    if any(value.get("passed") is not True for value in values.values()):
        raise ValueError("CANARY_ANDROID_FIXTURE_NOT_PASSED")
    if conversation_id is not None and values["voice-flow.json"].get("conversation_id") != conversation_id:
        raise ValueError("CANARY_RECEIPT_SOURCE_CONVERSATION_MISMATCH")
    return {name: {"path": str(path.resolve()), "sha256": digest(path.read_bytes()), "passed": True}
            for name, path in paths.items()}


def open_original(path: Path):
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def inventory(path: Path) -> list[dict]:
    with closing(open_original(path)) as db:
        return [dict(row) for row in db.execute(
            "SELECT s.id AS source_id,s.client_source_id,s.conversation_id,e.complete,e.sealed_seq,"
            "e.capture_end_us,COUNT(c.chunk_seq) AS chunks,SUM(LENGTH(c.payload)) AS encoded_bytes "
            "FROM sources s JOIN encoded_voice_sources e ON e.source_id=s.id "
            "LEFT JOIN encoded_voice_chunks c ON c.source_id=s.id "
            "WHERE s.audio_direction='uplink' GROUP BY s.id ORDER BY e.started_at_unix_ms,s.id LIMIT 32"
        )]


@dataclass(frozen=True)
class Recording:
    source_id: str
    conversation_id: str
    started_at_unix_ms: int
    capture_end_us: int
    epochs: dict[str, dict]
    chunks: tuple[Any, ...]

    def portions(self) -> list[dict]:
        from live_interaction.audio_v2 import PORTION_START, PORTION_END
        portions = []
        start = None
        for chunk in self.chunks:
            if chunk.flags & PORTION_START:
                start = chunk.capture_start_us
            if chunk.flags & PORTION_END:
                portions.append({"capture_start_us": start,
                                 "capture_end_us": chunk.capture_start_us + chunk.duration_us})
                start = None
        return portions

    def receipt(self) -> dict:
        return {"original_source_id": self.source_id,
                "original_conversation_id": self.conversation_id,
                "capture_start_us": self.chunks[0].capture_start_us,
                "capture_end_us": self.capture_end_us,
                "chunks": len(self.chunks),
                "speech_portions": self.portions(),
                "encoded_bytes": sum(len(c.payload) for c in self.chunks),
                "payloads_sha256": digest(b"".join(c.payload for c in self.chunks)),
                "metadata_sha256": digest(json.dumps({"epochs": self.epochs, "chunks": [
                    [c.epoch_id, c.chunk_seq, c.capture_start_us, c.duration_us, c.flags, c.payload_sha256]
                    for c in self.chunks]}, sort_keys=True).encode())}


def load_recording(path: Path, source_id: str) -> Recording:
    from live_interaction.audio_v2 import CodecEpoch, EncodedChunk, EPOCH_END, PORTION_START, PORTION_END
    with closing(open_original(path)) as db:
        source = db.execute(
            "SELECT s.id,s.conversation_id,e.* FROM sources s JOIN encoded_voice_sources e ON e.source_id=s.id "
            "WHERE s.audio_direction='uplink' AND (s.id=? OR s.client_source_id=?)",
            (source_id, source_id),
        ).fetchall()
        if len(source) != 1 or source[0]["complete"] != 1:
            raise ValueError("CANARY_NEEDS_ONE_COMPLETE_INPUT_SOURCE")
        source = source[0]
        epochs = {}
        for row in db.execute("SELECT * FROM encoded_voice_epochs WHERE source_id=?", (source["id"],)):
            raw = row["descriptor_json"]
            if digest(raw.encode()) != row["descriptor_sha256"]:
                raise ValueError("CANARY_ORIGINAL_EPOCH_DIGEST")
            epoch = CodecEpoch.from_dict(json.loads(raw))
            if epoch.codec != "opus" or epoch.container != "ogg" or epoch.decoded_sample_rate != 16000:
                raise ValueError("CANARY_NEEDS_NATIVE_OGG_OPUS")
            epochs[epoch.epoch_id] = epoch.as_dict()
        chunks, size, previous_end, active_epoch, portion_open = [], 0, None, None, False
        for row in db.execute("SELECT * FROM encoded_voice_chunks WHERE source_id=? ORDER BY chunk_seq",
                              (source["id"],)):
            chunk = EncodedChunk(source["id"], row["epoch_id"], row["chunk_seq"], row["capture_start_us"],
                                 row["duration_us"], row["flags"], bytes(row["payload"]))
            size += len(chunk.payload)
            if size > MAX_ENCODED_BYTES or len(chunks) >= MAX_CHUNKS:
                raise ValueError("CANARY_INPUT_BOUND")
            if chunk.chunk_seq != len(chunks) + 1 or chunk.payload_sha256 != row["payload_sha256"]:
                raise ValueError("CANARY_ORIGINAL_CHUNK_DIGEST_OR_SEQUENCE")
            if chunk.epoch_id not in epochs or (previous_end is not None and
                    (chunk.capture_start_us < previous_end or (active_epoch is not None and chunk.capture_start_us != previous_end))):
                raise ValueError("CANARY_ORIGINAL_CAPTURE_CLOCK")
            if active_epoch is not None and active_epoch != chunk.epoch_id:
                raise ValueError("CANARY_ORIGINAL_EPOCH_WITHOUT_END")
            if chunk.flags & PORTION_START:
                if portion_open:
                    raise ValueError("CANARY_ORIGINAL_PORTIONS_OVERLAP")
                portion_open = True
            if chunk.flags & PORTION_END:
                if not portion_open:
                    raise ValueError("CANARY_ORIGINAL_PORTION_WITHOUT_START")
                portion_open = False
            active_epoch = None if chunk.flags & EPOCH_END else chunk.epoch_id
            previous_end = chunk.capture_start_us + chunk.duration_us
            chunks.append(chunk)
        if (not chunks or portion_open or active_epoch is not None
                or source["sealed_seq"] != len(chunks) or source["capture_end_us"] != previous_end
                or previous_end - chunks[0].capture_start_us > MAX_CAPTURE_US):
            raise ValueError("CANARY_ORIGINAL_INCOMPLETE_OR_TOO_LONG")
        return Recording(source["id"], source["conversation_id"], source["started_at_unix_ms"], previous_end, epochs, tuple(chunks))


class CaptionEvidence:
    """A bounded test observer. Raw transcript text never enters its records."""
    def __init__(self, phrases: tuple[str, ...]):
        self.phrases = tuple(normalized(p) for p in phrases)
        self.sources: dict[str, dict] = {}
        self.sent: list[dict] = []
        self.probes: list[dict] = []
        self.observer_error: str | None = None

    def observe(self, event: dict, transcript_text: str | None = None) -> None:
        kind = event.get("type")
        if kind not in {"caption_audio_sent", "caption_timing_probe"}:
            return
        target = self.sent if kind == "caption_audio_sent" else self.probes
        if len(target) >= 10000:
            # The optional sidecar deliberately swallows observer exceptions.
            # Keep a sticky failed-evidence state instead of relying on a raise.
            self.observer_error = "CANARY_PROBE_BOUND"
            return
        clean = {"type": kind}
        for key in ("provider_connection_generation", "stream_start_sample", "stream_end_sample",
                    "capture_start_us", "capture_end_us", "sent_pcm_samples", "text_length"):
            value = event.get(key)
            if type(value) is int and 0 <= value < 2**53:
                clean[key] = value
        for key in ("start_offset", "end_offset", "transcript_kind", "offset_clock"):
            value = event.get(key)
            if isinstance(value, str) and len(value) <= 80:
                clean[key] = value
        for key in ("assigned", "observed_finished"):
            if type(event.get(key)) is bool:
                clean[key] = event[key]
        scope = event.get("caption_scope")
        if isinstance(scope, dict):
            clean["caption_scope"] = {key: value for key, value in scope.items()
                if key in {"session_id", "conversation_id", "source_id", "utterance_id", "turn_id", "utterance_sequence"}
                and ((isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", value))
                     or (type(value) is int and 0 <= value < 2**53))}
        if isinstance(transcript_text, str):
            text = normalized(transcript_text)
            clean["text_length"] = len(transcript_text)
            clean["normalized_text_sha256"] = digest(text.encode())
            clean["known_phrase_matches"] = [i for i, phrase in enumerate(self.phrases) if phrase in text]
        target.append(clean)

    def result(self, *, expected_replays: int = 2) -> dict:
        mapped, observed = [], set()
        for probe in self.probes:
            start, end = duration_ns(probe.get("start_offset")), duration_ns(probe.get("end_offset"))
            match = probe.get("known_phrase_matches", [])
            if start is None or end is None or end <= start or len(match) != 1:
                continue
            # This is a hypothesis check against every sent span, not assignment
            # by callback arrival time, latest source, text length or placeholder.
            # Cover the exact interval: microsecond truncation loses the half-
            # microsecond boundary of odd samples; flooring end can hide a crossing.
            left, right = start // SAMPLE_NS, (end + SAMPLE_NS - 1) // SAMPLE_NS
            spans = sorted((s for s in self.sent
                if s.get("provider_connection_generation") == probe.get("provider_connection_generation")
                and s.get("stream_start_sample", right) < right and s.get("stream_end_sample", left) > left),
                key=lambda s: s["stream_start_sample"])
            cursor, source_ids = left, set()
            for span in spans:
                if span["stream_start_sample"] > cursor:
                    break
                cursor = max(cursor, span["stream_end_sample"])
                source_ids.add((span.get("caption_scope") or {}).get("source_id"))
            if cursor < right or len(source_ids) != 1:
                continue
            source = self.sources.get(next(iter(source_ids)))
            if not source:
                continue
            portion_index = source["expected_portions"][match[0]]
            portion = source["portions"][portion_index]
            # A known phrase is tied to its declared ORIGINAL capture portion,
            # not whichever utterance happens to be current when the callback ran.
            if any(s.get("capture_start_us", -1) < portion["capture_start_us"]
                   or s.get("capture_end_us", 2**53) > portion["capture_end_us"] for s in spans):
                continue
            observed.add((source["replay_id"], match[0]))
            mapped.append({"replay_id": source["replay_id"], "known_phrase_index": match[0], "original_portion_index": portion_index,
                           "provider_connection_generation": probe.get("provider_connection_generation"),
                           "start_offset": probe["start_offset"], "end_offset": probe["end_offset"]})
        expected = {(source["replay_id"], i) for source in self.sources.values() for i in range(len(self.phrases))}
        return {"candidate_clock": "provider_connection_pcm_samples", "offset_clock": "unverified",
                "all_replays_support_candidate_clock": not self.observer_error and len(self.sources) == expected_replays and len(self.phrases) >= 2 and observed == expected,
                "matched_replays": sorted(observed), "mapped_probes": mapped,
                "observer_error": self.observer_error,
                "audio_sent": self.sent, "timing_probes": self.probes,
                "production_clock_configuration_changed": False}


def configure_caption_qualification(sidecar, evidence: CaptionEvidence, *, requested: bool) -> dict:
    result = {"requested": requested, "observer_enabled": False,
              "status": "unverified" if requested else "not_requested"}
    if requested:
        if sidecar is None or not hasattr(sidecar, "diagnostic_observer"):
            result.update(status="failed", code="CANARY_TIMESTAMP_OBSERVER_UNAVAILABLE")
        else:
            sidecar.diagnostic_observer = evidence.observe
            result["observer_enabled"] = True
    return result


def update_acceptance(report: dict, *, loop_shutdown_pending: bool = False) -> None:
    """Keep caption qualification independent; every lifecycle failure is fatal."""
    primary = (report.get("primary_checks_passed_before_cleanup") is True
               and not report.get("errors")
               and report.get("host_sessions_after_close") == 0
               and report.get("caption_sidecars_after_close") == 0)
    # run_reported still owns task/executor shutdown after run_provider returns.
    report["primary_passed"] = primary and not loop_shutdown_pending
    qualification = report.get("caption_qualification") or {}
    report["passed"] = report["primary_passed"] and (qualification.get("requested") is False
                                                     or qualification.get("status") == "passed")


class ReplaySocket:
    def __init__(self, ws, output_file, caption: CaptionEvidence, *, connection_generation: int):
        if type(connection_generation) is not int or connection_generation < 1:
            raise ValueError("CANARY_OUTPUT_GENERATION")
        self.ws, self.output_file, self.caption = ws, output_file, caption
        self.connection_generation = connection_generation
        self.changed = asyncio.Event()
        self.controls = deque(maxlen=256)
        self.events = deque(maxlen=2048)
        self.acks, self.output_sources, self.output_ended = {}, {}, set()
        self.output_interrupts, self.output_interrupt_rejections = {}, Counter()
        self.output_interrupt_duplicates = 0
        self.cursor = 0
        self.error: BaseException | None = None
        self.reader = asyncio.create_task(self.receive())
        self.heartbeat = asyncio.create_task(self.keep_alive())

    async def send(self, value):
        await self.ws.send(json.dumps(value, separators=(",", ":")))

    async def keep_alive(self):
        try:
            while True:
                await asyncio.sleep(5)
                await self.send({"type": "ping"})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.error = exc
            self.changed.set()

    def persist(self, kind: bytes, payload: bytes):
        self.output_file.write(kind + struct.pack("!I", len(payload)) + payload)
        self.output_file.flush()
        os.fsync(self.output_file.fileno())

    async def receive(self):
        from live_interaction.audio_v2 import AudioFrame, DOWNLINK_MAGIC, EPOCH_END
        try:
            async for raw in self.ws:
                if isinstance(raw, bytes):
                    frame = AudioFrame.decode(raw)
                    if frame.magic != DOWNLINK_MAGIC:
                        raise ValueError("CANARY_UNEXPECTED_BINARY")
                    binding = self.output_sources[frame.stream_ref]
                    if (frame.delivery_generation != self.connection_generation
                            or frame.epoch_ref != binding["epoch_ref"]):
                        raise ValueError("CANARY_OUTPUT_FRAME_IDENTITY")
                    source_id = binding["source_id"]
                    await asyncio.to_thread(self.persist, b"B", raw)
                    await self.send({"type": "output_ack", "stage": "device_durable", "source_id": source_id,
                                     "chunk_seq": frame.chunk_seq})
                    binding["last_event_seq"] = max(binding["last_event_seq"], frame.event_seq)
                    if frame.flags & EPOCH_END:
                        self.output_ended.add(source_id)
                    self.cursor = max(self.cursor, frame.event_seq)
                else:
                    value = json.loads(raw)
                    kind = value.get("type")
                    if kind == "output_epoch":
                        if (type(value.get("connection_generation")) is not int
                                or value["connection_generation"] != self.connection_generation):
                            raise ValueError("CANARY_OUTPUT_GENERATION")
                        identity = {key: value.get(key) for key in
                                    ("source_id", "server_source_id", "epoch_ref", "provider_response_id")}
                        if (any(not isinstance(identity[key], str) or not 1 <= len(identity[key]) <= 256
                                for key in ("source_id", "server_source_id"))
                                or type(identity["provider_response_id"]) is not int
                                or not 1 <= identity["provider_response_id"] <= 0xFFFFFFFF
                                or type(value.get("response_id")) is not int
                                or value.get("response_id") != identity["provider_response_id"]
                                or any(type(value.get(key)) is not int or value[key] < 1
                                       for key in ("stream_ref", "epoch_ref"))):
                            raise ValueError("CANARY_OUTPUT_EPOCH_IDENTITY")
                        previous = self.output_sources.get(value["stream_ref"])
                        if (previous and any(previous[key] != item for key, item in identity.items())) or any(
                                ref != value["stream_ref"] and item["source_id"] == identity["source_id"]
                                for ref, item in self.output_sources.items()):
                            raise ValueError("CANARY_OUTPUT_EPOCH_IDENTITY")
                        self.output_sources[value["stream_ref"]] = previous or {**identity, "last_event_seq": 0}
                        await asyncio.to_thread(self.persist, b"J", raw.encode())
                    elif kind == "output_interrupt":
                        binding = next((item for item in self.output_sources.values()
                                        if item["source_id"] == value.get("source_id")), None)
                        if (type(value.get("connection_generation")) is not int
                                or value["connection_generation"] != self.connection_generation):
                            self.output_interrupt_rejections["connection_generation"] += 1
                        elif binding is None:
                            # The urgent control may precede its epoch; unknown
                            # source ownership never becomes a terminal receipt.
                            self.output_interrupt_rejections["unknown_source"] += 1
                        elif (type(value.get("provider_response_id")) is not int
                              or value["provider_response_id"] != binding["provider_response_id"]):
                            self.output_interrupt_rejections["provider_response_id"] += 1
                        elif binding["source_id"] in self.output_interrupts:
                            self.output_interrupt_duplicates += 1
                        else:
                            await asyncio.to_thread(self.persist, b"J", raw.encode())
                            self.output_interrupts[binding["source_id"]] = {
                                "source_id": binding["source_id"], "server_source_id": binding["server_source_id"],
                                "provider_response_id": binding["provider_response_id"],
                                "connection_generation": self.connection_generation,
                            }
                    elif kind == "audio_ack":
                        self.acks[value["source_id"]] = value["chunk_seq"]
                    elif kind == "event":
                        event = value.get("event") or {}
                        self.cursor = max(self.cursor, int(event.get("seq") or 0))
                        self.events.append(event)
                        if event.get("type") in {"error", "resource_fallback"}:
                            raise ValueError("CANARY_PROVIDER_OR_AUTHORITY_FAILURE")
                    self.controls.append(value)
                self.changed.set()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.error = exc
        finally:
            self.changed.set()

    async def wait(self, predicate, timeout=15):
        async with asyncio.timeout(timeout):
            while True:
                self.changed.clear()
                if self.error is not None:
                    raise self.error
                value = predicate()
                if value:
                    return value
                if self.reader.done():
                    raise ValueError("CANARY_SOCKET_CLOSED")
                await self.changed.wait()

    async def control(self, kind, **fields):
        return await self.wait(lambda: next((c for c in reversed(self.controls)
            if c.get("type") == kind and all(c.get(k) == v for k, v in fields.items())), None))

    async def replay(self, recording, phrases, expected_portions, replay_id, generation, limits, timeout,
                     *, receipt=None, diagnostic_continue_on_transcript_mismatch=False):
        from live_interaction.audio_v2 import AudioFrame, UPLINK_MAGIC
        source_id = "canary_" + replay_id + "_" + secrets.token_hex(6)
        clock_start = recording.chunks[0].capture_start_us
        anchor = time.monotonic()
        result = receipt if receipt is not None else {}
        result.update(replay_id=replay_id, connection_generation=generation, passed=False,
                      phase="source_open", client_source_id=source_id, client_frames_sent=0,
                      original_chunks=len(recording.chunks), source_sealed=False,
                      known_phrase_indices=list(range(len(phrases))))
        before_seq = self.cursor
        before_output = {item["source_id"] for item in self.output_sources.values()}
        expected = tuple(normalized(phrase) for phrase in phrases)

        def snapshot():
            events = [e for e in self.events if int(e.get("seq") or 0) > before_seq]
            text = " ".join(str(e.get("text") or "") for e in events if e.get("type") == "input_transcript")
            matches = [phrase in normalized(text) for phrase in expected]
            turns = sum(e.get("type") == "turn_complete" for e in events)
            # A control has no event sequence. Attribute terminal coverage only
            # through an actual received audio frame newer than this replay.
            eligible = {item["source_id"] for item in self.output_sources.values()
                        if item["last_event_seq"] > before_seq} - before_output
            ended = self.output_ended & eligible
            interrupted = self.output_interrupts.keys() & eligible
            outputs, terminals = len(ended), len(ended | interrupted)
            result.update(main_phrase_matches=matches,
                missing_phrase_indices=[i for i, matched in enumerate(matches) if not matched],
                main_transcript_matches=all(matches), main_transcript_chars=len(text),
                normalized_transcript_sha256=digest(normalized(text).encode()),
                turn_complete_count=turns, turn_complete_required=len(expected),
                turn_complete_received=turns >= len(expected),
                output_ended_source_count=outputs, output_ended_source_required=len(expected),
                output_encoded_tail_received=outputs >= len(expected),
                output_ended_source_ids=sorted(ended),
                output_interrupted_source_count=len(interrupted),
                output_interrupt_receipts=[self.output_interrupts[key] for key in sorted(interrupted)],
                output_interrupt_unattributed_source_count=len(self.output_interrupts.keys() - eligible),
                output_interrupt_rejections=dict(self.output_interrupt_rejections),
                output_interrupt_duplicate_count=self.output_interrupt_duplicates,
                output_terminal_source_count=terminals, output_terminal_source_required=len(expected),
                output_terminal_received=terminals >= len(expected),
                output_terminal_policy="received_epoch_end_or_confirmed_source_interrupt",
                output_playback_proven=False,
                elapsed_ms=round((time.monotonic() - anchor) * 1000),
                event_counts=dict(Counter(e.get("type") for e in events)))
            if not result["source_sealed"]:
                result["server_durable_seq"] = self.acks.get(source_id, 0)
            return all(matches) and turns >= len(expected) and terminals >= len(expected)

        try:
            await self.send({"type": "source_open", "source_id": source_id,
                             "started_at_unix_ms": recording.started_at_unix_ms,
                             "source_time_now_us": clock_start, "lane": "live"})
            bound = await self.control("source_bound", source_id=source_id)
            result["server_source_id"] = bound["server_source_id"]
            self.caption.sources[bound["server_source_id"]] = {"replay_id": replay_id,
                "portions": recording.portions(), "expected_portions": expected_portions}
            before_seq = self.cursor
            before_output = {item["source_id"] for item in self.output_sources.values()}
            epoch_refs, outstanding = {}, deque()
            result["phase"] = "send_audio"
            for chunk in recording.chunks:
                if chunk.epoch_id not in epoch_refs:
                    await self.send({"type": "codec_epoch", "source_ref": bound["source_ref"],
                                     "descriptor": recording.epochs[chunk.epoch_id]})
                    epoch_refs[chunk.epoch_id] = (await self.control("epoch_bound", source_id=source_id,
                        epoch_id=chunk.epoch_id))["epoch_ref"]
                due = anchor + (chunk.capture_start_us + chunk.duration_us - clock_start) / 1_000_000
                if due > time.monotonic():
                    await asyncio.sleep(due - time.monotonic())
                def has_room():
                    while outstanding and outstanding[0].chunk_seq <= self.acks.get(source_id, 0):
                        outstanding.popleft()
                    return (sum(len(c.payload) for c in outstanding) + len(chunk.payload) <= limits["live_window_bytes"]
                            and sum(c.duration_us for c in outstanding) + chunk.duration_us <= limits["live_window_duration_us"]
                            and len(outstanding) < 31)
                await self.wait(has_room, timeout=3)
                await self.ws.send(AudioFrame(UPLINK_MAGIC, chunk.flags, bound["source_ref"], epoch_refs[chunk.epoch_id],
                    chunk.chunk_seq, chunk.capture_start_us, chunk.duration_us, generation, 0, chunk.payload).encode())
                result["client_frames_sent"] += 1
                outstanding.append(chunk)
            result["phase"] = "source_seal"
            await self.send({"type": "source_seal", "source_ref": bound["source_ref"],
                             "last_chunk_seq": len(recording.chunks), "capture_end_us": recording.capture_end_us})
            sealed = await self.control("source_sealed", source_id=source_id)
            if sealed.get("complete") is not True:
                raise ValueError("CANARY_ORIGINAL_NOT_DURABLE")
            result.update(source_sealed=True, server_durable_seq=sealed["server_durable_seq"], phase="await_turn_result")
            # A diagnostic MAY continue to the next socket generation after
            # proven durable input, observed final transcripts, complete turns
            # and terminal output. It NEVER promotes a missing verbatim phrase
            # to primary acceptance; the default path remains strict.
            await self.wait(lambda: snapshot() or (
                diagnostic_continue_on_transcript_mismatch
                and result["turn_complete_received"]
                and result["output_terminal_received"]
                and result["event_counts"].get("input_transcript", 0) >= len(expected)), timeout)
            strict_pass = bool(snapshot())
            result["phase"] = "source_unbind"
            await self.send({"type": "source_unbind", "source_ref": bound["source_ref"]})
            await self.control("source_unbound", source_id=source_id)
            result.update(passed=strict_pass,
                diagnostic_only=bool(diagnostic_continue_on_transcript_mismatch and not strict_pass),
                phase="complete" if strict_pass else "diagnostic_transcript_mismatch")
            return result
        except BaseException as exc:
            result["error_type"] = type(exc).__name__
            raise
        finally:
            # The caller owns this dictionary before the first await, so an
            # unsuccessful replay survives cancellation and the final receipt.
            # Original text stays in the isolated durable store, not this report.
            snapshot()

    async def close(self):
        self.heartbeat.cancel()
        try:
            await bounded(self.ws.close(), 5)
        finally:
            self.reader.cancel()
            await bounded(asyncio.gather(self.reader, self.heartbeat, return_exceptions=True), 2)


async def run_provider(args, recording: Recording, report: dict) -> dict:
    caption = CaptionEvidence(tuple(args.phrases))
    report["caption_qualification"] = {"requested": args.qualify_captions, "observer_enabled": False,
                                       "status": "unverified" if args.qualify_captions else "not_requested"}
    report["primary_passed"] = False
    store = host = listener = server = serving = http = started = wire = None
    conversation_id = ""
    checks_passed = False

    async def work():
        nonlocal store, host, listener, server, serving, http, started, wire, conversation_id
        import uvicorn
        import websockets
        from projects_hub.app import create_app
        from projects_hub.live_runtime import build_live_host
        from projects_hub.settings import Settings
        from projects_hub.store import DurableStore
        from live_interaction.audio_v2 import OPUS_PROFILE

        # Resolve versions before opening any socket or starting provider work.
        report["versions"] = {name: importlib.metadata.version(name) for name in
                              ("projects-hub", "live-interaction", "ai-resource-control", "av")}
        report["implementation_sha256"] = implementation_fingerprints()
        report["event_loop"] = type(asyncio.get_running_loop()).__name__
        if args.use_installed_provider_environment:
            use_installed_provider_environment()
            report["provider_environment_source"] = "installed_allowlisted_provider_environment"
        args.data_dir.mkdir(parents=True, exist_ok=True)
        if list(args.data_dir.iterdir()):
            raise ValueError("CANARY_DATA_DIR_MUST_BE_EMPTY")
        os.chmod(args.data_dir, 0o700)
        store = DurableStore(args.data_dir)
        # Acceptance needs ordinary authority. The host's environment is unchanged.
        environment = {k: v for k, v in os.environ.items()
                       if k not in {"AI_RESOURCE_CONTROL_FALLBACK_KEY", "GOOGLE_API_KEY4"}}
        host = build_live_host(store, environment=environment)
        settings = Settings(data_dir=args.data_dir, static_dir=Path("web/dist"),
                            session_secret=secrets.token_hex(32), dev_auth=True,
                            cookie_secure=False, release_sha="isolated-native-provider-canary")
        app = create_app(settings, store=store, live_host=host)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(32)
        listener.setblocking(False)
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_config=None, access_log=False,
                                              lifespan="on", timeout_graceful_shutdown=5))
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        async with asyncio.timeout(15):
            while not server.started:
                if serving.done():
                    await serving
                    raise ValueError("CANARY_SERVER_NOT_STARTED")
                await asyncio.sleep(.02)
        http = HttpSession(f"http://127.0.0.1:{port}")
        _, conversation_id = await asyncio.to_thread(login_and_conversation, http, "Native voice provider canary")
        report["provider_calls_attempted"] = True
        started = await asyncio.to_thread(start_live, http, conversation_id, "native_canary_" + secrets.token_hex(8))
        sidecar = host._caption_sidecars.get(started["session_id"]) if args.qualify_captions else None
        report["caption_qualification"] = configure_caption_qualification(
            sidecar, caption, requested=args.qualify_captions)
        cursor = 0
        output_path = args.data_dir / "canary-received-v2.bin"
        with output_path.open("xb") as output:
            os.chmod(output_path, 0o600)
            for generation in (1, 2):
                if generation == 2:
                    _, ticket = await asyncio.to_thread(http.request, "POST",
                        f"/api/live/{conversation_id}/sessions/{started['session_id']}/socket-ticket", {})
                    started.update(ticket)
                ws = await websockets.connect(f"ws://127.0.0.1:{port}" + started["socket_url"],
                    origin=f"http://127.0.0.1:{port}", subprotocols=["wl-live-v2", "wl-ticket." + started["socket_ticket"]],
                    additional_headers={"Cookie": http.cookie_header()}, open_timeout=10, close_timeout=5, max_size=128 * 1024)
                wire = ReplaySocket(ws, output, caption, connection_generation=generation)
                await wire.send({"type": "hello", "protocol": "wl-live-v2", "attempt_id": started["attempt_id"],
                    "connection_generation": generation, "cursor": cursor,
                    "capabilities": {"uplink_codecs": [OPUS_PROFILE], "downlink_codecs": [OPUS_PROFILE]}})
                hello = await wire.control("hello_ack")
                replay = {"replay_id": f"g{generation}", "connection_generation": generation, "passed": False}
                report["replays"].append(replay)
                await wire.replay(recording, args.phrases, args.portion, f"g{generation}",
                    generation, hello["audio_limits"], args.turn_timeout, receipt=replay,
                    diagnostic_continue_on_transcript_mismatch=args.diagnostic_continue_on_transcript_mismatch)
                cursor = wire.cursor
                await wire.close()
                wire = None
        if report["caption_qualification"]["observer_enabled"]:
            # The observation window is never used to assign an utterance.
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not caption.result()["all_replays_support_candidate_clock"]:
                await asyncio.sleep(.1)
        return len(report["replays"]) == 2 and all(replay.get("passed") is True for replay in report["replays"])

    try:
        checks_passed = await bounded(work(), args.timeout)
    except BaseException as exc:
        record_error(report, exc)
    finally:
        report["caption_clock"] = caption.result()
        qualification = report["caption_qualification"]
        if qualification["observer_enabled"]:
            qualification["status"] = ("passed" if report["caption_clock"]["all_replays_support_candidate_clock"]
                                       else "failed")
        report["primary_checks_passed_before_cleanup"] = bool(checks_passed)
        report["checks_passed_before_cleanup"] = (bool(checks_passed)
            and (not qualification["requested"] or qualification["status"] == "passed"))
        report["passed"] = False
        save_report(args.report, report)
        deadline = time.monotonic() + 25
        try:
            if wire is not None:
                await cleanup_step(wire.close(), report, "socket_close", min(deadline, time.monotonic()+7))
            if started and http:
                await cleanup_step(asyncio.to_thread(http.request, "POST",
                    f"/api/live/{conversation_id}/sessions/{started['session_id']}/stop", {}, timeout=8),
                    report, "live_stop", min(deadline, time.monotonic()+9))
            if server is not None:
                server.should_exit = True
            if serving is not None:
                if not await cleanup_step(serving, report, "asgi_shutdown", min(deadline, time.monotonic()+8)):
                    server.force_exit = True
            if host is not None:
                # Also covers setup failure before ASGI lifespan became active.
                if host.sessions:
                    await cleanup_step(host.stop_all(), report, "host_stop_all", deadline)
                await cleanup_step(host.close_audio_storage(), report, "audio_storage_close", deadline)
                report["host_sessions_after_close"] = len(host.sessions)
                report["caption_sidecars_after_close"] = len(host._caption_sidecars)
        except BaseException as exc:
            record_error(report, exc, phase="cleanup")
        finally:
            if listener is not None:
                listener.close()
            if store is not None:
                await cleanup_step(asyncio.to_thread(store.close), report, "store_close", deadline)
            update_acceptance(report, loop_shutdown_pending=True)
            save_report(args.report, report)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture-evidence", required=True, type=Path)
    parser.add_argument("--voice-flow", type=Path, help="Exact UTP voice-e2e/voice-flow.json from the SAME Android job artifact")
    parser.add_argument("--source", help="One original server/client source ID, replayed intact twice")
    parser.add_argument("--phrase", dest="phrases", action="append", help="Known synthetic phrase; at least two distinct phrases")
    parser.add_argument("--portion", type=int, action="append", help="Original zero-based speech portion for each --phrase; defaults to capture order")
    parser.add_argument("--data-dir", type=Path, help="NEW isolated server/client evidence directory")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--run-provider", action="store_true")
    parser.add_argument("--diagnostic-continue-on-transcript-mismatch", action="store_true",
                        help="Continue to WSS generation 2 only if real input receipts, finals and output tails exist; strict acceptance stays failed")
    parser.add_argument("--qualify-captions", action="store_true",
                        help="Additionally run and require strict caption qualification using a second ASR provider")
    parser.add_argument("--use-installed-provider-environment", action="store_true",
                        help="Read only installed providers.env using this candidate installer's allowlist")
    parser.add_argument("--turn-timeout", type=float, default=60)
    parser.add_argument("--timeout", type=float, default=360, help="Total provider-work deadline, followed by bounded cleanup")
    args = parser.parse_args(argv)
    args.phrases = args.phrases or list(DEFAULT_PHRASES)
    args.portion = args.portion if args.portion is not None else list(range(len(args.phrases)))
    original = args.fixture_evidence / "fixture-data" / "projects-hub.sqlite3"
    if not args.run_provider:
        inspection = {"provider_calls_attempted": False, "input_sources": inventory(original)}
        recording = None
        if args.source:
            recording = load_recording(original, args.source)
            inspection["selected_recording"] = recording.receipt()
        if args.voice_flow:
            inspection["fixture_receipts"] = fixture_receipts(args.fixture_evidence, args.voice_flow,
                recording.conversation_id if recording else None)
        print(json.dumps(inspection, ensure_ascii=False, indent=2))
        return 0
    if (not args.source or not 2 <= len(args.phrases) <= 4 or len(args.portion) != len(args.phrases)
            or len(set(args.portion)) != len(args.portion) or min(args.portion) < 0
            or not all(normalized(p) for p in args.phrases) or len(set(map(normalized, args.phrases))) != len(args.phrases)
            or not args.data_dir or not args.report or not args.voice_flow
            or not 5 <= args.turn_timeout <= 90 or not 30 <= args.timeout <= 480):
        parser.error("Provider run needs one whole source, distinct known phrases/portions, --voice-flow, fresh --data-dir/--report and bounded deadlines")
    if args.report.exists():
        parser.error("Report already exists; preserve the prior attempt evidence")
    if args.report.resolve().is_relative_to(args.data_dir.resolve()):
        parser.error("Place --report outside the new --data-dir so the store can start empty")
    report = {"passed": False, "primary_passed": False, "provider_calls_attempted": False,
              "diagnostic_continue_on_transcript_mismatch": args.diagnostic_continue_on_transcript_mismatch,
              "versions": {}, "replays": [],
              "caption_qualification": {"requested": args.qualify_captions, "observer_enabled": False,
                                        "status": "unverified" if args.qualify_captions else "not_requested"},
              "claim": "Native-captured compressed-input primary provider integration; separate from Android playback acceptance",
              "provenance": "Whole synthetic CI source intentionally replayed as new input, including its known offline phrase; this gate does not test outage suppression"}
    try:
        recording = load_recording(original, args.source)
        if max(args.portion) >= len(recording.portions()):
            raise ValueError("CANARY_KNOWN_PORTION_NOT_FOUND")
        report["recording"] = recording.receipt()
        report["fixture_receipts"] = fixture_receipts(args.fixture_evidence, args.voice_flow, recording.conversation_id)
        run_reported(args.report, report, lambda receipt: run_provider(args, recording, receipt))
    except BaseException as exc:
        record_error(report, exc, phase="preflight_or_setup")
    finally:
        # Both verdicts stay false through run_reported's loop/task shutdown.
        # Its checks_passed_before_loop_shutdown is therefore provisional false;
        # primary_checks_passed_before_cleanup retains the actual replay result.
        update_acceptance(report)
        save_report(args.report, report)
    print(json.dumps({key: report[key] for key in ("passed", "primary_passed", "caption_qualification", "claim",
                                                  "versions", "provider_calls_attempted")}, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
