#!/usr/bin/env python3
"""Real provider canary for offline buffered source recovery.

The first attempt intentionally stops after only part of the durable PCM source.
The second attempt uses the same stable client_source_id, replays the whole source,
and must end with one terminal server source and one memory object.
"""
from __future__ import annotations

import argparse
import base64
import http.cookiejar
import json
import time
import urllib.parse
import urllib.request
import uuid
from typing import Any

from devcoveer_voice_memory_canary import _request, _synthesize_pcm16_16k


def run_canary(base: str, timeout_seconds: float) -> dict[str, Any]:
    pcm, fixture = _synthesize_pcm16_16k()
    client_source_id = "local_" + uuid.uuid4().hex
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    started_at = time.monotonic()

    login = _request(
        opener,
        base,
        "POST",
        "/api/dev/login",
        {"display_name": "Projects Hub offline canary"},
    )
    workspace = login.get("workspace") or {}
    projects = login.get("projects") or []
    target = next(
        (
            project
            for project in projects
            if isinstance(project, dict) and project.get("name") == "Projects Hub"
        ),
        None,
    )
    if not target or not workspace.get("id"):
        raise RuntimeError("Projects Hub project/workspace is absent from canary bootstrap")
    project_id = str(target["id"])
    conversation = _request(
        opener,
        base,
        "POST",
        "/api/conversations",
        {"workspace_id": workspace["id"], "focus_project_id": project_id},
    )
    conversation_id = str(conversation.get("id") or "")
    if not conversation_id:
        raise RuntimeError("conversation creation did not return an id")

    chunk_bytes = 6400
    chunks = [pcm[offset : offset + chunk_bytes] for offset in range(0, len(pcm), chunk_bytes)]
    if len(chunks) < 4:
        raise RuntimeError("speech fixture is too small for retry canary")

    first_session = ""
    first_source = ""
    try:
        started = _request(
            opener,
            base,
            "POST",
            f"/api/live/{conversation_id}/sessions",
            {"audio_mode": "buffered", "client_source_id": client_source_id},
            timeout=max(15.0, timeout_seconds),
        )
        first_session = str(started.get("session_id") or "")
        first_source = str(started.get("source_id") or "")
        if not first_session or not first_source:
            raise RuntimeError("first Live start did not return session/source ids")
        _request(
            opener,
            base,
            "POST",
            f"/api/live/{conversation_id}/sessions/{first_session}/input",
            {"activity_start": True},
        )
        partial_count = max(2, len(chunks) // 4)
        for chunk in chunks[:partial_count]:
            _request(
                opener,
                base,
                "POST",
                f"/api/live/{conversation_id}/sessions/{first_session}/input",
                {"audio_base64": base64.b64encode(chunk).decode("ascii")},
            )
        _request(
            opener,
            base,
            "POST",
            f"/api/live/{conversation_id}/sessions/{first_session}/stop",
            {},
        )
        first_session = ""
    finally:
        if first_session:
            try:
                _request(
                    opener,
                    base,
                    "POST",
                    f"/api/live/{conversation_id}/sessions/{first_session}/stop",
                    {},
                )
            except Exception:
                pass

    partial = _request(
        opener,
        base,
        "GET",
        f"/api/conversations/{conversation_id}/sources/by-client/{client_source_id}",
    ).get("source") or {}
    partial_ok = (
        partial.get("id") == first_source
        and 0 < int(partial.get("audio_bytes") or 0) < len(pcm)
        and partial.get("status") in {"local_durable", "capturing", "agent_disposition_pending"}
    )

    session_id = ""
    cursor = 0
    tool_results: list[dict[str, Any]] = []
    event_types: set[str] = set()
    input_transcript_seen = False
    audio_seen = False
    output_seen = False
    turn_complete_seen = False
    try:
        restarted = _request(
            opener,
            base,
            "POST",
            f"/api/live/{conversation_id}/sessions",
            {"audio_mode": "buffered", "client_source_id": client_source_id},
            timeout=max(15.0, timeout_seconds),
        )
        session_id = str(restarted.get("session_id") or "")
        second_source = str(restarted.get("source_id") or "")
        reused_ok = (
            second_source == first_source
            and restarted.get("source_reused") is True
            and restarted.get("source_terminal") is False
        )
        if not session_id or not reused_ok:
            raise RuntimeError("retry did not reuse the incomplete source")

        reset = _request(
            opener,
            base,
            "GET",
            f"/api/conversations/{conversation_id}/sources/by-client/{client_source_id}",
        ).get("source") or {}
        reset_ok = (
            reset.get("id") == first_source
            and int(reset.get("audio_bytes") or 0) == 0
            and int(reset.get("audio_chunks") or 0) == 0
            and int(reset.get("transcript_revision") or 0) == 0
            and reset.get("status") == "capturing"
        )

        _request(
            opener,
            base,
            "POST",
            f"/api/live/{conversation_id}/sessions/{session_id}/input",
            {"activity_start": True},
        )
        for chunk in chunks:
            _request(
                opener,
                base,
                "POST",
                f"/api/live/{conversation_id}/sessions/{session_id}/input",
                {"audio_base64": base64.b64encode(chunk).decode("ascii")},
            )
            time.sleep(len(chunk) / 32000 * 0.55)
        _request(
            opener,
            base,
            "POST",
            f"/api/live/{conversation_id}/sessions/{session_id}/input",
            {"activity_end": True},
        )

        deadline = time.monotonic() + timeout_seconds
        memory_tool_ok = False
        while time.monotonic() < deadline:
            page = _request(
                opener,
                base,
                "GET",
                f"/api/live/{conversation_id}/sessions/{session_id}/events?after={cursor}",
            )
            events = page.get("events") or []
            for event in events if isinstance(events, list) else []:
                if not isinstance(event, dict):
                    continue
                kind = str(event.get("type") or "")
                if kind:
                    event_types.add(kind)
                if kind == "input_transcript":
                    input_transcript_seen = True
                elif kind == "audio":
                    audio_seen = True
                elif kind == "output_transcript":
                    output_seen = True
                elif kind == "turn_complete":
                    turn_complete_seen = True
                elif kind == "tool_result":
                    item = {
                        "name": str(event.get("name") or ""),
                        "status": str(event.get("status") or ""),
                        "code": str(event.get("code") or ""),
                    }
                    tool_results.append(item)
                    if item["name"] == "memory_commit_voice_source" and item["status"] == "ok":
                        memory_tool_ok = True
            cursor = int(page.get("cursor") or cursor)
            if memory_tool_ok and turn_complete_seen and (audio_seen or output_seen):
                break
            time.sleep(0.2)

        final_source = _request(
            opener,
            base,
            "GET",
            f"/api/conversations/{conversation_id}/sources/by-client/{client_source_id}",
        ).get("source") or {}
        memories = _request(
            opener,
            base,
            "GET",
            "/api/memories?"
            + urllib.parse.urlencode(
                {"workspace_id": workspace["id"], "project_id": project_id, "limit": 20}
            ),
        ).get("items") or []
        source_memories = [
            item
            for item in memories
            if isinstance(item, dict) and item.get("source_id") == first_source
        ]
        terminal_ok = (
            final_source.get("status") == "archived"
            and int(final_source.get("audio_bytes") or 0) == len(pcm)
            and int(final_source.get("transcript_revision") or 0) > 0
        )
        exactly_one_memory = len(source_memories) == 1
        ok = (
            partial_ok
            and reused_ok
            and reset_ok
            and input_transcript_seen
            and memory_tool_ok
            and terminal_ok
            and exactly_one_memory
            and turn_complete_seen
            and (audio_seen or output_seen)
        )
        return {
            "ok": ok,
            "offline_retry_canary": "pass" if ok else "fail",
            "fixture": fixture,
            "client_source_id": client_source_id,
            "server_source_reused": reused_ok,
            "partial_attempt_persisted": partial_ok,
            "incomplete_server_copy_reset": reset_ok,
            "input_transcript_observed": input_transcript_seen,
            "memory_tool_ok": memory_tool_ok,
            "terminal_source_ok": terminal_ok,
            "memory_objects_for_source": len(source_memories),
            "voice_response_observed": audio_seen,
            "output_transcript_observed": output_seen,
            "turn_complete_observed": turn_complete_seen,
            "physical_microphone_acceptance": "not_run",
            "event_types": sorted(event_types),
            "tool_results": tool_results[-10:],
            "elapsed_ms": round((time.monotonic() - started_at) * 1000),
        }
    finally:
        if session_id:
            try:
                _request(
                    opener,
                    base,
                    "POST",
                    f"/api/live/{conversation_id}/sessions/{session_id}/stop",
                    {},
                )
            except Exception:
                pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8196")
    parser.add_argument("--timeout-seconds", type=float, default=70.0)
    args = parser.parse_args()
    result = run_canary(args.base, max(15.0, min(args.timeout_seconds, 150.0)))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
