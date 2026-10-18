#!/usr/bin/env python3
"""Isolated emulator acceptance harness around the *production* ASGI app.

Only the external AI/resource runner is replaced. Android still owns AudioRecord,
VAD, MediaCodec, TLS WebSocket v2, its durable queue, decoding and AudioTrack;
the server uses the normal host, PyAV codecs and durable source/timeline stores.
Known signals are checked at the decoded input and recorded speaker boundaries.
This is an audio-path test, not an ASR or real-provider quality measurement.
Never mount these control endpoints in the deployed application.
"""
from __future__ import annotations

import argparse
import asyncio
from array import array
import base64
import importlib.machinery
import json
import math
from pathlib import Path
import secrets
import sys
import time
import types
from urllib.parse import parse_qs
import wave


PHRASES = {
    "hello": "Мира, проверка голосового разговора, слышишь меня?",
    "overlap": "Мира, второй вопрос после восстановления связи",
    "offline": "Эта запись сделана без соединения и должна сохраниться",
    "reply": "Проверка голоса пройдена.",
}
MAX_PCM_BYTES = 16000 * 2 * 90


def read_wav(path: Path) -> tuple[bytes, int]:
    with wave.open(str(path), "rb") as stream:
        if stream.getnchannels() != 1 or stream.getsampwidth() != 2:
            raise ValueError(f"Expected mono PCM16 WAV: {path.name}")
        rate = stream.getframerate()
        # Bound test artifacts, including a failed job that captured too long.
        if stream.getnframes() > rate * 900:
            raise ValueError("Audio evidence exceeds 15 minutes")
        return stream.readframes(stream.getnframes()), rate


