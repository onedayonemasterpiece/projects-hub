#!/usr/bin/env python3
"""Provider-backed long-input acceptance for PH-VOICE-2026-10-05-R1.

Uses the deployed Projects Hub WSS path and real Gemini Live provider. A local
speech synthesizer creates deterministic PCM; frames are sent at real-time pace.
This is intentionally not a substitute for the final physical-phone microphone
acceptance (V11).
"""
from __future__ import annotations

from array import array
import argparse
import asyncio
import json
import re
from pathlib import Path
import struct
import tempfile
import time
import sys
from typing import Any

from websockets.exceptions import ConnectionClosed

from devcoveer_voice_memory_canary import (
    ESPEAK_CANDIDATES,
    FFMPEG_CANDIDATES,
    PICO_CANDIDATES,
    _run_fixture,
    _wav_to_pcm16_16k,
)
from devcoveer_wss_canary import (
    INPUT_MAGIC,
    HttpSession,
    _origin,
    _ws_base,
    hello,
    login_and_conversation,
    open_socket,
    start_live,
)

RUSSIAN_TEXT = (
    "Фиолетовый маяк семьдесят три стоит у спокойного берега. "
    "Зелёный компас сорок два лежит рядом с картой старого города. "
    "Утренний ветер проходит между деревьями, а над водой медленно движутся облака. "
    "Янтарный мост девятнадцать отражается в тихой реке. "
)
OUTPUT_MAGIC = 0x574C4F31

ENGLISH_TEXT = (
    "Purple beacon seventy three stands beside a quiet shore. "
    "Green compass forty two rests next to a map of the old city. "
    "Morning wind moves between the trees while clouds drift slowly above the water. "
    "Amber bridge nineteen is reflected in the calm river. "
)


def synthesize_local() -> tuple[bytes, str, tuple[str, str, str]] | None:
    with tempfile.TemporaryDirectory(prefix="projects-hub-long-voice-") as tmp:
        root = Path(tmp)
        for executable in ESPEAK_CANDIDATES:
            if not executable.is_file():
                continue
            wav = root / "espeak.wav"
            if _run_fixture(
                [str(executable), "-v", "ru", "-s", "145", "-w", str(wav), RUSSIAN_TEXT],
                wav,
            ):
                return _wav_to_pcm16_16k(wav), executable.name, (
                    "фиолетов",
                    "зелён",
                    "янтар",
                )
        for executable in PICO_CANDIDATES:
            if not executable.is_file():
                continue
            wav = root / "pico.wav"
            if _run_fixture(
                [str(executable), "-l=ru-RU", "-w", str(wav), RUSSIAN_TEXT],
                wav,
            ):
                return _wav_to_pcm16_16k(wav), executable.name, (
                    "фиолетов",
                    "зелён",
                    "янтар",
                )
        for executable in FFMPEG_CANDIDATES:
            if not executable.is_file():
                continue
            pcm = root / "flite.pcm"
            escaped = ENGLISH_TEXT.replace("'", "")
            if _run_fixture(
                [
                    str(executable), "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", f"flite=text='{escaped}'",
                    "-ar", "16000", "-ac", "1", "-f", "s16le", "-y", str(pcm),
                ],
                pcm,
            ):
                raw = pcm.read_bytes()
                if raw and len(raw) % 2 == 0:
                    return raw, executable.name, ("purple", "green", "amber")
    return None


async def start_live_with_budget_retry(
    http: HttpSession,
    conversation_id: str,
    attempt_base: str,
    *,
    timeout: float = 120.0,
) -> tuple[dict[str, Any], int]:
    deadline = time.monotonic() + timeout
    retries = 0
    while True:
        attempt_id = attempt_base if retries == 0 else f"{attempt_base}_r{retries}"
        try:
            return start_live(http, conversation_id, attempt_id), retries
        except RuntimeError as exc:
            if "RESOURCE_TOKEN_BUDGET" not in str(exc) or time.monotonic() >= deadline:
                raise
            retries += 1
            await asyncio.sleep(min(5.0, max(0.2, deadline - time.monotonic())))


