#!/usr/bin/env python3
"""Real-audio acceptance for one mixed utterance targeting two projects."""
from __future__ import annotations

import argparse
import base64
import http.cookiejar
import json
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any

FFMPEG_CANDIDATES = (Path("/usr/bin/ffmpeg"), Path("/usr/local/bin/ffmpeg"))
PHRASE = (
    "Remember two separate project requirements from this one recording. "
    "For Projects Hub, save a requirement titled Offline status badge: "
    "show offline recording state in the voice island. "
    "For Wonderful Lections, save a requirement titled Speaker timer: "
    "show a quiet speaker timer during presentation. "
    "Save each requirement only in its named project. Do not merge them. "
    "After both confirmed function results, briefly confirm both by voice."
)


def request_json(
    opener: urllib.request.OpenerDirector,
    base: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    timeout: float = 20.0,
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
    if not isinstance(value, dict):
        raise RuntimeError(f"{method} {path} returned non-object JSON")
    return value


def synthesize() -> tuple[bytes, str]:
    for executable in FFMPEG_CANDIDATES:
        if not executable.is_file():
            continue
        with tempfile.TemporaryDirectory(prefix="projects-hub-multiproject-") as temp:
            output = Path(temp) / "voice.pcm"
            result = subprocess.run(
                [
                    str(executable),
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "flite=text=" + repr(PHRASE),
                    "-ar",
                    "16000",
                    "-ac",
                    "1",
                    "-f",
                    "s16le",
                    "-y",
                    str(output),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
                check=False,
            )
            if result.returncode == 0 and output.is_file() and output.stat().st_size > 1000:
                raw = output.read_bytes()
                prefix = b"\x00\x00" * int(16000 * 0.35)
                suffix = b"\x00\x00" * int(16000 * 0.8)
                return prefix + raw + suffix, "ffmpeg-flite"
    raise RuntimeError("local ffmpeg-flite speech fixture unavailable")


def run(base: str, timeout_seconds: float) -> dict[str, Any]:
    pcm, fixture = synthesize()
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    started_at = time.monotonic()

    login = request_json(
        opener,
        base,
        "POST",
        "/api/dev/login",
        {"display_name": "Projects Hub multi-project canary"},
    )
    workspace = login["workspace"]["id"]
    projects = {item["name"]: item["id"] for item in login["projects"]}
    required = {"Projects Hub", "Wonderful Lections"}
    if not required.issubset(projects):
        raise RuntimeError("required projects are absent from canary workspace")

    conversation = request_json(
        opener,
        base,
        "POST",
        "/api/conversations",
        {"workspace_id": workspace, "focus_project_id": None},
    )
    conversation_id = str(conversation["id"])
    client_source_id = "local_" + uuid.uuid4().hex
    session_id = ""
    cursor = 0
    tool_results: list[dict[str, Any]] = []
    event_types: set[str] = set()
    flags = {
        "input_transcript": False,
        "audio": False,
        "output_transcript": False,
        "turn_complete": False,
    }

    try:
        started = request_json(
            opener,
            base,
            "POST",
            f"/api/live/{conversation_id}/sessions",
            {"audio_mode": "buffered", "client_source_id": client_source_id},
            timeout=max(20.0, timeout_seconds),
        )
        session_id = str(started.get("session_id") or "")
        source_id = str(started.get("source_id") or "")
        if not session_id or not source_id:
            raise RuntimeError("Live start did not return session/source ids")

        request_json(
            opener,
            base,
            "POST",
            f"/api/live/{conversation_id}/sessions/{session_id}/input",
            {"activity_start": True},
        )
        for offset in range(0, len(pcm), 6400):
            chunk = pcm[offset : offset + 6400]
            request_json(
                opener,
                base,
                "POST",
                f"/api/live/{conversation_id}/sessions/{session_id}/input",
                {"audio_base64": base64.b64encode(chunk).decode("ascii")},
            )
            time.sleep(len(chunk) / 32000 * 0.55)
        request_json(
            opener,
            base,
            "POST",
            f"/api/live/{conversation_id}/sessions/{session_id}/input",
            {"activity_end": True},
        )

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            page = request_json(
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
                if kind in flags:
                    flags[kind] = True
                if kind == "tool_result":
                    tool_results.append(
                        {
                            "name": str(event.get("name") or ""),
                            "status": str(event.get("status") or ""),
                            "project_id": event.get("project_id"),
                            "memory_id": event.get("memory_id"),
                        }
                    )
            cursor = int(page.get("cursor") or cursor)
            memory_ok = [
                item
                for item in tool_results
                if item["name"] == "memory_commit_voice_source" and item["status"] == "ok"
            ]
            if (
                len(memory_ok) >= 2
                and flags["turn_complete"]
                and (flags["audio"] or flags["output_transcript"])
            ):
                break
            time.sleep(0.2)

        hub_items = request_json(
            opener,
            base,
            "GET",
            "/api/memories?"
            + urllib.parse.urlencode(
                {
                    "workspace_id": workspace,
                    "project_id": projects["Projects Hub"],
                    "limit": 20,
                }
            ),
        ).get("items") or []
        lections_items = request_json(
            opener,
            base,
            "GET",
            "/api/memories?"
            + urllib.parse.urlencode(
                {
                    "workspace_id": workspace,
                    "project_id": projects["Wonderful Lections"],
                    "limit": 20,
                }
            ),
        ).get("items") or []
        hub_for_source = [item for item in hub_items if item.get("source_id") == source_id]
        lections_for_source = [item for item in lections_items if item.get("source_id") == source_id]
        all_ids = {
            item.get("id")
            for item in hub_for_source + lections_for_source
            if item.get("id")
        }
        two_projects_ok = (
            len(hub_for_source) == 1
            and len(lections_for_source) == 1
            and len(all_ids) == 2
        )
        tools_ok = len(
            [
                item
                for item in tool_results
                if item["name"] == "memory_commit_voice_source" and item["status"] == "ok"
            ]
        ) >= 2
        ok = (
            flags["input_transcript"]
            and tools_ok
            and two_projects_ok
            and flags["turn_complete"]
            and (flags["audio"] or flags["output_transcript"])
        )
        result = {
            "ok": ok,
            "multi_project_real_audio_canary": "pass" if ok else "fail",
            "fixture": fixture,
            "source_id": source_id,
            "input_transcript_observed": flags["input_transcript"],
            "memory_tool_results_ok": tools_ok,
            "projects_with_memory_for_source": sorted(
                [
                    name
                    for name, items in (
                        ("Projects Hub", hub_for_source),
                        ("Wonderful Lections", lections_for_source),
                    )
                    if items
                ]
            ),
            "memory_objects_for_source": len(all_ids),
            "voice_response_observed": flags["audio"],
            "output_transcript_observed": flags["output_transcript"],
            "turn_complete_observed": flags["turn_complete"],
            "physical_microphone_acceptance": "not_run",
            "event_types": sorted(event_types),
            "tool_results": tool_results[-10:],
            "elapsed_ms": round((time.monotonic() - started_at) * 1000),
        }
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if ok else 1
    finally:
        if session_id:
            try:
                request_json(
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
    parser.add_argument("--timeout-seconds", type=float, default=80.0)
    args = parser.parse_args()
    raise SystemExit(run(args.base, max(20.0, min(args.timeout_seconds, 150.0))))


if __name__ == "__main__":
    main()
