from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_pwa_offline_capture_reuses_shared_live_interaction():
    app = (ROOT / "web/src/App.tsx").read_text(encoding="utf-8")
    store = (ROOT / "web/src/offlineSources.ts").read_text(encoding="utf-8")
    replay = (ROOT / "web/src/bufferedReplay.ts").read_text(encoding="utf-8")

    assert "createDurableMicrophoneCapture" in app
    assert "createLiveClient" in replay
    assert 'audio_mode: "buffered"' in replay
    assert "activity_start" in replay
    assert "activity_end" in replay

    product_audio = app + store + replay
    for forbidden in (
        "getUserMedia(",
        "new AudioContext",
        "createScriptProcessor(",
        "WebSocket(",
        "webrtc_vad",
    ):
        assert forbidden not in product_audio


def test_local_queue_is_durable_and_server_cleanup_requires_terminal_receipt():
    store = (ROOT / "web/src/offlineSources.ts").read_text(encoding="utf-8")
    replay = (ROOT / "web/src/bufferedReplay.ts").read_text(encoding="utf-8")

    assert "indexedDB.open" in store
    assert "crypto.subtle.digest" in store
    assert "recoverInterruptedVoiceSources" in store
    assert 'new Set(["archived", "ephemeral_processed"])' in replay
    assert "acknowledgeDeliveredSource" in replay
    assert "server_receipt_missing" in replay
