#!/usr/bin/env python3
"""Real-audio deployed Projects Hub memory canary.

A local deterministic speech synthesizer is used only to create microphone-like
PCM for acceptance. The audio itself goes through the same Projects Hub Live
input, Gemini Live transcription, typed function call, durable source/memory
write and voice response path.

This is not physical-device microphone acceptance.
"""
from __future__ import annotations

from array import array
import argparse
import base64
import http.cookiejar
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
from typing import Any

ESPEAK_CANDIDATES = (Path("/usr/bin/espeak"), Path("/usr/bin/espeak-ng"))
PICO_CANDIDATES = (Path("/usr/bin/pico2wave"), Path("/usr/local/bin/pico2wave"))
FFMPEG_CANDIDATES = (Path("/usr/bin/ffmpeg"), Path("/usr/local/bin/ffmpeg"))

RUSSIAN_PHRASE = (
    "Запомни в проекте Projects Hub тестовую заметку. "
    "Название заметки Голосовая канарейка. "
    "Содержание фиолетовый маяк семьдесят три. "
    "После сохранения коротко подтверди это голосом."
)
ENGLISH_PHRASE = (
    "Remember a test note in the Projects Hub project. "
    "The note title is Voice Canary. "
    "The content is purple beacon seventy three. "
    "After saving it confirm briefly with your voice."
)


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


def _wav_to_pcm16_16k(wav_path: Path) -> bytes:
    with wave.open(str(wav_path), "rb") as source:
        channels = source.getnchannels()
        width = source.getsampwidth()
        rate = source.getframerate()
        frames = source.readframes(source.getnframes())
    if width != 2 or channels not in (1, 2) or rate <= 0:
        raise RuntimeError("unsupported local speech fixture format")

    samples = array("h")
    samples.frombytes(frames)
    if sys.byteorder != "little":
        samples.byteswap()
    if channels == 2:
        mono = array("h")
        for index in range(0, len(samples) - 1, 2):
            mono.append(int((int(samples[index]) + int(samples[index + 1])) / 2))
        samples = mono
    if not samples:
        raise RuntimeError("local speech fixture is empty")

    if rate != 16000:
        output_count = max(1, int(len(samples) * 16000 / rate))
        converted = array("h")
        scale = rate / 16000
        last = len(samples) - 1
        for out_index in range(output_count):
            position = min(last, out_index * scale)
            left = int(position)
            right = min(last, left + 1)
            fraction = position - left
            value = int(samples[left] + (samples[right] - samples[left]) * fraction)
            converted.append(max(-32768, min(32767, value)))
        samples = converted

    silence_prefix = array("h", [0]) * int(16000 * 0.35)
    silence_suffix = array("h", [0]) * int(16000 * 0.8)
    pcm = silence_prefix + samples + silence_suffix
    if sys.byteorder != "little":
        pcm.byteswap()
    return pcm.tobytes()


