#!/usr/bin/env python3
"""Synthetic caption clock probe; NEVER Android/native audio pipeline acceptance.

Two known generated WAV phrases run in one real product CaptionSidecar, then
ordinary stop/join closes it and a new sidecar/provider connection repeats them.
The actual TranscribeSession and ordinary run_guarded are unchanged. Provider
generation numbers and raw offsets are preserved separately per instance.
Known multiword markers prove clock attribution only, never whole-phrase ASR
quality. An explicit opt-in may retain bounded synthetic transcript fragments in
this private receipt; it never changes product logs or caption assignment.
No provider call occurs without --run-provider. Native capture is always false.
"""
from __future__ import annotations

import argparse
import asyncio
from array import array
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import secrets
import sys
import time
import wave

from devcoveer_canary_support import (bounded, cleanup_step, record_error, run_reported,
                                    save_report, use_installed_provider_environment)
from devcoveer_native_voice_canary import CaptionEvidence, DEFAULT_PHRASES, implementation_fingerprints

PHRASES = dict(zip(("hello", "offline", "overlap"), DEFAULT_PHRASES))
MARKERS = {"hello": ("Мира проверка", "голосового разговора"),
           "offline": ("эта запись", "сделана без"),
           "overlap": ("Мира второй", "вопрос после")}
MAX_SYNTHETIC_OBSERVATIONS = 128
MAX_SYNTHETIC_TEXT_CHARS = 256
MAX_PROVIDER_LIFECYCLE_EVENTS = 128
PCM_SEND_DRAIN_SECONDS = 10


class SyntheticClockEvidence(CaptionEvidence):
    """Explicit marker→utterance mapping for this generated-audio probe only."""
    def __init__(self, names, *, observe_synthetic_transcripts=False):
        self.names = tuple(names)
        self.marker_utterances = [i for i, name in enumerate(names) for _ in MARKERS[name]]
        super().__init__(tuple(marker for name in names for marker in MARKERS[name]))
        self.observe_synthetic_transcripts = observe_synthetic_transcripts
        self.synthetic_observations = []
        self.synthetic_observation_truncated = False
        self.provider_lifecycle = []
        self.provider_lifecycle_truncated = False

    def observe_provider_event(self, event):
        # CaptionSidecar's diagnostic observer does not forward turn_complete.
        # This probe-only tee records the real shared-provider event unchanged
        # in meaning, with no inferred utterance, generation or finished flag.
        kind = event.get("type")
        if kind not in {"ready", "resumed", "reconnecting", "turn_complete", "error"}:
            return
        if len(self.provider_lifecycle) >= MAX_PROVIDER_LIFECYCLE_EVENTS:
            self.provider_lifecycle_truncated = True
            return
        self.provider_lifecycle.append({"sequence": len(self.provider_lifecycle)+1,
            "type": kind, "audio_sent_events_observed": len(self.sent),
            "timing_probes_observed": len(self.probes), "assigned": False})

    def observe(self, event, transcript_text=None):
        super().observe(event, transcript_text)
        if (not self.observe_synthetic_transcripts or event.get("type") != "caption_timing_probe"
                or not isinstance(transcript_text, str)):
            return
        if len(self.synthetic_observations) >= MAX_SYNTHETIC_OBSERVATIONS:
            self.synthetic_observation_truncated = True
            return
        # Metadata has already passed the base observer's scalar allowlist.
        metadata = self.probes[-1] if self.probes else {}
        observation = {key: metadata[key] for key in ("provider_connection_generation", "transcript_kind",
            "start_offset", "end_offset", "sent_pcm_samples", "observed_finished") if key in metadata}
        observation.update(native_capture=False, synthetic_input_only=True,
            text=transcript_text[:MAX_SYNTHETIC_TEXT_CHARS],
            text_truncated=len(transcript_text) > MAX_SYNTHETIC_TEXT_CHARS,
            assigned=False)
        self.synthetic_observations.append(observation)

    def result(self):
        # Several markers in one prefix are unambiguous only if all belong to
        # the SAME declared synthetic utterance. Cross-utterance accumulated
        # text is never assigned to the current or latest capture span.
        view = CaptionEvidence(self.phrases)
        view.sources, view.sent, view.observer_error = self.sources, self.sent, self.observer_error
        for item in self.probes:
            matches = item.get("known_phrase_matches", [])
            if len({self.marker_utterances[i] for i in matches}) == 1:
                view.probes.extend({**item, "known_phrase_matches": [i]} for i in matches)
        result = view.result()
        result["timing_probes"] = self.probes  # Raw observations, without synthetic duplicate probes.
        result["markers"] = [{"marker_index": i, "utterance_index": utterance,
                              "phrase_name": self.names[utterance], "marker": self.phrases[i]}
                             for i, utterance in enumerate(self.marker_utterances)]
        result["full_transcript_recognition_quality_verified"] = False
        result["final_transcripts_without_offsets"] = sum(
            p.get("transcript_kind") == "input_transcript" and
            ("start_offset" not in p or "end_offset" not in p) for p in self.probes)
        result["synthetic_transcript_observation_enabled"] = self.observe_synthetic_transcripts
        result["provider_lifecycle"] = self.provider_lifecycle
        result["provider_lifecycle_truncated"] = self.provider_lifecycle_truncated
        result["turn_complete_events_observed"] = sum(
            item["type"] == "turn_complete" for item in self.provider_lifecycle)
        if self.observe_synthetic_transcripts:
            result["synthetic_transcript_observations"] = self.synthetic_observations
            result["synthetic_observation_truncated"] = self.synthetic_observation_truncated
        return result

    def utterance_markers_observed(self, utterance_index):
        required = {i for i, utterance in enumerate(self.marker_utterances) if utterance == utterance_index}
        observed = {p["known_phrase_index"] for p in self.result()["mapped_probes"]}
        return required <= observed


