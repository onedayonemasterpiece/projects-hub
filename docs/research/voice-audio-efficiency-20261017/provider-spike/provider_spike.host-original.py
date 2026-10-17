"""Isolated Gemini Live spikes for the voice continuity contract.

No product store, tools or user data: only the shared live-interaction provider
behind ai-resource-control (projects-hub budget, sticky lease per session).
Prints sanitized measurements only; never prints keys, handles or env values.
"""
import argparse
import asyncio
import json
import os
import re
import sys
import time

from live_interaction import provider
from live_interaction.session_host import _QueueReader
from ai_resource_control import run_guarded

UNIT_CAP = 150_000
SESSION_CAP = 6
spent = {"units": 0, "sessions": 0}


def load_env():
    env = {}
    for path in ("/home/dev/.local/state/projects-hub/providers.env", "/home/dev/.local/state/projects-hub/service.env"):
        with open(path) as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                env[key.strip()] = value.strip().strip('"').strip("'")
    from projects_hub.live_resources import live_resource_environment
    return live_resource_environment(env)


class Session:
    def __init__(self, label, configuration, history=None, resume_handle=None):
        if spent["sessions"] >= SESSION_CAP:
            raise SystemExit("session cap reached")
        spent["sessions"] += 1
        self.label = label
        self.reader = _QueueReader()
        self.events = []
        self.handle = None
        self.t0 = time.monotonic()
        start = {"type": "start", "model": "gemini-3.8-live", "configuration": configuration,
                 "context": {}, "history": history or []}
        if resume_handle:
            start["resume_handle"] = resume_handle
        self.reader.feed(start)

    def on_event(self, event):
        kind = event.get("type")
        if kind == "resumption_checkpoint":
            self.handle = event.get("handle")
            event = {"type": kind}
        if kind == "resource_budget" and event.get("status") == "granted":
            spent["units"] += int(event.get("granted_units") or 0)
            if spent["units"] > UNIT_CAP:
                print(json.dumps({"abort": "unit cap exceeded", "units": spent["units"]}), flush=True)
                self.reader.feed({"type": "stop"})
        if kind == "audio":
            event = {"type": "audio", "bytes": len(event.get("data") or "") * 3 // 4}
        event["t"] = round(time.monotonic() - self.t0, 3)
        self.events.append(event)

    async def run(self, env):
        self.task = asyncio.create_task(run_guarded(
            consumer="projects-hub", environment=env, reader=self.reader, on_event=self.on_event,
            provider_run=provider.run, binding=f"spike:voice-continuity-20261016:{self.label}"))

    def dump(self, why):
        safe = [{k: (re.sub(r"AIza\S+", "[R]", str(v))[:160] if k in ("message", "text") else v)
                 for k, v in e.items() if k in ("type", "t", "code", "status", "message", "text", "modality", "granted_units", "input_type", "reason", "bytes")}
                for e in self.events[-40:]]
        print(json.dumps({"label": self.label, "failure": why, "events": safe}, ensure_ascii=False), flush=True)

    async def wait(self, predicate, timeout):
        try:
            await self._wait(predicate, timeout)
        except BaseException as exc:
            self.dump(type(exc).__name__)
            raise

    async def _wait(self, predicate, timeout):
        async with asyncio.timeout(timeout):
            while not predicate(self.events):
                if self.task.done():
                    self.task.result()
                    raise RuntimeError("provider ended")
                await asyncio.sleep(0.02)

    def first(self, kind):
        return next((e for e in self.events if e.get("type") == kind), None)

    async def stop(self):
        self.reader.feed({"type": "stop"})
        try:
            await asyncio.wait_for(self.task, 10)
        except Exception as exc:
            print(json.dumps({"label": self.label, "stop_error": type(exc).__name__}), flush=True)

    def summary(self):
        kinds = {}
        for e in self.events:
            kinds[e.get("type")] = kinds.get(e.get("type"), 0) + 1
        transcript = " ".join(e.get("text", "") for e in self.events if e.get("type") == "input_transcript")
        output = " ".join(e.get("text", "") for e in self.events if e.get("type") == "output_transcript")
        errors = [{"code": e.get("code"), "message": re.sub(r"AIza\S+", "[R]", str(e.get("message", "")))[:160]}
                  for e in self.events if e.get("type") == "error"]
        return {"label": self.label, "kinds": kinds, "input_transcript": transcript[:400],
                "output_transcript": output[:300], "errors": errors, "units_total": spent["units"]}


CONFIG = {"system_instruction": "You are a terse test assistant. Answer in at most five English words.",
          "manual_activity_detection": True, "input_audio_transcription": {"languageCodes": ["en-US"]}}


async def feed_speech(session, pcm, pace):
    chunk = 3200  # 100 ms PCM16/16 kHz
    session.reader.feed({"type": "activity_start"})
    sent_at = time.monotonic()
    import base64
    for offset in range(0, len(pcm), chunk):
        session.reader.feed({"type": "audio", "data": base64.b64encode(pcm[offset:offset + chunk]).decode(),
                             "queued_at": round(time.time() * 1000)})
        if pace > 0:
            await asyncio.sleep(0.1 / pace)
    session.reader.feed({"type": "activity_end"})
    return time.monotonic() - sent_at


async def pacing(env, pcm, pace, label):
    s = Session(label, CONFIG)
    await s.run(env)
    await s.wait(lambda ev: any(e.get("type") == "ready" for e in ev), 40)
    duration = await feed_speech(s, pcm, pace)
    t_end = time.monotonic() - s.t0
    await s.wait(lambda ev: any(e.get("type") == "turn_complete" for e in ev), 90)
    tc = next(e for e in s.events if e.get("type") == "turn_complete")["t"]
    first_in = next((e["t"] for e in s.events if e.get("type") == "input_transcript"), None)
    handle = s.handle
    await s.stop()
    out = s.summary()
    out.update({"pace": pace, "audio_seconds": round(len(pcm) / 32000, 1), "send_seconds": round(duration, 2),
                "activity_end_to_turn_complete_s": round(tc - t_end, 2),
                "first_input_transcript_after_end_s": None if first_in is None else round(first_in - t_end, 2),
                "checkpoint_seen": bool(handle)})
    print(json.dumps(out, ensure_ascii=False), flush=True)
    return handle


async def resume(env, handle, delay):
    await asyncio.sleep(delay)
    s = Session("resume", CONFIG, resume_handle=handle)
    await s.run(env)
    await s.wait(lambda ev: any(e.get("type") in ("ready", "resumption_rejected") for e in ev) and
                 any(e.get("type") == "ready" for e in ev), 40)
    s.reader.feed({"type": "text", "text": "What code word and number did I tell you earlier?"})
    await s.wait(lambda ev: any(e.get("type") == "turn_complete" for e in ev), 60)
    await s.stop()
    out = s.summary()
    ready = s.first("ready") or {}
    out.update({"delay_s": delay, "resumption_mode": ready.get("resumption_mode"),
                "rejected": bool(s.first("resumption_rejected")),
                "recalled_code_word": "penguin" in out["output_transcript"].lower(),
                "recalled_number": "42" in out["output_transcript"] or "forty" in out["output_transcript"].lower()})
    print(json.dumps(out, ensure_ascii=False), flush=True)


async def idle_go_away(env, minutes):
    s = Session("idle", CONFIG)
    await s.run(env)
    await s.wait(lambda ev: any(e.get("type") == "ready" for e in ev), 40)
    try:
        await s.wait(lambda ev: any(e.get("type") == "resumed" for e in ev), minutes * 60)
    except TimeoutError:
        pass
    await s.stop()
    out = s.summary()
    out.update({"go_away_at_s": (s.first("go_away") or {}).get("t"),
                "go_away_time_left": (s.first("go_away") or {}).get("time_left"),
                "resumed_at_s": (s.first("resumed") or {}).get("t"),
                "resumed_mode": (s.first("resumed") or {}).get("resumption_mode")})
    print(json.dumps(out, ensure_ascii=False), flush=True)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["pacing", "idle"])
    parser.add_argument("--pcm", required=True)
    parser.add_argument("--minutes", type=float, default=13)
    args = parser.parse_args()
    env = load_env()
    pcm = open(args.pcm, "rb").read()
    if args.mode == "pacing":
        handle = await pacing(env, pcm, 1, "realtime")
        await pacing(env, pcm, 0, "burst")
        if handle:
            await resume(env, handle, 30)
        else:
            print(json.dumps({"resume": "skipped: no checkpoint observed"}), flush=True)
    else:
        await idle_go_away(env, args.minutes)
    print(json.dumps({"spent": spent}), flush=True)


asyncio.run(main())
