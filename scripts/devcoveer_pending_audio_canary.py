#!/usr/bin/env python3
"""One real-provider audible-pending check with the actual shared host.

An authorized on-host operator runs the exact candidate Python with its ordinary
resource authority environment. This script does not accept credentials. A new
isolated store/actor/workspace is mandatory. No production user store is opened.

Only completion of the existing read-only github_repositories_list is delayed
(default nine seconds). Its declaration, ACL checks, result, the shared pending
timer and the provider remain unchanged. A normal repositories capability
transition keeps its SILENT/checkpoint contract. The empty isolated catalogue
needs no GitHub request. There is one Live model and no caption model.

This gate proves provider audio while a real tool is pending, not Android
microphone/playback acceptance. The input is a synthetic text request. Output
WAVs contain actual provider PCM; UI text or an emitted tool_progress event alone
cannot pass. No provider call occurs without --run-provider.
"""
from __future__ import annotations

import argparse
import asyncio
from array import array
import base64
from collections import Counter
import hashlib
import importlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import re
import secrets
import sys
import time
import wave

from devcoveer_canary_support import (bounded, cleanup_step, record_error, run_reported,
                                    save_report, use_installed_provider_environment)

TOOL = "github_repositories_list"
PROMPT = ("Проверь, какие репозитории уже подключены к моему рабочему пространству. "
          "Сначала активируй capability repositories через activate_capability, затем вызови "
          "github_repositories_list с пустыми аргументами и сообщи результат. "
          "Только чтение: ничего не подключай и не изменяй.")
# These ordinary prerequisite reads touch only the isolated store/session. A
# same-capability activate_capability is a product-validated no-op; actual router
# transitions remain handled by the unchanged shared resolver/SILENT contract.
SAFE_PREREQUISITES = frozenset({"projects_list_accessible", "runtime_versions_get", "preferences_get", "activate_capability"})
MAX_PCM_BYTES = 4 * 1024 * 1024