def samples(pcm: bytes) -> array:
    result = array("h", pcm[: len(pcm) // 2 * 2])
    if sys.byteorder != "little":
        result.byteswap()
    return result


def envelope(pcm: bytes, rate: int) -> list[float]:
    """10 ms energy preserves the phrase through lossy codecs and resampling."""
    values = samples(pcm)
    width = max(1, rate // 100)
    return [math.sqrt(sum(float(x) * x for x in values[i:i + width]) / width)
            for i in range(0, len(values) - width + 1, width)]


def signal_match(observed: list[float], reference: list[float]) -> dict:
    """Find a whole known phrase, ignoring silence and output start latency.

    Centered correlation cannot be satisfied by nonzero constant noise. The
    acceptance also requires >= 90% of the phrase and an audible signal. This
    deliberately verifies a fixture waveform, not words recognised by a model.
    """
    peak = max(reference, default=0)
    voiced = [i for i, value in enumerate(reference) if value > peak * .06]
    if not voiced or not observed:
        return {"matched": False, "correlation": 0.0, "offset_ms": None}
    reference_start = max(0, voiced[0] - 2)
    reference = reference[reference_start:voiced[-1] + 3]
    # The first/last 5% can be removed by native VAD; interior is still specific
    # to this whole phrase. Do not accept a short generic speech/noise fragment.
    trim = len(reference) // 20
    reference_start += trim
    reference = reference[trim:len(reference) - trim or None]
    n = len(reference)
    if n < 25 or len(observed) < n:
        return {"matched": False, "correlation": 0.0, "offset_ms": None,
                "reference_ms": n * 10, "observed_ms": len(observed) * 10}
    mean = sum(reference) / n
    centered = [v - mean for v in reference]
    ref_power = sum(v * v for v in centered)
    # Running sums keep long speaker recordings bounded in time as well as RAM.
    sums, squares = [0.0], [0.0]
    for value in observed:
        sums.append(sums[-1] + value)
        squares.append(squares[-1] + value * value)
    best, offset, audible = -1.0, 0, 0.0
    for start in range(len(observed) - n + 1):
        total = sums[start + n] - sums[start]
        square = squares[start + n] - squares[start]
        power = max(0.0, square - total * total / n)
        if power < 1 or ref_power < 1:
            continue
        cross = sum(v * observed[start + j] for j, v in enumerate(centered))
        corr = cross / math.sqrt(power * ref_power)
        if corr > best:
            best, offset, audible = corr, start, math.sqrt(square / n)
    return {"matched": best >= .68 and audible >= 50,
            "correlation": round(max(0.0, best), 5), "offset_ms": offset * 10,
            "rms": round(audible, 2), "reference_ms": n * 10,
            "reference_start_ms": reference_start * 10,
            "observed_ms": len(observed) * 10, "minimum_correlation": .68}


def resample(pcm: bytes, rate: int):
    """CI-only numeric evidence; this does not replace either production codec."""
    import av
    import numpy as np
    frame = av.AudioFrame.from_ndarray(np.frombuffer(pcm, dtype="<i2").reshape(1, -1),
                                      format="s16", layout="mono")
    frame.sample_rate = rate
    converter = av.AudioResampler(format="s16", layout="mono", rate=16000)
    frames = converter.resample(frame) + converter.resample(None)
    return np.concatenate([item.to_ndarray().reshape(-1) for item in frames]).astype(np.float64)


def waveform_match(pcm: bytes, rate: int, reference: tuple[bytes, int], match: dict) -> dict:
    """Independent sample correlation rejects other sounds with the same energy.

    First locate the phrase by envelope, then examine a bounded PCM window. The
    small remaining delay covers codec/resampler alignment and is found by FFT.
    NumPy is installed only in the CI harness, not a product dependency.
    """
    if not match["matched"]:
        return {**match, "pcm_correlation": 0.0}
    import numpy as np
    target = resample(*reference)
    start = match["reference_start_ms"] * 16
    count = match["reference_ms"] * 16
    target = target[start:start + count]
    # Slice before converting a potentially long recording: < 15 s per check.
    offset_ms = max(0, match["offset_ms"] - 150)
    end_ms = match["offset_ms"] + match["reference_ms"] + 150
    observed = resample(pcm[offset_ms * rate // 1000 * 2:end_ms * rate // 1000 * 2], rate)
    if len(observed) < len(target):
        return {**match, "matched": False, "pcm_correlation": 0.0}
    target -= target.mean()
    n = len(target)
    size = 1 << (len(observed) + n - 1).bit_length()
    correlation = np.fft.irfft(np.fft.rfft(observed, size) *
                              np.fft.rfft(target[::-1], size), size)[n - 1:len(observed)]
    sums = np.concatenate(([0.0], np.cumsum(observed)))
    squares = np.concatenate(([0.0], np.cumsum(observed * observed)))
    power = np.maximum(1.0, squares[n:] - squares[:-n] - (sums[n:] - sums[:-n]) ** 2 / n)
    norm = np.sqrt(power * max(1.0, float(np.dot(target, target))))
    score = float(np.max(np.abs(correlation) / norm))
    return {**match, "matched": score >= .45, "pcm_correlation": round(score, 5),
            "minimum_pcm_correlation": .45}


class Fixture:
    def __init__(self, data_dir: Path, audio_dir: Path):
        from projects_hub.auth import COOKIE_NAME, issue_session
        from projects_hub.store import DurableStore

        # A fresh, explicit directory is mandatory: never open a user's DB.
        data_dir.mkdir(parents=True, exist_ok=True)
        if list(data_dir.iterdir()):
            raise RuntimeError("Fixture data directory must be empty")
        self.store = DurableStore(data_dir)
        boot = self.store.ensure_dev_workspace("Android voice acceptance fixture")
        actor, workspace = boot["actor"]["id"], boot["workspace"]["id"]
        conversation = self.store.get_or_create_personal_conversation(actor, workspace)
        self.secret = secrets.token_hex(32)
        self.login = {"cookie": f"{COOKIE_NAME}={issue_session(actor, self.secret)}; Path=/; Secure; HttpOnly; SameSite=Lax",
                      "actor_id": actor, "workspace_id": workspace,
                      "conversation_id": conversation["id"]}
        self.audio_dir = audio_dir
        self.signals = {name: read_wav(audio_dir / f"{name}.wav") for name in PHRASES}
        self.references = {name: envelope(*value) for name, value in self.signals.items()}
        reply, rate = self.signals["reply"]
        if rate != 24000:
            raise ValueError("Provider reply fixture must be PCM16 mono 24 kHz")
        self.reply = reply
        self.stats = {"external_provider": "controlled_fixture", "turns": 0,
                      "output_turns": 0, "connection_count": 0, "main_audio_bytes": 0,
                      "main_rms": 0.0, "input_matches": [], "input_match_failures": [],
                      "outages": 0, "speaking": False,
                      "binary_uplink_bytes": 0, "binary_downlink_bytes": 0}
        self.audio_square = 0.0
        self.outage = False
        self.connections: set[asyncio.Event] = set()
        self.processes: set[asyncio.subprocess.Process] = set()
        self.mic = wave.open(str(audio_dir / "decoded-microphone.wav"), "wb")
        self.mic.setparams((1, 2, 16000, 0, "NONE", "not compressed"))

    def state(self) -> dict:
        # Production stores and receipts; no replica of the transport protocol.
        with self.store._lock:
            tables = {row[0] for row in self.store.db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            sources = []
            if "encoded_voice_sources" in tables:
                sources = [dict(row) for row in self.store.db.execute(
                    "SELECT e.source_id,e.contiguous_seq,e.sealed_seq,e.capture_end_us,e.complete,e.played_us,e.output_cancelled,"
                    "s.audio_direction,s.audio_bytes,s.audio_projection_bytes "
                    "FROM encoded_voice_sources e JOIN sources s ON s.id=e.source_id ORDER BY e.source_id")]
            chunks = self.store.db.execute("SELECT COUNT(*) FROM encoded_voice_chunks").fetchone()[0] \
                if "encoded_voice_chunks" in tables else 0
        return {**self.stats, "encoded_sources": sources, "encoded_chunks": chunks,
                "outage": self.outage}

    async def runner(self, *, consumer, environment, reader, on_event, provider_run, binding):
        assert consumer == "projects-hub"
        caption = ":caption:" in binding
        start = json.loads(await reader.readline())
        assert start["type"] == "start" and start["configuration"]["manual_activity_detection"] is True
        on_event({"type": "ready", "model": start["model"]})
        turn = bytearray()
        reply_task = None

        async def respond(text):
            on_event({"type": "input_timing", "activity_end_sent_at": round(time.time() * 1000)})
            on_event({"type": "input_transcript", "text": text})
            if caption:
                return
            on_event({"type": "output_transcript", "text": PHRASES["reply"]})
            # Same raw provider output contract as the managed real provider;
            # the actual shared host encodes it into negotiated downlink v2.
            size = 24000 * 2 * 160 // 1000
            for offset in range(0, len(self.reply), size):
                on_event({"type": "audio", "data": base64.b64encode(self.reply[offset:offset + size]).decode(),
                          "mime_type": "audio/pcm;rate=24000"})
                await asyncio.sleep(.16)
            on_event({"type": "turn_complete"})
            self.stats["output_turns"] += 1

        try:
            while raw := await reader.readline():
                message = json.loads(raw)
                kind = message.get("type")
                if kind == "stop":
                    return
                if kind == "activity_start":
                    turn.clear()
                    if reply_task is not None and not reply_task.done():
                        reply_task.cancel()
                        await asyncio.gather(reply_task, return_exceptions=True)
                        on_event({"type": "interrupted"})
                elif kind == "audio":
                    pcm = base64.b64decode(message["data"], validate=True)
                    if len(turn) + len(pcm) > MAX_PCM_BYTES:
                        raise RuntimeError("Fixture input exceeded bounded 90 second turn")
                    turn.extend(pcm)
                    if not caption:
                        if self.stats["main_audio_bytes"] + len(pcm) > MAX_PCM_BYTES * 3:
                            raise RuntimeError("Fixture exceeded total audio evidence limit")
                        self.mic.writeframesraw(pcm)
                        self.stats["main_audio_bytes"] += len(pcm)
                        self.audio_square += sum(float(v) * v for v in samples(pcm))
                        self.stats["main_rms"] = round(math.sqrt(self.audio_square /
                            max(1, self.stats["main_audio_bytes"] // 2)), 2)
                elif kind == "activity_end" and turn:
                    if not caption:
                        self.stats["turns"] += 1
                    observed = envelope(bytes(turn), 16000)
                    matches = [(name, signal_match(observed, self.references[name]))
                               for name in ("hello", "overlap", "offline")]
                    name, result = max(matches, key=lambda entry: entry[1]["correlation"])
                    result = await asyncio.to_thread(waveform_match, bytes(turn), 16000,
                                                     self.signals[name], result)
                    result = {"name": name, **result}
                    if not caption:
                        key = "input_matches" if result["matched"] else "input_match_failures"
                        self.stats[key].append(result)
                        print(json.dumps({"event": "emulator_signal", **result}), flush=True)
                    turn.clear()
                    if result["matched"]:
                        if reply_task is not None:
                            await asyncio.gather(reply_task, return_exceptions=True)
                        reply_task = asyncio.create_task(respond(PHRASES[name]))
                elif kind == "reconfigure":
                    # Keep the normal capability handshake if the product asks
                    # for one; no external tool effect is simulated here.
                    on_event({"type": "capability_ready", "transition_id": message.get("transition_id"),
                              "capability": message.get("capability")})
        finally:
            if reply_task is not None:
                reply_task.cancel()
                await asyncio.gather(reply_task, return_exceptions=True)

    async def control(self, scope, receive, send):
        from starlette.responses import JSONResponse
        action = scope["path"].removeprefix("/_e2e/")
        method = scope["method"]
        if action == "login" and method == "GET":
            result = self.login
        elif action == "state" and method == "GET":
            result = self.state()
        elif action in {"outage", "restore"} and method == "POST":
            self.outage = action == "outage"
            if self.outage:
                self.stats["outages"] += 1
                for connection in tuple(self.connections):
                    connection.set()
            result = {"outage": self.outage}
        elif action == "speak" and method == "POST":
            name = parse_qs(scope["query_string"].decode()).get("name", [""])[0]
            if name not in {"hello", "overlap", "offline"} or self.stats["speaking"]:
                await JSONResponse({"error": "invalid or overlapping fixture input"}, status_code=409)(scope, receive, send)
                return
            self.stats["speaking"] = True
            process = None
            try:
                process = await asyncio.create_subprocess_exec(
                    "paplay", "--device=ph_ci_mic", str(self.audio_dir / f"{name}.wav"))
                self.processes.add(process)
                async with asyncio.timeout(12):
                    code = await process.wait()
                if code:
                    raise RuntimeError(f"paplay failed: {code}")
            finally:
                if process is not None:
                    if process.returncode is None:
                        process.kill()
                        await process.wait()
                    self.processes.discard(process)
                self.stats["speaking"] = False
            result = {"name": name, "played_into": "ph_ci_mic.monitor"}
        else:
            await JSONResponse({"error": "unknown fixture control"}, status_code=404)(scope, receive, send)
            return
        await JSONResponse(result)(scope, receive, send)

    def wrap(self, app):
        async def application(scope, receive, send):
            if scope["type"] == "http" and scope["path"].startswith("/_e2e/"):
                return await self.control(scope, receive, send)
            if scope["type"] != "websocket":
                return await app(scope, receive, send)
            if self.outage:
                await receive()
                await send({"type": "websocket.close", "code": 1013})
                return
            dropped = asyncio.Event()
            self.connections.add(dropped)
            closed = False

            async def socket_receive():
                nonlocal closed
                incoming = asyncio.create_task(receive())
                outage = asyncio.create_task(dropped.wait())
                try:
                    done, _ = await asyncio.wait((incoming, outage), return_when=asyncio.FIRST_COMPLETED)
                    if outage in done:
                        if not closed:
                            closed = True
                            await send({"type": "websocket.close", "code": 1012})
                        return {"type": "websocket.disconnect", "code": 1012}
                    result = incoming.result()
                    if result.get("bytes"):
                        self.stats["binary_uplink_bytes"] += len(result["bytes"])
                    return result
                finally:
                    for task in (incoming, outage):
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(incoming, outage, return_exceptions=True)

            async def socket_send(message):
                nonlocal closed
                if closed:
                    return
                if message["type"] == "websocket.accept":
                    self.stats["connection_count"] += 1
                if message["type"] == "websocket.close":
                    closed = True
                if message.get("bytes"):
                    self.stats["binary_downlink_bytes"] += len(message["bytes"])
                await send(message)
            try:
                await app(scope, socket_receive, socket_send)
            finally:
                self.connections.discard(dropped)
        return application

    def close(self):
        self.mic.close()
        self.store.close()


def verify_output(args):
    pcm, rate = read_wav(args.verify_output)
    expected, expected_rate = read_wav(args.audio_dir / "reply.wav")
    match = waveform_match(pcm, rate, (expected, expected_rate),
                           signal_match(envelope(pcm, rate), envelope(expected, expected_rate)))
    state = json.loads(args.state.read_text())
    matched_names = {item["name"] for item in state["input_matches"] if item["matched"]}
    checks = {"speaker_waveform_matches_reply": match["matched"],
              "live_input_waveform_matches_hello": "hello" in matched_names,
              "fresh_input_after_reconnect_matches": "overlap" in matched_names,
              "offline_recording_not_executed": "offline" not in matched_names,
              "compressed_sources_persisted": bool(state["encoded_sources"]),
              "binary_compressed_audio_both_directions": state["binary_uplink_bytes"] > 0 and state["binary_downlink_bytes"] > 0,
              "reconnected": state["connection_count"] >= 2,
              "controlled_provider_boundary_explicit": state["external_provider"] == "controlled_fixture"}
    report = {"passed": all(checks.values()), "checks": checks, "speaker_match": match,
              "input_matches": state["input_matches"], "input_match_failures": state["input_match_failures"],
              "claim": "Real emulator microphone/codec/socket/store/playback path with a controlled AI runner; not real-provider ASR acceptance."}
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["passed"]:
        raise SystemExit(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--audio-dir", required=True, type=Path)
    parser.add_argument("--cert", type=Path)
    parser.add_argument("--key", type=Path)
    parser.add_argument("--verify-output", type=Path)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.verify_output:
        if not args.state or not args.report:
            parser.error("--verify-output requires --state and --report")
        return verify_output(args)
    if not args.data_dir or not args.cert or not args.key:
        parser.error("Server requires --data-dir, --cert and --key")
    from projects_hub.app import create_app
    from projects_hub.live_runtime import build_live_host
    from projects_hub.settings import Settings
    import uvicorn

    fixture = Fixture(args.data_dir, args.audio_dir)
    boundary = types.ModuleType("ai_resource_control")
    boundary.__spec__ = importlib.machinery.ModuleSpec("ai_resource_control", loader=None)
    boundary.run_guarded = fixture.runner
    sys.modules["ai_resource_control"] = boundary
    host = build_live_host(fixture.store, environment={})
    settings = Settings(data_dir=args.data_dir, static_dir=Path("web/dist"),
                        session_secret=fixture.secret, cookie_secure=True,
                        dev_auth=False, release_sha="android-emulator-controlled-fixture")
    app = create_app(settings, store=fixture.store, live_host=host)
    try:
        uvicorn.run(fixture.wrap(app), host="0.0.0.0", port=8443,
                    ssl_certfile=str(args.cert), ssl_keyfile=str(args.key),
                    log_level="info", access_log=False)
    finally:
        fixture.close()


if __name__ == "__main__":
    main()
