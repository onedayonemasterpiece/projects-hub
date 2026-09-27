#!/usr/bin/env python3
"""Verify durable Projects Hub memory can be recalled after a service restart.

Run this only after the real-audio memory canary has created at least one memory
for the dedicated "Projects Hub canary" pilot actor. The script does not create
or replay audio. It proves a new Live session can read the already durable
project memory and answer, while the memory set remains unchanged.
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
    timeout: float = 15.0,
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


def _memory_signature(items: list[dict[str, Any]]) -> list[tuple[str, str, int, str]]:
    signature = []
    for item in items:
        if not isinstance(item, dict):
            continue
        signature.append(
            (
                str(item.get("id") or ""),
                str(item.get("source_id") or ""),
                int(item.get("revision") or 0),
                str(item.get("content_sha256") or ""),
            )
        )
    return sorted(signature)


def run_canary(base: str, timeout_seconds: float) -> dict[str, Any]:
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    started_at = time.monotonic()

    health = _request(opener, base, "GET", "/healthz")
    login = _request(
        opener,
        base,
        "POST",
        "/api/dev/login",
        {"display_name": "Projects Hub canary"},
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

    before = _request(
        opener,
        base,
        "GET",
        "/api/memories?"
        + urllib.parse.urlencode(
            {"workspace_id": workspace["id"], "project_id": project_id, "limit": 20}
        ),
    ).get("items") or []
    if not isinstance(before, list) or not before:
        raise RuntimeError("restart recall requires an existing canary memory")
    before_signature = _memory_signature(before)
    if not before_signature or any(not row[0] or not row[1] or row[2] <= 0 or not row[3] for row in before_signature):
        raise RuntimeError("existing canary memory has incomplete readback metadata")

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

    session_id = ""
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
            {"audio_mode": "realtime"},
            timeout=max(15.0, timeout_seconds),
        )
        session_id = str(started.get("session_id") or "")
        if not session_id:
            raise RuntimeError("Live start did not return a session id")

        _request(
            opener,
            base,
            "POST",
            f"/api/live/{conversation_id}/sessions/{session_id}/input",
            {
                "text": (
                    "Это проверка восстановления после рестарта. "
                    "Не сохраняй новую память и не проси повторять исходную речь. "
                    "Обязательно вызови memory_read_project для текущего проекта, "
                    "прочитай уже сохранённую тестовую заметку и затем коротко "
                    "подтверди голосом, что память доступна после рестарта."
                )
            },
        )

        deadline = time.monotonic() + timeout_seconds
        read_tool_ok = False
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
                    if item["name"] == "memory_read_project" and item["status"] == "ok":
                        read_tool_ok = True
                elif kind == "output_transcript":
                    output_transcript_seen = True
                elif kind == "audio":
                    audio_seen = True
                elif kind == "turn_complete":
                    turn_complete_seen = True
            cursor = int(page.get("cursor") or cursor)
            if read_tool_ok and turn_complete_seen and (audio_seen or output_transcript_seen):
                break
            time.sleep(0.2)

        after = _request(
            opener,
            base,
            "GET",
            "/api/memories?"
            + urllib.parse.urlencode(
                {"workspace_id": workspace["id"], "project_id": project_id, "limit": 20}
            ),
        ).get("items") or []
        after_signature = _memory_signature(after if isinstance(after, list) else [])
        read_tool_ok = any(
            item["name"] == "memory_read_project" and item["status"] == "ok"
            for item in tool_results
        )
        memory_unchanged = after_signature == before_signature
        ok = (
            health.get("ok") is True
            and read_tool_ok
            and memory_unchanged
            and turn_complete_seen
            and (audio_seen or output_transcript_seen)
        )
        return {
            "ok": ok,
            "service_restart_recall_canary": "pass" if ok else "fail",
            "release_sha": str(health.get("release_sha") or ""),
            "memory_count_before": len(before_signature),
            "memory_count_after": len(after_signature),
            "memory_set_unchanged": memory_unchanged,
            "memory_read_tool_ok": read_tool_ok,
            "voice_response_observed": audio_seen,
            "output_transcript_observed": output_transcript_seen,
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
                    timeout=10.0,
                )
            except Exception:
                pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8196")
    parser.add_argument("--timeout-seconds", type=float, default=45.0)
    args = parser.parse_args()
    result = run_canary(args.base, max(10.0, min(args.timeout_seconds, 120.0)))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