def _resample_pcm16(pcm: bytes, source_rate: int, target_rate: int = 16000) -> bytes:
    if not pcm or len(pcm) % 2 or source_rate <= 0 or target_rate <= 0:
        raise RuntimeError("invalid provider PCM fixture")
    if source_rate == target_rate:
        return pcm
    samples = array("h")
    samples.frombytes(pcm)
    if sys.byteorder != "little":
        samples.byteswap()
    output_count = max(1, int(len(samples) * target_rate / source_rate))
    converted = array("h")
    scale = source_rate / target_rate
    last = len(samples) - 1
    for out_index in range(output_count):
        position = min(last, out_index * scale)
        left = int(position)
        right = min(last, left + 1)
        fraction = position - left
        value = int(samples[left] + (samples[right] - samples[left]) * fraction)
        converted.append(max(-32768, min(32767, value)))
    if sys.byteorder != "little":
        converted.byteswap()
    return converted.tobytes()


async def synthesize_from_provider(args) -> tuple[bytes, str, tuple[str, str, str]]:
    http = HttpSession(args.http_base)
    _, conversation_id = login_and_conversation(http, args.display_name + " Fixture")
    started, _ = await start_live_with_budget_retry(
        http,
        conversation_id,
        f"ph_voice_fixture_{int(time.time())}",
    )
    session_id = str(started["session_id"])
    chunks: list[bytes] = []
    transcript: list[str] = []
    rate: int | None = None
    markers = ("purple", "green", "amber")
    try:
        async with await open_socket(
            started=started,
            cookie_header=http.cookie_header(),
            ws_base=args.ws_base,
            origin=args.origin,
        ) as ws:
            await hello(ws, started)
            prompt = (
                "Technical audio fixture. Do not call tools or save anything. "
                "Speak exactly the sentence after the colon, with no preface or explanation: "
                + ENGLISH_TEXT
            )
            await ws.send(json.dumps(
                {"type": "input", "message": {"text": prompt}},
                ensure_ascii=False,
            ))
            deadline = time.monotonic() + 60.0
            complete = False
            while time.monotonic() < deadline and not complete:
                try:
                    raw = await asyncio.wait_for(
                        ws.recv(),
                        min(10.0, max(0.1, deadline - time.monotonic())),
                    )
                except asyncio.TimeoutError:
                    continue
                if isinstance(raw, bytes):
                    if len(raw) < 14:
                        continue
                    magic, _seq, chunk_rate = struct.unpack("!III", raw[:12])
                    body = raw[12:]
                    if magic != OUTPUT_MAGIC or not body or len(body) % 2:
                        continue
                    if rate is None:
                        rate = chunk_rate
                    elif rate != chunk_rate:
                        raise RuntimeError("provider fixture sample rate changed mid-response")
                    chunks.append(body)
                    continue
                payload = json.loads(raw)
                if payload.get("type") != "event":
                    continue
                event = payload.get("event") or {}
                kind = str(event.get("type") or "")
                if kind == "output_transcript" and isinstance(event.get("text"), str):
                    transcript.append(event["text"])
                elif kind == "tool_call":
                    raise RuntimeError("provider fixture unexpectedly called a tool")
                elif kind == "error":
                    raise RuntimeError(
                        "provider fixture failed: " + str(event.get("code") or "LIVE_ERROR")
                    )
                elif kind == "turn_complete":
                    complete = True
            if not complete:
                raise RuntimeError("provider fixture did not complete")
            try:
                await ws.send(json.dumps({"type": "stop", "reason": "ph_voice_fixture"}))
            except Exception:
                pass
        spoken = " ".join(transcript).lower()
        if not chunks or rate is None:
            raise RuntimeError("provider fixture returned no PCM")
        words = []
        for word in re.findall(r"[a-z]{4,}", spoken):
            if word not in words:
                words.append(word)
        if len(words) < 3:
            raise RuntimeError("provider fixture transcript is too short for control markers")
        markers = (words[0], words[len(words) // 2], words[-1])
        return (
            _resample_pcm16(b"".join(chunks), rate),
            f"gemini-live-output-{rate}hz",
            markers,
        )
    finally:
        try:
            http.request(
                "POST",
                f"/api/live/{conversation_id}/sessions/{session_id}/stop",
                {},
                timeout=10.0,
            )
        except Exception:
            pass


def repeated_pcm(seed: bytes, seconds: float) -> bytes:
    needed = int(seconds * 32000)
    if not seed or len(seed) % 2:
        raise RuntimeError("invalid PCM seed")
    return (seed * (needed // len(seed) + 1))[:needed]


async def run_once(args, run_number: int, seed: bytes, markers: tuple[str, str, str]) -> dict[str, Any]:
    http = HttpSession(args.http_base)
    _, conversation_id = login_and_conversation(http, args.display_name)
    attempt = f"ph_voice_long_{run_number}_{int(time.time())}"
    started, start_retries = await start_live_with_budget_retry(
        http,
        conversation_id,
        attempt,
    )
    session_id = str(started["session_id"])
    source_id = str(started.get("source_id") or "")
    pcm = repeated_pcm(seed, args.seconds)
    frame_bytes = 3200  # 100 ms, PCM16 mono 16 kHz
    event_types: list[str] = []
    finals: list[str] = []
    interims: list[str] = []
    budget_events: list[dict[str, Any]] = []
    terminal: dict[str, Any] | None = None
    ack_count = 0
    max_ack = 0
    tool_calls = 0
    turn_complete_at: float | None = None
    first_interim_at: float | None = None
    first_any_transcript_at: float | None = None
    stream_started_at: float | None = None
    checkpoints: list[dict[str, Any]] = []
    receiver_finished = asyncio.Event()
    turn_complete = asyncio.Event()

    try:
        async with await open_socket(
            started=started,
            cookie_header=http.cookie_header(),
            ws_base=args.ws_base,
            origin=args.origin,
        ) as ws:
            await hello(ws, started)

            async def receive_loop() -> None:
                nonlocal ack_count, max_ack, terminal, tool_calls
                nonlocal first_interim_at, first_any_transcript_at, turn_complete_at
                try:
                    while True:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), 5.0)
                        except asyncio.TimeoutError:
                            if turn_complete.is_set():
                                return
                            continue
                        now = time.monotonic()
                        if isinstance(raw, bytes):
                            event_types.append("audio_binary")
                            continue
                        payload = json.loads(raw)
                        kind = str(payload.get("type") or "")
                        if kind == "audio_ack":
                            ack_count += 1
                            max_ack = max(max_ack, int(payload.get("seq") or 0))
                            continue
                        if kind != "event":
                            if kind:
                                event_types.append(kind)
                            continue
                        event = payload.get("event") or {}
                        event_kind = str(event.get("type") or "")
                        if event_kind:
                            event_types.append(event_kind)
                        if event_kind == "interim_input_transcript":
                            text = str(event.get("text") or "")
                            if text:
                                interims.append(text)
                                if first_interim_at is None:
                                    first_interim_at = now
                                if first_any_transcript_at is None:
                                    first_any_transcript_at = now
                        elif event_kind == "input_transcript":
                            text = str(event.get("text") or "")
                            if text:
                                finals.append(text)
                                if first_any_transcript_at is None:
                                    first_any_transcript_at = now
                        elif event_kind == "resource_budget":
                            budget_events.append({
                                key: event.get(key)
                                for key in (
                                    "status", "modality", "code",
                                    "estimated_units", "requested_units", "granted_units",
                                )
                                if event.get(key) is not None
                            })
                        elif event_kind == "error":
                            terminal = {
                                "kind": "error",
                                "code": str(event.get("code") or ""),
                            }
                            turn_complete.set()
                            return
                        elif event_kind == "tool_call":
                            tool_calls += 1
                        elif event_kind == "turn_complete":
                            turn_complete_at = now
                            turn_complete.set()
                            return
                except ConnectionClosed as exc:
                    if not turn_complete.is_set():
                        terminal = {
                            "kind": "connection_closed",
                            "code": int(exc.code),
                        }
                        turn_complete.set()
                finally:
                    receiver_finished.set()

            receiver = asyncio.create_task(receive_loop())
            await ws.send(json.dumps({
                "type": "input",
                "message": {"activity_start": True},
            }))
            stream_started_at = time.monotonic()
            checkpoints_seconds = sorted({
                value for value in (30.0, 60.0, 90.0, 180.0, float(args.seconds))
                if value <= args.seconds
            })
            next_checkpoint = 0
            seq = 0
            target = stream_started_at

            for offset in range(0, len(pcm), frame_bytes):
                if terminal:
                    break
                frame = pcm[offset: offset + frame_bytes]
                seq += 1
                age_ms = max(0, round((time.monotonic() - target) * 1000))
                await ws.send(struct.pack("!III", INPUT_MAGIC, seq, age_ms) + frame)
                target = stream_started_at + min(len(pcm), offset + len(frame)) / 32000.0
                while next_checkpoint < len(checkpoints_seconds):
                    value = checkpoints_seconds[next_checkpoint]
                    if time.monotonic() - stream_started_at < value:
                        break
                    checkpoints.append({
                        "second": value,
                        "terminal": terminal,
                        "acks": ack_count,
                        "interim_events": len(interims),
                        "final_events": len(finals),
                    })
                    next_checkpoint += 1
                delay = target - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)

            streamed_seconds = (
                time.monotonic() - stream_started_at if stream_started_at is not None else 0.0
            )
            while next_checkpoint < len(checkpoints_seconds):
                value = checkpoints_seconds[next_checkpoint]
                if streamed_seconds + 0.05 < value:
                    break
                checkpoints.append({
                    "second": value,
                    "terminal": terminal,
                    "acks": ack_count,
                    "interim_events": len(interims),
                    "final_events": len(finals),
                })
                next_checkpoint += 1
            if terminal is None:
                await ws.send(json.dumps({
                    "type": "input",
                    "message": {"activity_end": True},
                }))
                try:
                    await asyncio.wait_for(turn_complete.wait(), args.final_timeout)
                except asyncio.TimeoutError:
                    terminal = {"kind": "turn_timeout", "code": "TURN_COMPLETE_TIMEOUT"}

            if not receiver.done():
                if turn_complete.is_set():
                    try:
                        await asyncio.wait_for(receiver_finished.wait(), 2.0)
                    except asyncio.TimeoutError:
                        receiver.cancel()
                else:
                    receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)

            try:
                await ws.send(json.dumps({"type": "stop", "reason": "ph_voice_long_canary"}))
            except Exception:
                pass

        source = http.request(
            "GET", f"/api/sources/{source_id}", timeout=20.0
        )[1].get("source") or {}
        combined = " ".join(finals).lower()
        marker_hits = [marker in combined for marker in markers]
        first_interim_ms = (
            round((first_interim_at - stream_started_at) * 1000)
            if first_interim_at is not None and stream_started_at is not None
            else None
        )
        first_any_transcript_ms = (
            round((first_any_transcript_at - stream_started_at) * 1000)
            if first_any_transcript_at is not None and stream_started_at is not None
            else None
        )
        requested = sum(
            1 for item in budget_events if item.get("status") == "requested"
        )
        granted = sum(
            1 for item in budget_events if item.get("status") == "granted"
        )
        right_sizing = sum(
            1 for item in budget_events if item.get("status") == "right_sizing"
        )
        resource_denials = [
            item for item in budget_events
            if str(item.get("code") or "").startswith("RESOURCE_")
            and item.get("status") not in {"right_sizing"}
        ]
        transport_ok = bool(
            terminal is None
            and streamed_seconds >= args.seconds - 1.0
            and ack_count >= max(1, seq - 2)
            and len(checkpoints) == len(checkpoints_seconds)
            and int(source.get("audio_bytes") or 0) >= len(pcm)
        )
        interim_ok = bool(
            interims
            and first_interim_ms is not None
            and first_interim_ms <= args.partial_latency_ms
        )
        final_transcript_ok = bool(
            finals
            and all(marker_hits)
            and sum(len(item) for item in finals) >= args.min_final_chars
            and int(source.get("transcript_revision") or 0) > 0
        )
        resource_ok = not resource_denials
        safety_ok = tool_calls == 0
        passed = bool(
            transport_ok
            and interim_ok
            and final_transcript_ok
            and resource_ok
            and safety_ok
        )
        return {
            "ok": passed,
            "run": run_number,
            "session_id": session_id,
            "source_id": source_id,
            "duration_target_s": args.seconds,
            "start_budget_retries": start_retries,
            "v02_transport_ok": transport_ok,
            "v03_interim_ok": interim_ok,
            "v04_final_transcript_ok": final_transcript_ok,
            "resource_ok": resource_ok,
            "safety_ok": safety_ok,
            "streamed_s": round(streamed_seconds, 3),
            "frames_sent": seq,
            "audio_ack_count": ack_count,
            "max_audio_ack_seq": max_ack,
            "checkpoints": checkpoints,
            "first_interim_ms": first_interim_ms,
            "first_any_transcript_ms": first_any_transcript_ms,
            "interim_events": len(interims),
            "final_events": len(finals),
            "final_chars_total": sum(len(item) for item in finals),
            "final_chars_max_event": max((len(item) for item in finals), default=0),
            "marker_hits": marker_hits,
            "transcript_revision": int(source.get("transcript_revision") or 0),
            "durable_audio_bytes": int(source.get("audio_bytes") or 0),
            "resource_budget_events": len(budget_events),
            "resource_budget_requested": requested,
            "resource_budget_granted": granted,
            "resource_budget_right_sizing": right_sizing,
            "resource_denials": resource_denials,
            "tool_calls": tool_calls,
            "turn_complete_ms_after_stream": (
                round((turn_complete_at - (stream_started_at + args.seconds)) * 1000)
                if turn_complete_at is not None and stream_started_at is not None
                else None
            ),
            "terminal": terminal,
            "event_types": sorted(set(event_types)),
        }
    finally:
        try:
            http.request(
                "POST",
                f"/api/live/{conversation_id}/sessions/{session_id}/stop",
                {},
                timeout=10.0,
            )
        except Exception:
            pass


