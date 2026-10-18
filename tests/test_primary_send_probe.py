"""Real transparent proxy logic with a controlled socket, no provider/network."""
import asyncio
import base64
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def probe(monkeypatch):
    path = Path(__file__).parents[1] / "scripts/devcoveer_primary_send_probe.py"
    monkeypatch.syspath_prepend(str(path.parent))
    spec = importlib.util.spec_from_file_location("primary_send_probe_tested", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_proxy_records_only_completed_sends_and_preserves_payloads(probe):
    entered, release = asyncio.Event(), asyncio.Event()
    sent, connections = [], []
    endpoint = "wss://provider.invalid/primary"
    url = endpoint + "?key=synthetic-secret-key"
    pcm = b"\x10\x01" * 320
    start = json.dumps({"realtimeInput": {"activityStart": {}}})
    audio = json.dumps({"realtimeInput": {"audio": {"mimeType": "audio/pcm;rate=16000",
                                                  "data": base64.b64encode(pcm).decode()}}})
    end = json.dumps({"realtimeInput": {"activityEnd": {}}})
    setup = json.dumps({"setup": {"systemInstruction": "private-setup-body"}})

    class Inner:
        fail = False
        async def send(self, message, *args, **kwargs):
            sent.append((message, args, kwargs))
            if self.fail:
                raise RuntimeError(url + " private-setup-body " + audio)
            if message is audio:
                entered.set()
                await release.wait()
            return "unchanged-return-value"

        async def recv(self):
            return "unchanged-receive"

    inner = Inner()

    class Context:
        closed = False
        async def __aenter__(self):
            return inner
        async def __aexit__(self, *args):
            self.closed = True
            return False

    context = Context()
    def factory(*args, **kwargs):
        connections.append((args, kwargs))
        return context

    audit = probe.ProviderSendAudit()
    connect = audit.connect_factory(factory, endpoint)
    async with connect(url, open_timeout=15, max_size=1024) as ws:
        assert await ws.recv() == "unchanged-receive"
        await ws.send(setup)
        await ws.send(start)
        pending = asyncio.create_task(ws.send(audio, text=True))
        await asyncio.wait_for(entered.wait(), 1)
        assert audit.result()["activities"][0]["packets"] == 0
        release.set()
        assert await pending == "unchanged-return-value"
        inner.fail = True
        with pytest.raises(RuntimeError):
            await ws.send(audio)
        inner.fail = False
        await ws.send(end)
    result = audit.result()
    activity = result["activities"][0]
    assert activity["packets"] == 1 and activity["samples"] == 320 and activity["pcm_bytes"] == 640
    assert activity["pcm_sha256"] == hashlib.sha256(pcm).hexdigest()
    assert activity["pcm_chunk_hashes_sha256"] == hashlib.sha256(hashlib.sha256(pcm).hexdigest().encode()).hexdigest()
    assert activity["activity_start_send_completed_ms"] <= activity["first_audio_send_completed_ms"] <= activity["activity_end_send_completed_ms"]
    assert activity["first_audio_send_completed_ms"] == activity["last_audio_send_completed_ms"]
    assert result["provider_connection_count"] == activity["provider_connection_index"] == 1
    assert result["failed_write_count"] == 1 and result["valid"] is False
    assert result["invalid_reasons"] == ["write_not_completed"]
    assert connections == [((url,), {"open_timeout": 15, "max_size": 1024})]
    assert [item[0] for item in sent] == [setup, start, audio, audio, end]
    assert sent[2][2] == {"text": True}
    assert context.closed is True
    encoded = json.dumps(result)
    assert all(value not in encoded for value in (url, "synthetic-secret-key", "private-setup-body", base64.b64encode(pcm).decode()))


def test_loopback_and_other_endpoint_are_identity_passthrough(probe):
    audit = probe.ProviderSendAudit()
    original_result = object()
    connect = audit.connect_factory(lambda *args, **kwargs: original_result, "wss://provider.invalid/primary")
    assert connect("ws://127.0.0.1:1234/native", subprotocols=["wl-live-v2"]) is original_result
    assert connect("wss://provider.invalid/primary-other?key=private") is original_result
    assert audit.result()["provider_connection_count"] == 0


def test_audit_is_bounded_and_marks_invalid_order_without_promoting_it(probe):
    audit = probe.ProviderSendAudit()
    start = json.dumps({"realtimeInput": {"activityStart": {}}})
    end = json.dumps({"realtimeInput": {"activityEnd": {}}})
    for _ in range(9):
        audit.sent(1, start)
        audit.sent(1, end)
    result = audit.result()
    assert len(result["activities"]) == 8
    assert result["valid"] is False
    assert set(result["invalid_reasons"]) == {"empty_activity", "activity_bound", "activity_end_without_start"}


def test_caption_observer_cannot_be_enabled_by_primary_probe(probe):
    with pytest.raises(SystemExit, match="second ASR lease"):
        probe.main(["--qualify-captions"])


@pytest.mark.parametrize("flag", ["--qualify", "--qualify-caption"])
def test_parsed_caption_abbreviation_is_refused_before_provider_work(tmp_path, probe, monkeypatch, flag):
    import sqlite3
    from test_native_voice_canary import original_db
    from live_interaction.audio_v2 import PORTION_START, PORTION_END
    evidence_dir = tmp_path / "smoke-evidence"
    store_dir = evidence_dir / "fixture-data"
    store_dir.mkdir(parents=True)
    original, _ = original_db(store_dir, probe.canary)
    with sqlite3.connect(original) as db:
        db.execute("UPDATE encoded_voice_chunks SET flags=flags|? WHERE chunk_seq=1", (PORTION_END,))
        db.execute("UPDATE encoded_voice_chunks SET flags=flags|? WHERE chunk_seq=2", (PORTION_START,))
    (evidence_dir / "audio-verification.json").write_text('{"passed":true}')
    flow = tmp_path / "voice-flow.json"
    flow.write_text('{"passed":true,"conversation_id":"conversation_original"}')
    report_path = tmp_path / "report.json"
    provider_calls = []

    async def forbidden_provider(*args):
        provider_calls.append(args)
        raise AssertionError("Provider work must not begin")

    monkeypatch.setattr(probe.canary, "run_provider", forbidden_provider)
    assert probe.main(["--fixture-evidence", str(evidence_dir), "--voice-flow", str(flow),
        "--source", "device_original", "--phrase", "первая проверка", "--phrase", "вторая проверка",
        "--data-dir", str(tmp_path / "fresh-data"), "--report", str(report_path), "--run-provider", flag]) == 1
    result = json.loads(report_path.read_text())
    assert provider_calls == []
    assert probe.canary.run_provider is forbidden_provider
    assert result["provider_calls_attempted"] is False
    assert result["primary_passed"] is False and result["passed"] is False
    assert any(item.get("error_code") == "CANARY_PRIMARY_SEND_PROBE_CAPTIONS_UNSUPPORTED" for item in result["errors"])
