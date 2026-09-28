#!/usr/bin/env python3
"""Real deployed Projects Hub Live/provider/function canary.

This canary deliberately does not pretend to be microphone acceptance. It uses a
server-side text turn only to prove the deployed central Live model -> typed
function -> deterministic backend -> provider continuation path without exposing
provider credentials.
"""
from __future__ import annotations

import argparse
import http.cookiejar
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


def _request(
    opener: urllib.request.OpenerDirector,
    base: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        urllib.parse.urljoin(base.rstrip("/") + "/", path.lstrip("/")),
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            value = json.load(response)
    except urllib.error.HTTPError as exc:
        body = exc.read(4096).decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {exc.code} for {path}: {body[:500]}") from None
    except (OSError, urllib.error.URLError, ValueError) as exc:
        raise RuntimeError(f"{method} {path} failed: {type(exc).__name__}") from None
    if not isinstance(value, dict):
        raise RuntimeError(f"{method} {path} returned non-object JSON")
    return value


def run_canary(base: str, timeout_seconds: float) -> dict[str, Any]:
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    started_at = time.monotonic()

    login = _request(opener, base, "POST", "/api/dev/login", {})
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
        raise RuntimeError("Projects Hub project/workspace is absent from pilot bootstrap")

    conversation = _request(
        opener,
        base,
        "POST",
        "/api/conversations",
        {"workspace_id": workspace["id"], "focus_project_id": None},
    )
    conversation_id = str(conversation.get("id") or "")
    if not conversation_id:
        raise RuntimeError("conversation creation did not return an id")

    session_id = ""
    source_id = ""
    cursor = 0
    event_types: set[str] = set()
    tool_results: list[dict[str, Any]] = []
    output_transcript_seen = False
    audio_seen = False
    turn_complete_seen = False
    try:
        started = _request(
            opener,
            base,
            "POST",
            f"/api/live/{conversation_id}/sessions",
            {},
            timeout=max(10.0, timeout_seconds),
        )
        session_id = str(started.get("session_id") or "")
        source_id = str(started.get("source_id") or "")
        if not session_id or not source_id:
            raise RuntimeError("Live start did not return session/source ids")

        prompt = (
            "Это служебный canary Projects Hub. Не сохраняй эту реплику в память. "
            "Используй conversation_set_focus и установи фокус текущего разговора "
            "на проект с названием Projects Hub из initial application context. "
            "Только после подтвержденного function result коротко ответь голосом, "
            "что фокус установлен."
        )
        _request(
            opener,
            base,
            "POST",
            f"/api/live/{conversation_id}/sessions/{session_id}/input",
            {"text": prompt},
        )

        deadline = time.monotonic() + timeout_seconds
        focus_tool_ok = False
        while time.monotonic() < deadline:
            page = _request(
                opener,
                base,
                "GET",
                f"/api/live/{conversation_id}/sessions/{session_id}/events?after={cursor}",
            )
            events = page.get("events") or []
            if not isinstance(events, list):
                raise RuntimeError("Live events response is invalid")
            for event in events:
                if not isinstance(event, dict):
                    continue
                kind = str(event.get("type") or "")
                if kind:
                    event_types.add(kind)
                if kind == "tool_result":
                    item = {
                        "name": str(event.get("name") or ""),
                        "status": str(event.get("status") or ""),
                        "code": str(event.get("code") or ""),
                    }
                    tool_results.append(item)
                    if (
                        item["name"] == "conversation_set_focus"
                        and item["status"] == "ok"
                    ):
                        focus_tool_ok = True
                elif kind == "output_transcript":
                    output_transcript_seen = True
                elif kind == "audio":
                    audio_seen = True
                elif kind == "turn_complete":
                    turn_complete_seen = True
            cursor = int(page.get("cursor") or cursor)
            if focus_tool_ok and turn_complete_seen and (audio_seen or output_transcript_seen):
                break
            time.sleep(0.2)

        current = _request(
            opener,
            base,
            "GET",
            f"/api/conversations/{conversation_id}",
        )
        readback_ok = (
            current.get("focus_project_id") == target.get("id")
            and current.get("focus_project_name") == "Projects Hub"
        )
        focus_tool_ok = any(
            item["name"] == "conversation_set_focus" and item["status"] == "ok"
            for item in tool_results
        )
        ok = (
            focus_tool_ok
            and readback_ok
            and turn_complete_seen
            and (audio_seen or output_transcript_seen)
        )
        return {
            "ok": ok,
            "provider_function_canary": "pass" if ok else "fail",
            "physical_microphone_acceptance": "not_run",
            "focus_tool_ok": focus_tool_ok,
            "conversation_readback_ok": readback_ok,
            "voice_response_observed": audio_seen,
            "output_transcript_observed": output_transcript_seen,
            "turn_complete_observed": turn_complete_seen,
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
                    timeout=10.0,
                )
            except Exception:
                pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8196")
    parser.add_argument("--timeout-seconds", type=float, default=45.0)
    args = parser.parse_args()
    result = run_canary(args.base, max(5.0, min(args.timeout_seconds, 120.0)))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