async def async_main(args) -> int:
    local = synthesize_local()
    if local is None:
        seed, fixture, markers = await synthesize_from_provider(args)
    else:
        seed, fixture, markers = local
    runs = []
    for run_number in range(1, args.runs + 1):
        result = await run_once(args, run_number, seed, markers)
        runs.append(result)
        print(json.dumps({"progress": result}, ensure_ascii=False, sort_keys=True), flush=True)
        if not result.get("ok") and not args.continue_after_failure:
            break
    summary = {
        "ok": len(runs) == args.runs and all(item.get("ok") for item in runs),
        "fixture": fixture,
        "physical_phone_acceptance": "not_run",
        "runs_requested": args.runs,
        "runs_completed": len(runs),
        "duration_seconds": args.seconds,
        "runs": runs,
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0 if summary["ok"] else 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--http-base", default="http://127.0.0.1:8196")
    parser.add_argument("--ws-base")
    parser.add_argument("--origin")
    parser.add_argument("--display-name", default="PH Voice Canary")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--seconds", type=float, default=300.0)
    parser.add_argument("--partial-latency-ms", type=int, default=10000)
    parser.add_argument("--min-final-chars", type=int, default=4000)
    parser.add_argument("--final-timeout", type=float, default=90.0)
    parser.add_argument("--continue-after-failure", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.runs <= 5:
        raise SystemExit("--runs must be 1..5")
    if not 5 <= args.seconds <= 600:
        raise SystemExit("--seconds must be 5..600")
    args.ws_base = args.ws_base or _ws_base(args.http_base)
    args.origin = args.origin or _origin(args.http_base)
    return asyncio.run(async_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
