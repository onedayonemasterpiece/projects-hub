#!/usr/bin/env python3
"""Two-user real-provider isolation acceptance for PH-VOICE V09.

Creates/reuses two distinct loopback dev-auth actors, runs two simultaneous
180-second WSS voice sessions, and verifies actor-private source isolation.
No product tools are invoked by the harness.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import struct
import time
from typing import Any

from websockets.exceptions import ConnectionClosed

from devcoveer_voice_long_input_canary import (
    repeated_pcm,
    start_live_with_budget_retry,
    synthesize_from_provider,
    synthesize_local,
)
from devcoveer_wss_canary import (
    INPUT_MAGIC,
    HttpSession,
    _origin,
    _ws_base,
    hello,
    open_socket,
)


def login_actor(http: HttpSession, display_name: str) -> tuple[str, str, str]:
    _, login = http.request("POST", "/api/dev/login", {"display_name": display_name})
    actor_id = str((login.get("actor") or {}).get("id") or "")
    workspace_id = str((login.get("workspace") or {}).get("id") or "")
    if not actor_id or not workspace_id:
        raise RuntimeError("dev login did not return actor/workspace")
    _, conversation = http.request(
        "POST",
        "/api/conversations",
        {"workspace_id": workspace_id, "focus_project_id": None},
    )
    conversation_id = str(conversation.get("id") or "")
    if not conversation_id:
        raise RuntimeError("conversation creation did not return id")
    return actor_id, workspace_id, conversation_id


async def fixture(args) -> tuple[bytes, str]:
    local = synthesize_local()
    if local is not None:
        pcm, name, _markers = local
        return pcm, name
    pcm, name, _markers = await synthesize_from_provider(args)
    return pcm, name


async def stream_one(
    *,
    args,
    http: HttpSession,
    conversation_id: str,
    started: dict[str, Any],
    seed: bytes,
    seconds: float,
    label: str,
) -> dict[str, Any]:
    source_id = str(started.get("source_id") or "")
    session_id = str(started.get("session_id") or "")
    state: dict[str, Any] = {
        "acks": 0,
        "max_ack": 0,
        "input_transcripts": 0,
        "turn_complete": 0,
        "tool_calls": 0,
        "errors": [],
        "resource_denials": [],
        "event_types": [],
    }
    done = asyncio.Event()
    seq = 0
    pcm = repeated_pcm(seed, seconds)

    async with await open_socket(
        started=started,
        cookie_header=http.cookie_header(),
        ws_base=args.ws_base,
        origin=args.origin,
    ) as ws:
        await hello(ws, started)

        async def receiver():
            try:
                while not done.is_set():
                    raw = await ws.recv()
                    if isinstance(raw, bytes):
                        state["event_types"].append("audio_binary")
                        continue
                    payload = json.loads(raw)
                    kind = str(payload.get("type") or "")
                    if kind == "audio_ack":
                        state["acks"] += 1
                        state["max_ack"] = max(
                            state["max_ack"], int(payload.get("seq") or 0)
                        )
                        continue
                    if kind != "event":
                        if kind:
                            state["event_types"].append(kind)
                        continue
                    event = payload.get("event") or {}
                    event_kind = str(event.get("type") or "")
                    if event_kind:
                        state["event_types"].append(event_kind)
                    if event_kind == "input_transcript":
                        state["input_transcripts"] += 1
                    elif event_kind == "turn_complete":
                        state["turn_complete"] += 1
                        done.set()
                    elif event_kind == "tool_call":
                        state["tool_calls"] += 1
                    elif event_kind == "error":
                        state["errors"].append(str(event.get("code") or "LIVE_ERROR"))
                        done.set()
                    elif (
                        event_kind == "resource_budget"
                        and str(event.get("status") or "") == "denied"
                    ):
                        state["resource_denials"].append(
                            str(event.get("code") or "RESOURCE_DENIED")
                        )
                        done.set()
            except ConnectionClosed as exc:
                if not done.is_set():
                    state["errors"].append(f"WS_CLOSE_{int(exc.code)}")
                    done.set()

        recv_task = asyncio.create_task(receiver())
        await ws.send(json.dumps({"type": "input", "message": {"activity_start": True}}))
        started_at = time.monotonic()
        target = started_at
        frame_bytes = 3200
        checkpoints = []
        checkpoint_values = [30.0, 60.0, 90.0, 180.0]
        next_checkpoint = 0
        for offset in range(0, len(pcm), frame_bytes):
            if done.is_set():
                break
            frame = pcm[offset : offset + frame_bytes]
            seq += 1
            await ws.send(struct.pack("!III", INPUT_MAGIC, seq, 0) + frame)
            target = started_at + min(len(pcm), offset + len(frame)) / 32000.0
            elapsed = time.monotonic() - started_at
            while (
                next_checkpoint < len(checkpoint_values)
                and elapsed >= checkpoint_values[next_checkpoint]
            ):
                checkpoints.append(
                    {
                        "second": checkpoint_values[next_checkpoint],
                        "acks": state["acks"],
                        "errors": list(state["errors"]),
                        "resource_denials": list(state["resource_denials"]),
                    }
                )
                next_checkpoint += 1
            delay = target - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)

        streamed_s = time.monotonic() - started_at
        while next_checkpoint < len(checkpoint_values):
            value = checkpoint_values[next_checkpoint]
            if streamed_s + 0.05 < value:
                break
            checkpoints.append(
                {
                    "second": value,
                    "acks": state["acks"],
                    "errors": list(state["errors"]),
                    "resource_denials": list(state["resource_denials"]),
                }
            )
            next_checkpoint += 1

        if not done.is_set():
            await ws.send(json.dumps({"type": "input", "message": {"activity_end": True}}))
            try:
                await asyncio.wait_for(done.wait(), 45.0)
            except asyncio.TimeoutError:
                state["errors"].append("TURN_COMPLETE_TIMEOUT")

        try:
            await ws.send(json.dumps({"type": "stop", "reason": "ph_voice_v09"}))
        except Exception:
            pass
        recv_task.cancel()
        await asyncio.gather(recv_task, return_exceptions=True)

    return {
        "label": label,
        "session_id": session_id,
        "source_id": source_id,
        "seconds_target": seconds,
        "streamed_s": round(streamed_s, 3),
        "frames_sent": seq,
        "audio_ack_count": state["acks"],
        "max_audio_ack_seq": state["max_ack"],
        "input_transcript_count": state["input_transcripts"],
        "turn_complete_count": state["turn_complete"],
        "tool_calls": state["tool_calls"],
        "errors": state["errors"],
        "resource_denials": state["resource_denials"],
        "checkpoints": checkpoints,
        "event_types": sorted(set(state["event_types"])),
        "ok": bool(
            streamed_s >= seconds - 1.0
            and state["max_ack"] >= max(1, seq - 2)
            and len(checkpoints) == 4
            and state["input_transcripts"] >= 1
            and state["turn_complete"] >= 1
            and state["tool_calls"] == 0
            and not state["errors"]
            and not state["resource_denials"]
        ),
    }


async def async_main(args) -> int:
    seed, fixture_name = await fixture(args)
    suffix = int(time.time())
    users = []
    for label in ("A", "B"):
        http = HttpSession(args.http_base)
        actor_id, workspace_id, conversation_id = login_actor(
            http, f"PH Voice V09 User {label}"
        )
        started, retries = await start_live_with_budget_retry(
            http,
            conversation_id,
            f"ph_voice_v09_{label}_{suffix}",
            timeout=120.0,
        )
        users.append(
            {
                "label": label,
                "http": http,
                "actor_id": actor_id,
                "workspace_id": workspace_id,
                "conversation_id": conversation_id,
                "started": started,
                "start_budget_retries": retries,
            }
        )

    try:
        results = await asyncio.gather(
            *[
                stream_one(
                    args=args,
                    http=item["http"],
                    conversation_id=item["conversation_id"],
                    started=item["started"],
                    seed=seed,
                    seconds=args.seconds,
                    label=item["label"],
                )
                for item in users
            ]
        )

        own_reads = []
        cross_denied = []
        for index, item in enumerate(users):
            source_id = results[index]["source_id"]
            _, own = item["http"].request("GET", f"/api/sources/{source_id}")
            own_reads.append(str((own.get("source") or {}).get("id") or "") == source_id)
            other = users[1 - index]["http"]
            try:
                other.request("GET", f"/api/sources/{source_id}")
                cross_denied.append(False)
            except RuntimeError as exc:
                cross_denied.append(
                    "HTTP 404" in str(exc) or "HTTP 403" in str(exc)
                )

        ok = bool(
            users[0]["actor_id"] != users[1]["actor_id"]
            and users[0]["workspace_id"] != users[1]["workspace_id"]
            and all(item["ok"] for item in results)
            and all(own_reads)
            and all(cross_denied)
        )
        summary = {
            "ok": ok,
            "fixture": fixture_name,
            "seconds": args.seconds,
            "actors_distinct": users[0]["actor_id"] != users[1]["actor_id"],
            "workspaces_distinct": users[0]["workspace_id"] != users[1]["workspace_id"],
            "own_source_reads": own_reads,
            "cross_source_reads_denied": cross_denied,
            "users": [
                {
                    "label": item["label"],
                    "actor_id": item["actor_id"],
                    "workspace_id": item["workspace_id"],
                    "start_budget_retries": item["start_budget_retries"],
                    **results[index],
                }
                for index, item in enumerate(users)
            ],
        }
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return 0 if ok else 2
    finally:
        for item in users:
            try:
                item["http"].request(
                    "POST",
                    f"/api/live/{item['conversation_id']}/sessions/{item['started']['session_id']}/stop",
                    {},
                    timeout=10.0,
                )
            except Exception:
                pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--http-base", default="http://127.0.0.1:8196")
    parser.add_argument("--ws-base")
    parser.add_argument("--origin")
    parser.add_argument("--seconds", type=float, default=180.0)
    args = parser.parse_args()
    if not 30 <= args.seconds <= 300:
        raise SystemExit("--seconds must be 30..300")
    args.ws_base = args.ws_base or _ws_base(args.http_base)
    args.origin = args.origin or _origin(args.http_base)
    return asyncio.run(async_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