def observed_provider_run(provider_run, observer):
    """Forward the ordinary provider callback; observe only bounded lifecycle."""
    async def run(**kwargs):
        forward = kwargs.get("on_event")

        def on_event(event):
            if forward is not None:
                forward(event)
            observer.observe_provider_event(event)

        return await provider_run(**{**kwargs, "on_event": on_event})
    return run


def sent_utterance_samples(observer, scope):
    # This count comes only from actual successful provider writes, and uses
    # their explicit input scope. It never assigns a provider transcript.
    return sum(item["stream_end_sample"]-item["stream_start_sample"] for item in observer.sent
               if item.get("caption_scope") == scope)


async def collect_utterances(sidecar, observer, speech, portions, *, session_id,
                             conversation_id, source_id, connection, emitted_errors,
                             observation_seconds, checkpoint):
    """Collect every planned input independently of ASR/clock gate success."""
    connection["utterance_observations"] = []

    def check_available():
        if emitted_errors or observer.observer_error or not sidecar.available:
            raise ValueError("CANARY_CAPTION_EVIDENCE_UNAVAILABLE")

    for i, item in enumerate(speech):
        scope = {"session_id": session_id, "conversation_id": conversation_id, "source_id": source_id,
                 "utterance_id": f"synthetic_utterance_{i+1}", "turn_id": f"synthetic_turn_{i+1}",
                 "utterance_sequence": i+1}
        observation = {"utterance_index": i, "phrase_name": item["name"],
                       "input_scope": scope, "pcm_samples_expected": len(item["pcm"])//2,
                       "pcm_samples_sent": 0, "collection_complete": False,
                       "observation_seconds": observation_seconds,
                       "arrival_observations_assign_transcripts": False,
                       "timing_probes_before_input": len(observer.probes),
                       "provider_events_before_input": len(observer.provider_lifecycle)}
        connection["utterance_observations"].append(observation)
        check_available()
        if not sidecar.feed({"activity_start": True}):
            raise ValueError("CANARY_CAPTION_INPUT_REJECTED")
        anchor = time.monotonic()
        for offset in range(0, len(item["pcm"]), 3200):
            chunk = item["pcm"][offset:offset+3200]
            due = anchor + (offset+len(chunk))/32000
            await asyncio.sleep(max(0, due-time.monotonic()))
            if not sidecar.feed_pcm(chunk, caption_scope=scope,
                    capture_start_us=portions[i]["capture_start_us"]+offset*1_000_000//32000):
                raise ValueError("CANARY_CAPTION_INPUT_REJECTED")
        if not sidecar.feed({"activity_end": True}):
            raise ValueError("CANARY_CAPTION_INPUT_REJECTED")
        observation["activity_end_queued"] = True
        try:
            async with asyncio.timeout(PCM_SEND_DRAIN_SECONDS):
                while sent_utterance_samples(observer, scope) < observation["pcm_samples_expected"]:
                    check_available()
                    await asyncio.sleep(.1)
            # Fixed diagnostic window after actual PCM send. Failed recognition
            # must not prevent the second utterance or second connection. Even
            # a real turn_complete is an observation, never an utterance ID or
            # permission to invent offsets/finished for a final fragment.
            deadline = time.monotonic()+observation_seconds
            while time.monotonic() < deadline:
                check_available()
                await asyncio.sleep(min(.1, max(0, deadline-time.monotonic())))
            observation["collection_complete"] = True
        finally:
            observation["pcm_samples_sent"] = sent_utterance_samples(observer, scope)
            observation["timing_probes_after_window"] = len(observer.probes)
            observation["provider_events_after_window"] = len(observer.provider_lifecycle)
            observation["markers_mapped_at_window_end"] = observer.utterance_markers_observed(i)
            checkpoint()


def load_speech(directory, names):
    import av
    result = []
    for name in names:
        path = directory / (name + ".wav")
        if path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("CANARY_SYNTHETIC_WAV_BOUND")
        with wave.open(str(path), "rb") as source:
            rate = source.getframerate()
            if source.getnchannels() != 1 or source.getsampwidth() != 2 or not 0 < source.getnframes() <= rate * 12:
                raise ValueError("CANARY_SYNTHETIC_WAV_FORMAT")
            raw = source.readframes(source.getnframes())
        frame = av.AudioFrame(format="s16", layout="mono", samples=len(raw)//2)
        frame.sample_rate = rate
        frame.planes[0].update(raw)
        converter = av.AudioResampler(format="s16", layout="mono", rate=16000)
        frames = converter.resample(frame) + converter.resample(None)
        pcm = b"".join(bytes(item.planes[0])[:item.samples*2] for item in frames)
        values = array("h", pcm)
        if sys.byteorder != "little":
            values.byteswap()
        rms = math.sqrt(sum(float(v)*v for v in values)/len(values)) if values else 0
        if rms < 100:
            raise ValueError("CANARY_SYNTHETIC_WAV_SILENT")
        result.append({"name": name, "phrase": PHRASES[name], "pcm": pcm,
                       "wav_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                       "pcm_sha256": hashlib.sha256(pcm).hexdigest(), "pcm_samples": len(values),
                       "duration_us": len(values)*1_000_000//16000, "rms": round(rms, 3)})
    return result


def connection_result(observer):
    result = observer.result()
    # Native replay's two-source acceptance is deliberately left untouched.
    # This separate probe has one synthetic source and two known utterances.
    result.pop("all_replays_support_candidate_clock", None)
    expected = {(source["replay_id"], i) for source in observer.sources.values() for i in range(len(observer.phrases))}
    matched = {tuple(pair) for pair in result["matched_replays"]}
    generations = sorted({item.get("provider_connection_generation") for item in observer.sent
                          if type(item.get("provider_connection_generation")) is int})
    result["provider_generations_observed"] = generations
    result["unmapped_expected_markers"] = [
        {"replay_id": replay, "marker_index": i,
         "utterance_index": observer.marker_utterances[i]} for replay, i in sorted(expected-matched)]
    result["passed"] = bool(len(observer.sources) == 1 and len(observer.names) == 2
                            and observer.marker_utterances == [0, 0, 1, 1]
                            and not observer.observer_error and matched == expected
                            and len(generations) == 1 and generations[0] > 0)
    return result


async def run(args, speech, report):
    store = sidecar = None

    async def work():
        nonlocal store, sidecar
        from live_interaction.transcribe import run as transcribe_run
        from projects_hub.live_transcription import CaptionSidecar, CAPTION_MODEL
        from projects_hub.live_adapter import _transcription_vocabulary
        from projects_hub.live_resources import ConversationScope, live_resource_environment
        from projects_hub.store import DurableStore
        report["versions"] = {name: importlib.metadata.version(name) for name in
                              ("projects-hub", "live-interaction", "ai-resource-control", "av")}
        report["model"] = CAPTION_MODEL
        report["implementation_sha256"] = implementation_fingerprints()
        if args.use_installed_provider_environment:
            use_installed_provider_environment()
            report["provider_environment_source"] = "installed_allowlisted_provider_environment"
        environment = live_resource_environment({k: v for k, v in os.environ.items()
            if k not in {"AI_RESOURCE_CONTROL_FALLBACK_KEY", "GOOGLE_API_KEY4"}})
        args.data_dir.mkdir(parents=True, exist_ok=True)
        if list(args.data_dir.iterdir()):
            raise ValueError("CANARY_DATA_DIR_MUST_BE_EMPTY")
        os.chmod(args.data_dir, 0o700)
        store = DurableStore(args.data_dir)
        boot = store.ensure_dev_workspace("Isolated synthetic caption clock probe")
        actor_id, workspace_id = boot["actor"]["id"], boot["workspace"]["id"]
        conversation = store.create_conversation(actor_id, workspace_id)
        binding = ConversationScope(workspace_id, actor_id, conversation["id"]).resource_binding()
        vocabulary = _transcription_vocabulary(store.list_projects(actor_id, workspace_id))
        for instance in (1, 2):
            session_id = "caption_probe_" + secrets.token_hex(10)
            source = store.create_source(actor_id, conversation["id"])
            source_id = source["id"]
            observer = SyntheticClockEvidence(tuple(item["name"] for item in speech),
                observe_synthetic_transcripts=args.observe_synthetic_transcripts)
            portions = [{"capture_start_us": 5_000_000+i*15_000_000,
                         "capture_end_us": 5_000_000+i*15_000_000+item["duration_us"]}
                        for i, item in enumerate(speech)]
            observer.sources[source_id] = {"replay_id": f"provider_instance_{instance}",
                "portions": portions, "expected_portions": observer.marker_utterances}
            emitted_errors = []

            def emit(event):
                if event.get("type") == "caption_unavailable" and event.get("code") not in {
                    "CAPTION_CLOCK_UNVERIFIED", "CAPTION_ALIGNMENT_UNAVAILABLE"}:
                    emitted_errors.append("caption_provider_unavailable")

            sidecar = CaptionSidecar(environment=environment, binding=f"{binding}:caption:{session_id}",
                vocabulary=vocabulary, emit=emit,
                provider_run=observed_provider_run(transcribe_run, observer),
                native_offset_clock="unverified")
            sidecar.diagnostic_observer = observer.observe
            report["provider_calls_attempted"] = True
            sidecar.start(native=True)
            connection = {"instance": instance, "native_capture": False, "passed": False}
            report["connections"].append(connection)
            try:
                await collect_utterances(sidecar, observer, speech, portions,
                    session_id=session_id, conversation_id=conversation["id"], source_id=source_id,
                    connection=connection, emitted_errors=emitted_errors,
                    observation_seconds=args.observation_seconds,
                    checkpoint=lambda: save_report(args.report, report))
            finally:
                # Stop the old real provider/lease before constructing a new one.
                try:
                    await bounded(sidecar.stop(), 10)
                finally:
                    connection.update(connection_result(observer))
                    connection["provider_task_joined"] = sidecar.task is not None and sidecar.task.done()
                    connection["all_inputs_collected"] = (
                        len(connection.get("utterance_observations", [])) == len(speech)
                        and all(item["collection_complete"] and item["pcm_samples_sent"] == item["pcm_samples_expected"]
                                for item in connection["utterance_observations"]))
                    connection["passed"] = (connection["passed"] and connection["provider_task_joined"]
                                            and connection["all_inputs_collected"] and not emitted_errors)
                    if connection["provider_task_joined"]:
                        sidecar = None
                    save_report(args.report, report)
        report["passed"] = len(report["connections"]) == 2 and all(c["passed"] for c in report["connections"])

    try:
        await bounded(work(), args.timeout)
    except BaseException as exc:
        record_error(report, exc)
    finally:
        checks_passed = bool(report["passed"])
        report["passed"] = False
        save_report(args.report, report)
        deadline = time.monotonic()+12
        if sidecar is not None:
            await cleanup_step(sidecar.stop(), report, "caption_stop", deadline)
        if store is not None:
            await cleanup_step(asyncio.to_thread(store.close), report, "store_close", deadline)
        report["passed"] = checks_passed and not report.get("errors")
        save_report(args.report, report)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio-dir", type=Path, required=True)
    parser.add_argument("--phrase-name", choices=tuple(PHRASES), action="append")
    parser.add_argument("--run-provider", action="store_true")
    parser.add_argument("--use-installed-provider-environment", action="store_true")
    parser.add_argument("--observe-synthetic-transcripts", action="store_true",
                        help="Generated test input ONLY: retain bounded raw ASR fragments in the private probe receipt")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--timeout", type=float, default=150)
    parser.add_argument("--observation-seconds", type=float, default=5,
                        help="Fixed 1–10s diagnostic window after each successful synthetic PCM send")
    args = parser.parse_args(argv)
    args.phrase_name = args.phrase_name or ["hello", "overlap"]
    if len(args.phrase_name) != 2 or len(set(args.phrase_name)) != 2:
        parser.error("Select exactly two distinct known synthetic phrases")
    if not 1 <= args.observation_seconds <= 10:
        parser.error("--observation-seconds must be bounded to 1–10 seconds")
    speech = load_speech(args.audio_dir, args.phrase_name)
    report = {"passed": False, "native_capture": False, "android_acceptance": False,
              "native_pipeline_acceptance": False, "provider_calls_attempted": False,
              "claim": "Clock-only synthetic PCM marker attribution across two separately closed provider connections; not whole-phrase ASR quality",
              "full_transcript_recognition_quality_verified": False,
              "synthetic_transcript_observation_enabled": args.observe_synthetic_transcripts,
              "observation_schedule": {"after_successful_pcm_send_seconds": args.observation_seconds,
                                       "pcm_send_drain_seconds": PCM_SEND_DRAIN_SECONDS,
                                       "recognition_controls_collection": False},
              "offset_clock": "unverified", "production_clock_configuration_changed": False,
              "versions": {}, "connections": [],
              "inputs": [{k:v for k,v in item.items() if k not in {"pcm", "phrase"}} for item in speech]}
    if not args.run_provider:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if not args.data_dir or not args.report or not 45 <= args.timeout <= 240:
        parser.error("Need fresh --data-dir/--report and a bounded 45–240s timeout")
    if args.report.exists() or args.report.resolve().is_relative_to(args.data_dir.resolve()):
        parser.error("Use a new report path outside the new data directory")
    run_reported(args.report, report, lambda receipt: run(args, speech, receipt))
    print(json.dumps({key: report[key] for key in ("passed", "native_capture", "provider_calls_attempted", "claim")}, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
