"""Isolated Gemini Live spikes for the voice continuity contract.

No product store, tools or user data: only the shared live-interaction provider
behind ai-resource-control (projects-hub budget, one sticky lease for this run).
Prints sanitized measurements only; never prints keys, handles or env values.
"""
import argparse
import asyncio
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import inspect
import json
import re
import sys
import time
from pathlib import Path

from live_interaction import provider
from live_interaction.session_host import _QueueReader
from ai_resource_control import run_guarded
from ai_resource_control.client import Config, Control, ResourceError

UNIT_CAP = 150_000
SESSION_CAP = 6
EXPECTED_SOURCE_SHA256 = {
    "provider": "22b8aa8d3901d09953bc4378383af8c121b875a81ad13dc7b430e5af7d1d59f5",
    "resource_client": "7bb64af520ffe10593a44dc2deb4c2f16941611398057eecc67c6960f0c608b8",
    "resource_live": "a21d8e366a7670b95c6bb467cdad648f906aeb557d02d1c5ce76fcdefb68867d",
}


class SpikeControl(Control):
    """A test ceiling around SDK RPCs; the SDK remains the only authority client.

    This caps authority reservations, not provider billing. Unknown mutating
    responses retain their full request as an upper bound and stop this run.
    """

    def __init__(self, config, **kwargs):
        super().__init__(replace(config, fallback_key=""), **kwargs)
        self.acquire_attempts = self.connection_attempts = 0
        self.initial_reserved_units = self.additional_reserved_units = 0
        self.unknown_reserved_units = 0
        self.usage_events = 0
        self.observed_input_seen = self.observed_output_seen = False
        self.release_confirmed = False
        self.lease = None
        self._reservation_lock = asyncio.Lock()

    async def acquire(self, *args, **kwargs):
        if self.acquire_attempts:
            raise ResourceError("SPIKE_SECOND_ACQUIRE_FORBIDDEN")
        self.acquire_attempts += 1
        self.lease = await super().acquire(*args, **kwargs)
        return self.lease

    @property
    def reserved_units(self):
        return self.initial_reserved_units + self.additional_reserved_units

    async def rpc(self, operation, payload, **kwargs):
        if operation not in ("acquire", "grant"):
            result = await super().rpc(operation, payload, **kwargs)
            if operation == "release":
                self.release_confirmed = True
            return result
        async with self._reservation_lock:
            requested = payload.get("p_tokens")
            if type(requested) is not int or requested <= 0:
                raise ResourceError("SPIKE_INVALID_RESERVATION_REQUEST")
            if self.unknown_reserved_units:
                raise ResourceError("SPIKE_RESERVATION_UNCERTAIN")
            if self.reserved_units + requested > UNIT_CAP:
                raise ResourceError("SPIKE_AUTHORITY_RESERVATION_CAP")
            self.unknown_reserved_units += requested
            try:
                result = await super().rpc(operation, payload, **kwargs)
            except ResourceError as exc:
                # This code is a definitive refusal in the SDK contract. Its
                # audio right-sizing retry can proceed without counting a grant.
                if exc.code == "RESOURCE_TOKEN_BUDGET":
                    self.unknown_reserved_units -= requested
                raise
            data, _ = result
            granted = data.get("tokens")
            if type(granted) is not int or not 0 <= granted <= requested:
                raise ResourceError("SPIKE_INVALID_RESERVATION_RECEIPT")
            self.unknown_reserved_units -= requested
            if operation == "acquire":
                self.initial_reserved_units += granted
            else:
                self.additional_reserved_units += granted
            return result

    def connection_start(self):
        if self.unknown_reserved_units:
            raise ResourceError("SPIKE_RESERVATION_UNCERTAIN")
        if self.connection_attempts >= SESSION_CAP:
            raise ResourceError("SPIKE_PROVIDER_CONNECTION_CAP")
        self.connection_attempts += 1

    def summary(self):
        lease = self.lease
        return {
            "authority_reservation_cap_units": UNIT_CAP,
            "initial_reserved_units": self.initial_reserved_units,
            "additional_reserved_units": self.additional_reserved_units,
            "authority_reserved_units": self.reserved_units,
            "unknown_reserved_units_upper_bound": self.unknown_reserved_units,
            "authority_accounting_complete": self.unknown_reserved_units == 0,
            "sdk_estimated_admitted_input_units": getattr(lease, "used_estimated_tokens", None),
            "provider_usage_events": self.usage_events,
            "provider_observed_input_tokens": getattr(lease, "observed_input_tokens", None) if self.observed_input_seen else None,
            "provider_observed_output_tokens": getattr(lease, "observed_output_tokens", None) if self.observed_output_seen else None,
            "acquire_attempts": self.acquire_attempts,
            "provider_connection_attempts": self.connection_attempts,
            "provider_connection_cap": SESSION_CAP,
            "authority_lease_release_confirmed": self.release_confirmed,
            "provider_billing_guaranteed": False,
        }