def _run_fixture(argv: list[str], output: Path, *, timeout: int = 30) -> bool:
    try:
        result = subprocess.run(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and output.is_file() and output.stat().st_size > 256


def _synthesize_pcm16_16k() -> tuple[bytes, str]:
    with tempfile.TemporaryDirectory(prefix="projects-hub-voice-canary-") as tmp:
        root = Path(tmp)

        for executable in ESPEAK_CANDIDATES:
            if not executable.is_file():
                continue
            wav_path = root / "espeak.wav"
            if _run_fixture(
                [
                    str(executable),
                    "-v",
                    "ru",
                    "-s",
                    "135",
                    "-w",
                    str(wav_path),
                    RUSSIAN_PHRASE,
                ],
                wav_path,
            ):
                return _wav_to_pcm16_16k(wav_path), executable.name

        for executable in PICO_CANDIDATES:
            if not executable.is_file():
                continue
            wav_path = root / "pico.wav"
            if _run_fixture(
                [str(executable), "-l=ru-RU", "-w", str(wav_path), RUSSIAN_PHRASE],
                wav_path,
            ):
                return _wav_to_pcm16_16k(wav_path), executable.name

        for executable in FFMPEG_CANDIDATES:
            if not executable.is_file():
                continue
            pcm_path = root / "flite.pcm"
            filter_value = (
                "flite=text='Remember a test note in the Projects Hub project. "
                "The note title is Voice Canary. "
                "The content is purple beacon seventy three. "
                "After saving it confirm briefly with your voice.'"
            )
            if _run_fixture(
                [
                    str(executable),
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    filter_value,
                    "-ar",
                    "16000",
                    "-ac",
                    "1",
                    "-f",
                    "s16le",
                    "-y",
                    str(pcm_path),
                ],
                pcm_path,
            ):
                raw = pcm_path.read_bytes()
                prefix = b"\x00\x00" * int(16000 * 0.35)
                suffix = b"\x00\x00" * int(16000 * 0.8)
                return prefix + raw + suffix, "ffmpeg-flite"

    raise RuntimeError(
        "local speech fixture unavailable: no espeak/espeak-ng/pico2wave/ffmpeg-flite"
    )


def run_canary(base: str, timeout_seconds: float) -> dict[str, Any]:
    pcm, fixture = _synthesize_pcm16_16k()
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    started_at = time.monotonic()

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
    source_id = ""
    cursor = 0
    event_types: set[str] = set()
    tool_results: list[dict[str, Any]] = []
    input_transcript_seen = False
    output_transcript_seen = False
    audio_response_seen = False
    turn_complete_seen = False
    try:
        started = _request(
            opener,
            base,
            "POST",
            f"/api/live/{conversation_id}/sessions",
            {"audio_mode": "buffered"},
            timeout=max(15.0, timeout_seconds),
        )
        session_id = str(started.get("session_id") or "")
        source_id = str(started.get("source_id") or "")
        if not session_id or not source_id:
            raise RuntimeError("Live start did not return session/source ids")

        _request(
            opener,
            base,
            "POST",
            f"/api/live/{conversation_id}/sessions/{session_id}/input",
            {"activity_start": True},
        )

        chunk_bytes = 6400
        for offset in range(0, len(pcm), chunk_bytes):
            chunk = pcm[offset : offset + chunk_bytes]
            _request(
                opener,
                base,
                "POST",
                f"/api/live/{conversation_id}/sessions/{session_id}/input",
                {"audio_base64": base64.b64encode(chunk).decode("ascii")},
            )
            time.sleep(len(chunk) / 32000 * 0.75)
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
            if not isinstance(events, list):
                raise RuntimeError("Live events response is invalid")
            for event in events:
                if not isinstance(event, dict):
                    continue
                kind = str(event.get("type") or "")
                if kind:
                    event_types.add(kind)
                if kind == "input_transcript":
                    input_transcript_seen = True
                elif kind == "output_transcript":
                    output_transcript_seen = True
                elif kind == "audio":
                    audio_response_seen = True
                elif kind == "turn_complete":
                    turn_complete_seen = True
                elif kind == "tool_result":
                    item = {
                        "name": str(event.get("name") or ""),
                        "status": str(event.get("status") or ""),
                        "code": str(event.get("code") or ""),
                        "revision": event.get("revision"),
                    }
                    tool_results.append(item)
                    if (
                        item["name"] == "memory_commit_voice_source"
                        and item["status"] == "ok"
                    ):
                        memory_tool_ok = True
            cursor = int(page.get("cursor") or cursor)
            if (
                memory_tool_ok
                and input_transcript_seen
                and turn_complete_seen
                and (audio_response_seen or output_transcript_seen)
            ):
                break
            time.sleep(0.2)

        source = _request(opener, base, "GET", f"/api/sources/{source_id}").get("source") or {}
        memories = _request(
            opener,
            base,
            "GET",
            "/api/memories?"
            + urllib.parse.urlencode(
                {"workspace_id": workspace["id"], "project_id": project_id, "limit": 20}
            ),
        ).get("items") or []
        saved = next(
            (
                item
                for item in memories
                if isinstance(item, dict) and item.get("source_id") == source_id
            ),
            None,
        )
        durable_source_ok = (
            int(source.get("audio_bytes") or 0) >= len(pcm)
            and int(source.get("audio_chunks") or 0) > 1
            and int(source.get("transcript_revision") or 0) > 0
            and source.get("status") == "archived"
        )
        memory_readback_ok = bool(
            saved
            and saved.get("project_id") == project_id
            and int(saved.get("transcript_revision") or 0) > 0
            and int(saved.get("revision") or 0) > 0
            and saved.get("content_sha256")
        )
        memory_tool_ok = any(
            item["name"] == "memory_commit_voice_source" and item["status"] == "ok"
            for item in tool_results
        )
        ok = (
            input_transcript_seen
            and memory_tool_ok
            and durable_source_ok
            and memory_readback_ok
            and turn_complete_seen
            and (audio_response_seen or output_transcript_seen)
        )
        return {
            "ok": ok,
            "fixture": fixture,
            "real_audio_memory_canary": "pass" if ok else "fail",
            "physical_microphone_acceptance": "not_run",
            "input_transcript_observed": input_transcript_seen,
            "memory_tool_ok": memory_tool_ok,
            "durable_source_readback_ok": durable_source_ok,
            "memory_readback_ok": memory_readback_ok,
            "voice_response_observed": audio_response_seen,
            "output_transcript_observed": output_transcript_seen,
            "turn_complete_observed": turn_complete_seen,
            "audio_bytes": int(source.get("audio_bytes") or 0),
            "audio_chunks": int(source.get("audio_chunks") or 0),
            "transcript_revision": int(source.get("transcript_revision") or 0),
            "memory_revision": int(saved.get("revision") or 0) if saved else 0,
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
    parser.add_argument("--timeout-seconds", type=float, default=70.0)
    args = parser.parse_args()
    result = run_canary(args.base, max(10.0, min(args.timeout_seconds, 150.0)))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
