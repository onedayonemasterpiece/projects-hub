#!/usr/bin/env python3
"""Production WSS acceptance for the optional Gemini Transcribe Live caption sidecar."""
from __future__ import annotations

import argparse
import asyncio
import json
import struct
import time

from devcoveer_voice_long_input_canary import synthesize_local
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

CAPTION_MODEL = "gemini-3.5-transcribe-live"
OUTPUT_MAGIC = 0x574C4F31


async def run(args) -> dict:
    fixture = synthesize_local()
    if fixture is None:
        raise RuntimeError("no local speech synthesizer is available")
    pcm, engine, markers = fixture

    http = HttpSession(args.http_base)
    _, conversation_id = login_and_conversation(http, args.display_name)
    started = start_live(
        http,
        conversation_id,
        f"ph_transcribe_{int(time.time())}",
    )
    if started.get("caption_enabled") is not True:
        raise RuntimeError("caption sidecar is not enabled")
    if started.get("caption_model") != CAPTION_MODEL:
        raise RuntimeError("unexpected caption model")

    result = {
        "ok": False,
        "caption_model": CAPTION_MODEL,
        "engine": engine,
        "caption_interim_events": 0,
        "caption_final_events": 0,
        "caption_final_chars": 0,
        "caption_marker_hits": [],
        "main_input_transcript_events": 0,
        "main_output_audio": False,
        "turn_complete": False,
        "audio_acks": 0,
        "caption_unavailable": None,
    }
    caption_finals: list[str] = []

    async with await open_socket(
        started=started,
        cookie_header=http.cookie_header(),
        ws_base=args.ws_base,
        origin=args.origin,
    ) as ws:
        await hello(ws, started)
        done = asyncio.Event()

        async def receive_loop():
            while True:
                raw = await ws.recv()
                if isinstance(raw, bytes):
                    if len(raw) >= 12:
                        magic = struct.unpack("!I", raw[:4])[0]
                        if magic == OUTPUT_MAGIC:
                            result["main_output_audio"] = True
                    continue
                payload = json.loads(raw)
                kind = str(payload.get("type") or "")
                if kind != "event":
                    continue
                event = payload.get("event") or {}
                event_kind = str(event.get("type") or "")
                if event_kind == "audio_ack":
                    result["audio_acks"] += 1
                elif event_kind == "caption_interim_transcript":
                    if str(event.get("text") or "").strip():
                        result["caption_interim_events"] += 1
                elif event_kind == "caption_final_transcript":
                    text = str(event.get("text") or "").strip()
                    if text:
                        result["caption_final_events"] += 1
                        caption_finals.append(text)
                elif event_kind == "caption_unavailable":
                    result["caption_unavailable"] = str(event.get("code") or "CAPTION_UNAVAILABLE")
                    done.set()
                    return
                elif event_kind == "input_transcript":
                    if str(event.get("text") or "").strip():
                        result["main_input_transcript_events"] += 1
                elif event_kind == "turn_complete":
                    result["turn_complete"] = True

                if (
                    result["caption_final_events"] > 0
                    and result["turn_complete"]
                    and result["main_output_audio"]
                ):
                    done.set()
                    return

        receiver = asyncio.create_task(receive_loop())
        await ws.send(json.dumps({
            "type": "input",
            "message": {"activity_start": True},
        }))

        frame_bytes = 1280  # 40 ms of PCM16 mono at 16 kHz; sidecar re-batches to Google's 100 ms.
        seq = 0
        started_at = time.monotonic()
        target = started_at
        for offset in range(0, len(pcm), frame_bytes):
            frame = pcm[offset: offset + frame_bytes]
            seq += 1
            age_ms = max(0, round((time.monotonic() - target) * 1000))
            await ws.send(struct.pack("!III", INPUT_MAGIC, seq, age_ms) + frame)
            target = started_at + min(len(pcm), offset + len(frame)) / 32000.0
            delay = target - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)

        await ws.send(json.dumps({
            "type": "input",
            "message": {"activity_end": True},
        }))
        try:
            await asyncio.wait_for(done.wait(), args.timeout)
        except asyncio.TimeoutError:
            pass

        try:
            await ws.send(json.dumps({"type": "stop", "reason": "transcribe_canary"}))
        except Exception:
            pass
        if not receiver.done():
            receiver.cancel()
        await asyncio.gather(receiver, return_exceptions=True)

    combined = " ".join(caption_finals).lower()
    result["caption_final_chars"] = len(combined)
    result["caption_marker_hits"] = [marker for marker in markers if marker in combined]
    result["ok"] = bool(
        result["caption_unavailable"] is None
        and result["caption_interim_events"] > 0
        and result["caption_final_events"] > 0
        and result["caption_marker_hits"]
        and result["turn_complete"]
        and result["main_output_audio"]
    )
    if not result["ok"]:
        raise RuntimeError(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--http-base", default="http://127.0.0.1:8088")
    parser.add_argument("--ws-base")
    parser.add_argument("--origin")
    parser.add_argument("--display-name", default="PH Transcribe Canary")
    parser.add_argument("--timeout", type=float, default=45.0)
    args = parser.parse_args()
    args.ws_base = args.ws_base or _ws_base(args.http_base)
    args.origin = args.origin or _origin(args.http_base)
    result = asyncio.run(run(args))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