class PendingEvidence:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.origin = clock()
        self.call_id = None
        self.entered = self.released = self.returned = None
        self.non_blocking = False
        self.call_count = 0
        self.result_verified = False
        self.turn_complete_after_result = False
        self.errors = []
        self.audio = {"pending": bytearray(), "after_result": bytearray()}
        self.rates = {}
        self.chunks = []
        self.events = Counter()
        self.progress_seq = self.result_seq = None
        self.tool_calls = []
        self.prerequisite_calls = 0

    def tool_diagnostic(self, session, call, phase, code=None):
        if len(self.tool_calls) >= 32:
            if "tool_diagnostic_bound" not in self.errors:
                self.errors.append("tool_diagnostic_bound")
            return
        item = {"phase": phase}
        for key, value in (("name", call.get("name")), ("capability", getattr(session, "capability", None)), ("code", code)):
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", value):
                item[key] = value
        self.tool_calls.append(item)

    def host_events(self, events):
        for event in events:
            if event.get("id") != self.call_id:
                continue
            if event.get("type") == "tool_progress":
                self.progress_seq = event.get("seq")
            elif event.get("type") == "tool_result" and event.get("status") == "ok":
                self.result_seq = event.get("seq")

    def observe(self, session, event):
        self.host_events(session.events)
        kind = str(event.get("type") or "")
        self.events[kind] += 1
        now = self.clock()
        if kind == "tool_call" and isinstance(event.get("calls"), list):
            for call in event["calls"][:16]:
                if isinstance(call, dict):
                    self.tool_diagnostic(session, call, "provider_call")
        if kind in {"error", "resource_fallback"}:
            self.errors.append(kind)
        if kind == "turn_complete" and self.returned is not None and self.result_seq is not None:
            self.turn_complete_after_result = True
        if kind != "audio":
            return
        # Audio before the tool call (including a router acknowledgement) is not
        # evidence of the shared NON_BLOCKING pending path under test.
        phase = ("pending" if self.entered is not None and self.released is None and self.progress_seq is not None
                 else "after_result" if self.returned is not None and self.result_seq is not None else None)
        if phase is None:
            return
        mime = str(event.get("mime_type") or "")
        match = re.fullmatch(r"audio/pcm;rate=(16000|24000|48000)", mime)
        if not match:
            self.errors.append("audio_format")
            return
        try:
            pcm = base64.b64decode(event.get("data") or "", validate=True)
        except (ValueError, TypeError):
            self.errors.append("audio_encoding")
            return
        if len(pcm) % 2 or len(self.audio[phase]) + len(pcm) > MAX_PCM_BYTES:
            self.errors.append("audio_bound")
            return
        rate = int(match[1])
        if self.rates.setdefault(phase, rate) != rate:
            self.errors.append("audio_rate_changed")
            return
        self.audio[phase].extend(pcm)
        if len(self.chunks) < 2048:
            self.chunks.append({"phase": phase, "received_ms": round((now-self.origin)*1000),
                                "pcm_bytes": len(pcm), "rate": rate,
                                "tool_progress_seq": self.progress_seq, "tool_result_seq": self.result_seq})

    def audio_metrics(self, phase):
        raw = self.audio[phase]
        samples = array("h", raw)
        if sys.byteorder != "little":
            samples.byteswap()
        rms = math.sqrt(sum(float(s)*s for s in samples) / len(samples)) if samples else 0
        duration = len(samples) / self.rates.get(phase, 24000)
        return {"duration_ms": round(duration*1000), "rms": round(rms, 3),
                "pcm_bytes": len(raw), "pcm_sha256": hashlib.sha256(raw).hexdigest(),
                "audible_signal": duration >= .2 and rms >= 50}

    def result(self):
        pending, complete = self.audio_metrics("pending"), self.audio_metrics("after_result")
        return {"passed": self.call_count == 1 and self.non_blocking and self.result_verified
                    and pending["audible_signal"] and complete["audible_signal"]
                    and self.turn_complete_after_result and not self.errors,
                "target_tool": TOOL, "target_calls": self.call_count, "declared_non_blocking": self.non_blocking,
                "result_verified": self.result_verified,
                "pending_provider_audio": pending, "completion_provider_audio": complete,
                "turn_complete_after_result": self.turn_complete_after_result,
                "tool_entered_ms": round((self.entered-self.origin)*1000) if self.entered is not None else None,
                "completion_released_ms": round((self.released-self.origin)*1000) if self.released is not None else None,
                "tool_returned_ms": round((self.returned-self.origin)*1000) if self.returned is not None else None,
                "event_counts": dict(self.events), "audio_chunks": self.chunks, "evidence_errors": self.errors[:20],
                "prerequisite_calls": self.prerequisite_calls, "tool_calls": self.tool_calls}

    def save_audio(self, directory):
        for phase, pcm in self.audio.items():
            if not pcm:
                continue
            path = directory / (phase + "-provider.wav")
            with wave.open(str(path), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(self.rates[phase])
                output.writeframes(pcm)
            os.chmod(path, 0o600)


async def execute_read_tool(evidence, original_execute, session, call, delay):
    from projects_hub.store import StoreError

    name = call.get("name")
    evidence.tool_diagnostic(session, call, "entered")
    try:
        if name != TOOL:
            if name not in SAFE_PREREQUISITES or evidence.prerequisite_calls >= 4:
                evidence.errors.append("unexpected_or_excess_prerequisite")
                raise StoreError("CANARY_UNEXPECTED_TOOL", "This isolated canary authorizes catalogue/prerequisite reads only")
            evidence.prerequisite_calls += 1
            result = await original_execute(session, call)
        else:
            if evidence.call_count:
                evidence.errors.append("repeated_target_tool")
                raise StoreError("CANARY_REPEATED_TARGET_TOOL", "Only one delayed catalogue read is authorized")
            evidence.call_count += 1
            evidence.call_id = call.get("id")
            evidence.entered = evidence.clock()
            evidence.non_blocking = TOOL in session.non_blocking_tools
            await asyncio.sleep(delay)
            evidence.released = evidence.clock()
            result = await original_execute(session, call)
            evidence.returned = evidence.clock()
            evidence.result_verified = result == {"repositories": [], "requires_connection": True}
        evidence.tool_diagnostic(session, call, "returned")
        return result
    except BaseException as exc:
        evidence.tool_diagnostic(session, call, "error", getattr(exc, "code", None) or type(exc).__name__)
        raise


async def run(args, report):
    evidence = PendingEvidence()
    store = host = started = resource = actor = None

    async def work():
        nonlocal store, host, started, resource, actor
        from projects_hub.live_adapter import _functions
        from projects_hub.live_resources import ConversationScope
        from projects_hub.live_runtime import build_live_host
        from projects_hub.store import DurableStore
        declaration = next(item for item in _functions() if item["name"] == TOOL)
        if declaration.get("behavior") != "NON_BLOCKING":
            raise ValueError("CANARY_TOOL_NOT_NON_BLOCKING")
        report["versions"] = {name: importlib.metadata.version(name) for name in
                              ("projects-hub", "live-interaction", "ai-resource-control")}
        report["implementation_sha256"] = {name: hashlib.sha256(Path(importlib.import_module(name).__file__).read_bytes()).hexdigest()
            for name in ("projects_hub.live_adapter", "live_interaction.session_host", "live_interaction.provider",
                         "ai_resource_control.client", "ai_resource_control.live")}
        if args.use_installed_provider_environment:
            use_installed_provider_environment()
            report["provider_environment_source"] = "installed_allowlisted_provider_environment"
        args.data_dir.mkdir(parents=True, exist_ok=True)
        if list(args.data_dir.iterdir()):
            raise ValueError("CANARY_DATA_DIR_MUST_BE_EMPTY")
        os.chmod(args.data_dir, 0o700)
        store = DurableStore(args.data_dir)
        boot = store.ensure_dev_workspace("Isolated audible pending canary")
        actor_id, workspace_id = boot["actor"]["id"], boot["workspace"]["id"]
        conversation = store.create_conversation(actor_id, workspace_id)
        actor = {"subject": actor_id, "tenant_id": workspace_id}
        resource = ConversationScope(workspace_id, actor_id, conversation["id"]).resource_binding()
        environment = {k: v for k, v in os.environ.items()
                       if k not in {"AI_RESOURCE_CONTROL_FALLBACK_KEY", "GOOGLE_API_KEY4"}}
        host = build_live_host(store, environment=environment)
        report["shared_progress_delay_ms"] = host.tool_progress_delay_ms
        original_execute, original_event = host.adapter.execute_tool, host.adapter.on_event

        async def delayed_execute(session, call):
            return await execute_read_tool(evidence, original_execute, session, call, args.delay)

        def observe(session, event):
            evidence.observe(session, event)
            return original_event(session, event)

        host.adapter.execute_tool = delayed_execute
        host.adapter.on_event = observe
        report["provider_calls_attempted"] = True
        # Buffered disables the optional caption lease. The same Mira model and
        # ordinary declarations handle this synthetic typed request.
        started = await host.start(resource_id=resource, actor=actor, conversation_id=conversation["id"],
            audio_mode="buffered", attempt_id="pending_canary_"+secrets.token_hex(8))
        await host.input(session_id=started["session_id"], resource_id=resource, actor=actor, message={"text": PROMPT})
        cursor = 0
        while True:
            page = host.events(session_id=started["session_id"], resource_id=resource, actor=actor, after=cursor)
            evidence.host_events(page["events"])
            cursor = page["cursor"]
            if page.get("gap"):
                evidence.errors.append("event_gap")
            if page.get("closed") or evidence.errors:
                break
            if evidence.returned is not None and evidence.turn_complete_after_result:
                break
            await asyncio.sleep(.1)

    try:
        await bounded(work(), args.timeout)
    except BaseException as exc:
        record_error(report, exc)
    finally:
        report.update({key: value for key, value in evidence.result().items() if key != "passed"})
        report["passed"] = False
        save_report(args.report, report)
        deadline = time.monotonic() + 20
        try:
            if host is not None:
                if started:
                    await cleanup_step(host.stop(session_id=started["session_id"], resource_id=resource, actor=actor),
                                       report, "live_stop", min(deadline, time.monotonic()+10))
                await cleanup_step(host.stop_all(), report, "host_stop_all", deadline)
                await cleanup_step(host.close_audio_storage(), report, "audio_storage_close", deadline)
                report["host_sessions_after_close"] = len(host.sessions)
                report["caption_sidecars_after_close"] = len(host._caption_sidecars)
            evidence.save_audio(args.data_dir)
        except BaseException as exc:
            record_error(report, exc, phase="cleanup_or_audio_save")
        finally:
            if store is not None:
                await cleanup_step(asyncio.to_thread(store.close), report, "store_close", deadline)
            result = evidence.result()
            report.update({key: value for key, value in result.items() if key != "passed"})
            report["passed"] = (result["passed"] and not report.get("errors")
                and report.get("host_sessions_after_close") == 0 and report.get("caption_sidecars_after_close") == 0)
            save_report(args.report, report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-provider", action="store_true")
    parser.add_argument("--use-installed-provider-environment", action="store_true",
                        help="Read only installed providers.env using this candidate installer's allowlist")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--delay", type=float, default=9)
    parser.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args(argv)
    if not args.run_provider:
        print(json.dumps({"provider_calls_attempted": False, "target_tool": TOOL,
                          "completion_delay_seconds": args.delay, "required_boundary": "authorized on-host operator and ordinary authority"}))
        return 0
    if not args.data_dir or not args.report or not 8 <= args.delay <= 10 or not 30 <= args.timeout <= 240:
        parser.error("Need new --data-dir/--report, 8–10s delay and bounded timeout")
    if args.report.exists():
        parser.error("Preserve the prior attempt report; select a new path")
    if args.report.resolve().is_relative_to(args.data_dir.resolve()):
        parser.error("Place --report outside the new --data-dir so the store can start empty")
    report = {"passed": False, "provider_calls_attempted": False, "versions": {},
              "input": "one synthetic text request", "artificial_completion_delay_seconds": args.delay,
              "claim": "Actual shared-host NON_BLOCKING tool pending plus real provider audio; separate from Android acceptance",
              "router_contract_changed": False, "caption_model_requested": False}
    run_reported(args.report, report, lambda receipt: run(args, receipt))
    print(json.dumps({key: report[key] for key in ("passed", "claim", "provider_calls_attempted", "versions")}, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
