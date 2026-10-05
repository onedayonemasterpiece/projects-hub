#!/usr/bin/env python3
"""Provider-backed long-input acceptance for PH-VOICE-2026-10-05-R1.

Uses the deployed Projects Hub WSS path and real Gemini Live provider. A local
speech synthesizer creates deterministic PCM; frames are sent at real-time pace.
This is intentionally not a substitute for the final physical-phone microphone
acceptance (V11).
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import struct
import tempfile
import time
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
    "Это техническая проверка длинной речи Projects Hub. "
    "Контроль начало: фиолетовый маяк семьдесят три. "
    "Ничего не сохраняй, не вызывай инструменты и не выполняй действий. "
    "Контроль середина: зелёный компас сорок два. "
    "Продолжай только слушать эту длинную реплику. "
    "Контроль конец: янтарный мост девятнадцать. "
)
ENGLISH_TEXT = (
    "This is a technical long speech test for Projects Hub. "
    "Control start: purple beacon seventy three. "
    "Do not save anything, call tools, or perform actions. "
    "Control middle: green compass forty two. "
    "Keep listening to this long utterance. "
    "Control end: amber bridge nineteen. "
)


def synthesize() -> tuple[bytes, str, tuple[str, str, str]]:
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
    raise RuntimeError("no local speech synthesizer available")


def repeated_pcm(seed: bytes, seconds: float) -> bytes:
    needed = int(seconds * 32000)
    if not seed or len(seed) % 2:
        raise RuntimeError("invalid PCM seed")
    return (seed * (needed // len(seed) + 1))[:needed]


async def run_once(args, run_number: int, seed: bytes, markers: tuple[str, str, str]) -> dict[str, Any]:
    http = HttpSession(args.http_base)
    _, conversation_id = login_and_conversation(http, args.display_name)
    attempt = f"ph_voice_long_{run_number}_{int(time.time())}"
    started = start_live(http, conversation_id, attempt)
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
    first_partial_at: float | None = None
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
                nonlocal first_partial_at, turn_complete_at
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
                                if first_partial_at is None:
                                    first_partial_at = now
                        elif event_kind == "input_transcript":
                            text = str(event.get("text") or "")
                            if text:
                                finals.append(text)
                                if first_partial_at is None:
                                    first_partial_at = now
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
            if terminal is None:
                await ws.send(json.dumps({
                    "type": "input",
                    "message": {"audio_stream_end": True},
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
        first_partial_ms = (
            round((first_partial_at - stream_started_at) * 1000)
            if first_partial_at is not None and stream_started_at is not None
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
        passed = bool(
            terminal is None
            and streamed_seconds >= args.seconds - 1.0
            and ack_count >= max(1, seq - 2)
            and len(checkpoints) == len(checkpoints_seconds)
            and first_partial_ms is not None
            and first_partial_ms <= args.partial_latency_ms
            and finals
            and all(marker_hits)
            and sum(len(item) for item in finals) >= args.min_final_chars
            and int(source.get("audio_bytes") or 0) >= len(pcm)
            and int(source.get("transcript_revision") or 0) > 0
            and tool_calls == 0
            and not resource_denials
        )
        return {
            "ok": passed,
            "run": run_number,
            "session_id": session_id,
            "source_id": source_id,
            "duration_target_s": args.seconds,
            "streamed_s": round(streamed_seconds, 3),
            "frames_sent": seq,
            "audio_ack_count": ack_count,
            "max_audio_ack_seq": max_ack,
            "checkpoints": checkpoints,
            "first_partial_ms": first_partial_ms,
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
    seed, fixture, markers = synthesize()
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