@contextmanager
def bounded_connections(control):
    # Instrument this isolated runner process only, before provider.run imports
    # connect. It counts even rejected-resume retries, before socket creation.
    import websockets
    original = websockets.connect
    def connect(*args, **kwargs):
        control.connection_start()
        return original(*args, **kwargs)
    websockets.connect = connect
    try:
        yield
    finally:
        websockets.connect = original


def source_attribution():
    import ai_resource_control.client as client
    import ai_resource_control.live as live
    result = {"python": sys.version.split()[0], "sources": {}}
    for name, module in (("provider", provider), ("resource_client", client), ("resource_live", live)):
        path = Path(inspect.getsourcefile(module)).resolve()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        result["sources"][name] = {"path": str(path), "sha256": digest,
                                   "matches_expected": digest == EXPECTED_SOURCE_SHA256[name]}
    return result


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
        self.label = label
        self.reader = _QueueReader()
        self.events = []
        self.provider_error_codes = []
        self.handle = None
        self.handle_observed_at_ms = 0
        self.handle_event_index = -1
        self.audio_turn_generation = 0
        self.task = None
        self.runtime = None
        self.t0 = time.monotonic()
        start = {"type": "start", "model": "gemini-3.8-live", "configuration": configuration,
                 "context": {}, "history": history or []}
        if resume_handle:
            start["resume_handle"] = resume_handle
        self.feed(start)

    def feed(self, message, *, generation=None):
        message = dict(message, queued_at=round(time.time() * 1000))
        if generation is not None:
            message["audio_turn_generation"] = generation
        self.reader.feed(message)

    def on_event(self, event):
        kind = event.get("type")
        if kind == "resumption_checkpoint":
            self.handle = event.get("handle")
            self.handle_observed_at_ms = round(time.time() * 1000)
            self.handle_event_index = len(self.events)
            event = {"type": kind}
        elif kind == "resumption_state" and not event.get("resumable"):
            self.handle = None
            self.handle_observed_at_ms = 0
            self.handle_event_index = -1
        if kind == "audio":
            event = {"type": "audio", "bytes": len(event.get("data") or "") * 3 // 4}
        event["t"] = round(time.monotonic() - self.t0, 3)
        event["observed_at_ms"] = round(time.time() * 1000)
        self.events.append(event)

    async def run(self, runtime):
        self.runtime = runtime
        runtime.current = self
        def emit(event):
            # run_guarded suppresses a pre-ready provider error while classifying
            # failover. Retain only its machine code so that failure is visible.
            if event.get("type") == "error":
                self.provider_error_codes.append(event.get("code"))
            runtime.emit(event)
        self.task = asyncio.create_task(provider.run(
            reader=self.reader, on_event=emit, resource_guard=runtime.lease))

    def dump(self, why):
        safe = [{k: v for k, v in e.items() if k in ("type", "t", "code", "status", "modality", "input_type", "bytes", "audio_turn_generation")}
                for e in self.events[-40:]]
        print(json.dumps({"label": self.label, "failure": why, "provider_error_codes": self.provider_error_codes,
                          "events": safe}, ensure_ascii=False), flush=True)

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
        caller = asyncio.current_task()
        cancelling_on_entry = caller.cancelling()
        if self.task is None:
            self.reader.close()
            return
        if cancelling_on_entry and not self.task.done():
            self.task.cancel()
        elif not self.task.done():
            self.feed({"type": "stop"})
        self.reader.close()
        try:
            await asyncio.wait_for(self.task, 10)
        except asyncio.CancelledError:
            if caller.cancelling() > cancelling_on_entry or not self.task.done():
                raise
        except Exception as exc:
            print(json.dumps({"label": self.label, "stop_error": type(exc).__name__}), flush=True)
            raise
        if self.provider_error_codes:
            raise ResourceError("SPIKE_PROVIDER_ERROR")

    def summary(self):
        kinds = {}
        for e in self.events:
            kinds[e.get("type")] = kinds.get(e.get("type"), 0) + 1
        transcript = " ".join(e.get("text", "") for e in self.events if e.get("type") == "input_transcript")
        output = " ".join(e.get("text", "") for e in self.events if e.get("type") == "output_transcript")
        errors = [{"code": e.get("code")}
                  for e in self.events if e.get("type") == "error"]
        return {"label": self.label, "kinds": kinds, "input_transcript": transcript[:400],
                "output_transcript": output[:300], "errors": errors,
                "provider_error_codes": self.provider_error_codes,
                "accounting": self.runtime.control.summary() if self.runtime else {}}


CONFIG = {"system_instruction": "You are a terse test assistant. Answer in at most five English words.",
          "manual_activity_detection": True, "input_audio_transcription": {"languageCodes": ["en-US"]}}


async def feed_speech(session, pcm, pace):
    chunk = 3200  # 100 ms PCM16/16 kHz
    session.audio_turn_generation += 1
    generation = session.audio_turn_generation
    session.feed({"type": "activity_start"}, generation=generation)
    sent_at = time.monotonic()
    import base64
    for offset in range(0, len(pcm), chunk):
        session.feed({"type": "audio", "data": base64.b64encode(pcm[offset:offset + chunk]).decode()},
                     generation=generation)
        if pace > 0:
            await asyncio.sleep(0.1 / pace)
    session.feed({"type": "activity_end"}, generation=generation)
    return time.monotonic() - sent_at


async def pacing(runtime, pcm, pace, label):
    s = Session(label, CONFIG)
    handle = None
    try:
        await s.run(runtime)
        await s.wait(lambda ev: any(e.get("type") == "ready" for e in ev), 40)
        duration = await feed_speech(s, pcm, pace)
        generation = s.audio_turn_generation
        await s.wait(lambda ev: any(e.get("type") == "input_timing" and
                     e.get("audio_turn_generation") == generation and
                     e.get("activity_end_sent_at") for e in ev), 40)
        end_event = next(e for e in s.events if e.get("type") == "input_timing" and
                         e.get("audio_turn_generation") == generation and e.get("activity_end_sent_at"))
        end_index = s.events.index(end_event)
        if any(e.get("type") == "input_dropped" for e in s.events):
            raise RuntimeError("audio turn was dropped")
        if end_event.get("audio_chunks") != (len(pcm) + 3199) // 3200:
            raise RuntimeError("provider audio chunk count mismatch")
        await s.wait(lambda ev: any(e.get("type") == "turn_complete" for e in ev[end_index + 1:]), 90)
        tc = next(e["observed_at_ms"] for e in s.events[end_index + 1:] if e.get("type") == "turn_complete")
        first_in = (s.first("input_transcript") or {}).get("observed_at_ms")
        end_sent_at = end_event["activity_end_sent_at"]
        try:
            await s._wait(lambda ev: s.handle and s.handle_event_index > end_index and
                          s.handle_observed_at_ms >= end_sent_at, 8)
        except TimeoutError:
            pass
        if s.handle_event_index > end_index and s.handle_observed_at_ms >= end_sent_at:
            handle = s.handle
    finally:
        await s.stop()
    out = s.summary()
    out.update({"pace": pace, "audio_seconds": round(len(pcm) / 32000, 1), "send_seconds": round(duration, 2),
                "audio_turn_generation": generation,
                "provider_audio_chunks": end_event["audio_chunks"],
                "activity_end_to_turn_complete_s": round((tc - end_sent_at) / 1000, 2),
                "first_input_transcript_after_end_s": None if first_in is None else round((first_in - end_sent_at) / 1000, 2),
                "checkpoint_after_audio_end_seen": bool(handle)})
    print(json.dumps(out, ensure_ascii=False), flush=True)
    return handle


async def resume(runtime, handle, delay):
    await asyncio.sleep(delay)
    s = Session("resume", CONFIG, resume_handle=handle)
    try:
        await s.run(runtime)
        await s.wait(lambda ev: any(e.get("type") == "ready" for e in ev), 40)
        if (s.first("ready") or {}).get("resumption_mode") == "checkpoint" and not s.first("resumption_rejected"):
            s.feed({"type": "text", "text": "What code word and number did I tell you earlier?"})
            await s.wait(lambda ev: any(e.get("type") == "turn_complete" for e in ev), 60)
    finally:
        await s.stop()
    out = s.summary()
    ready = s.first("ready") or {}
    checkpoint_resumed = ready.get("resumption_mode") == "checkpoint" and not s.first("resumption_rejected")
    out.update({"delay_s": delay, "resumption_mode": ready.get("resumption_mode"),
                "rejected": bool(s.first("resumption_rejected")),
                "checkpoint_resumed": bool(checkpoint_resumed),
                "recalled_code_word": bool(checkpoint_resumed and re.search(r"\bblue\s+penguin\b", out["output_transcript"], re.I)),
                "recalled_number": bool(checkpoint_resumed and re.search(r"\b42\b|\bforty[ -]+two\b", out["output_transcript"], re.I))})
    print(json.dumps(out, ensure_ascii=False), flush=True)


async def idle_go_away(runtime, minutes):
    s = Session("idle", CONFIG)
    try:
        await s.run(runtime)
        await s.wait(lambda ev: any(e.get("type") == "ready" for e in ev), 40)
        try:
            await s.wait(lambda ev: any(e.get("type") == "resumed" for e in ev), minutes * 60)
        except TimeoutError:
            pass
    finally:
        await s.stop()
    out = s.summary()
    out.update({"go_away_at_s": (s.first("go_away") or {}).get("t"),
                "go_away_time_left": (s.first("go_away") or {}).get("time_left"),
                "resumed_at_s": (s.first("resumed") or {}).get("t"),
                "resumed_mode": (s.first("resumed") or {}).get("resumption_mode")})
    print(json.dumps(out, ensure_ascii=False), flush=True)


class SpikeRun:
    """The original three scenarios, all inside one guarded SDK conversation."""

    def __init__(self, control, mode, pcm, minutes):
        self.control, self.mode, self.pcm, self.minutes = control, mode, pcm, minutes
        self.lease = self.current = self.emit = None
        self.completed = False
        self.errors = []

    def on_event(self, event):
        if event.get("type") == "error":
            self.errors.append(event.get("code"))
        elif event.get("type") == "usage":
            self.control.usage_events += 1
            metadata = event.get("metadata") or {}
            for source, flag in (("promptTokenCount", "observed_input_seen"),
                                 ("responseTokenCount", "observed_output_seen")):
                value = metadata.get(source)
                if type(value) is int and 0 <= value < 2**53:
                    setattr(self.control, flag, True)
        if self.current is not None:
            self.current.on_event(event)

    async def provider_run(self, *, load_key, reader, on_event, resource_guard):
        # The SDK has already acquired and activated exactly this lease. Never
        # re-acquire between realtime/burst/resume/idle or export key material.
        if resource_guard is not self.control.lease:
            raise ResourceError("SPIKE_MANAGED_LEASE_REQUIRED")
        if any(type(getattr(resource_guard, field, None)) is not int for field in
               ("used_estimated_tokens", "observed_input_tokens", "observed_output_tokens")):
            raise ResourceError("SPIKE_ACCOUNTING_UNAVAILABLE")
        self.lease, self.emit = resource_guard, on_event
        await reader.readline()  # SDK's prepended start; each original Session owns its input queue.
        if self.mode in ("pacing", "all"):
            handle = await pacing(self, self.pcm, 1, "realtime")
            await pacing(self, self.pcm, 0, "burst")
            if handle:
                await resume(self, handle, 30)
            else:
                print(json.dumps({"resume": "skipped: no fresh checkpoint after audio end"}), flush=True)
        if self.mode in ("idle", "all"):
            await idle_go_away(self, self.minutes)
        self.completed = True


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["pacing", "idle", "all"])
    parser.add_argument("--pcm", required=True)
    parser.add_argument("--minutes", type=float, default=13)
    args = parser.parse_args()
    if not 0 < args.minutes <= 13:
        parser.error("--minutes must be above zero and at most 13")
    attribution = source_attribution()
    print(json.dumps({"source_attribution": attribution}), flush=True)
    if not all(item["matches_expected"] for item in attribution["sources"].values()):
        print(json.dumps({"abort": "SPIKE_SOURCE_MISMATCH"}), flush=True)
        return 2
    env = load_env()
    pcm = Path(args.pcm).read_bytes()
    if not pcm or len(pcm) % 2 or len(pcm) > 15 * 32000:
        parser.error("--pcm must be nonempty mono PCM16/16 kHz, at most 15 seconds")
    control = SpikeControl(Config.from_env("projects-hub", env))
    runtime = SpikeRun(control, args.mode, pcm, args.minutes)
    reader = _QueueReader()
    reader.feed({"type": "start", "model": "gemini-3.8-live"})
    reader.close()
    failure = None
    try:
        with bounded_connections(control):
            async with asyncio.timeout(18 * 60):
                await run_guarded(
                    consumer="projects-hub", environment=env, reader=reader,
                    on_event=runtime.on_event, provider_run=runtime.provider_run,
                    control=control, binding="spike:voice-continuity-20261016")
    except (Exception, asyncio.CancelledError) as exc:
        failure = type(exc).__name__
    finally:
        await control.close()
        print(json.dumps({"spike_completed": runtime.completed, "failure": failure,
                          "errors": runtime.errors, "accounting": control.summary()}), flush=True)
    return 0 if runtime.completed and not runtime.errors and failure is None else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

