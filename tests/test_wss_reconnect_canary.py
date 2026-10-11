"""Offline control-flow checks for the existing deployed WSS reconnect canary."""
import json
import struct
from types import SimpleNamespace

import pytest

from scripts import devcoveer_wss_canary as canary


@pytest.mark.asyncio
@pytest.mark.parametrize("wrong_session", [False, True])
async def test_reconnect_keeps_session_rotates_ticket_and_stops(monkeypatch, wrong_session):
    started = {
        "session_id": "session-fixed",
        "socket_ticket": "old-test-ticket",
        "socket_url": "/same/socket",
        "attempt_id": "test-attempt",
        "transport": "wss",
        "transport_protocol": "wl-live-v1",
    }
    http_calls = []
    sockets = []

    class Closed(Exception):
        pass

    class FakeHttp:
        def __init__(self, base):
            assert base == "http://canary.invalid"

        def cookie_header(self):
            return "test-cookie"

        def request(self, method, path, payload, **kwargs):
            http_calls.append((method, path))
            assert method == "POST"
            if path.endswith("/socket-ticket"):
                assert "/session-fixed/socket-ticket" in path
                return 200, {
                    "session_id": "other-session" if wrong_session else "session-fixed",
                    "socket_ticket": "new-test-ticket",
                    "socket_url": "/same/socket",
                }
            assert path.endswith("/session-fixed/stop")
            return 200, {}

    class FakeSocket:
        def __init__(self, index):
            self.index = index
            self.closed = False
            self.queue = []
            self.stop_seen = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            self.closed = True

        async def close(self, **kwargs):
            assert kwargs["code"] == 1000
            self.closed = True

        async def send(self, raw):
            if isinstance(raw, bytes):
                magic, seq, age = struct.unpack("!III", raw[:12])
                assert (magic, seq, age) == (canary.INPUT_MAGIC, 1, 0)
                assert raw[12:] == b"\x00\x00" * 1600
                self.queue.append(json.dumps({"type": "audio_ack", "seq": seq}))
                return
            message = json.loads(raw)
            if message["type"] == "hello":
                assert message["attempt_id"] == "test-attempt"
                if self.index:
                    assert message["cursor"] == 4
                    assert message["connection_generation"] == 2
                    # Real reconnect may replay events before hello_ack.
                    self.queue.append(json.dumps({"type": "event", "event": {
                        "type": "diagnostic", "seq": 5,
                    }}))
                self.queue.append(json.dumps({"type": "hello_ack"}))
            elif message["type"] == "input" and "text" in message["message"]:
                word = "готово" if self.index == 0 else "связь"
                assert word in message["message"]["text"]
                seq = 1 if self.index == 0 else 6
                # Split output verifies that the marker can span transcript deltas.
                for offset, text in enumerate((word[:1], word[1:])):
                    self.queue.append(json.dumps({"type": "event", "event": {
                        "type": "output_transcript", "text": text, "seq": seq + offset,
                    }}))
                self.queue.extend([
                    struct.pack("!III", 0x574C4F31, seq + 2, 24000) + b"\x00\x00",
                    json.dumps({"type": "event", "event": {
                        "type": "turn_complete", "seq": seq + 3,
                    }}),
                ])
            elif message["type"] == "stop":
                self.stop_seen = True
                self.closed = True
            else:
                assert message["type"] == "input"
                assert message["message"] in ({"activity_start": True}, {"activity_end": True})

        async def recv(self):
            if self.queue:
                return self.queue.pop(0)
            if self.closed:
                raise Closed()
            raise AssertionError("Canary waited without a prepared frame")

    async def open_socket(**kwargs):
        index = len(sockets)
        assert kwargs["started"]["session_id"] == "session-fixed"
        assert kwargs["started"]["socket_ticket"] == (
            "new-test-ticket" if index else "old-test-ticket"
        )
        ws = FakeSocket(index)
        sockets.append(ws)
        return ws

    monkeypatch.setattr(canary, "ConnectionClosed", Closed)
    monkeypatch.setattr(canary, "HttpSession", FakeHttp)
    monkeypatch.setattr(canary, "login_and_conversation", lambda *_: ("workspace", "conversation"))
    monkeypatch.setattr(canary, "start_live", lambda *_: dict(started))
    monkeypatch.setattr(canary, "open_socket", open_socket)
    args = SimpleNamespace(
        http_base="http://canary.invalid", ws_base="ws://canary.invalid",
        origin="http://canary.invalid", display_name="test", timeout=1,
    )

    if wrong_session:
        with pytest.raises(RuntimeError, match="Ticket renewal changed the Live session"):
            await canary.reconnect(args)
        assert len(sockets) == 1
    else:
        result = await canary.reconnect(args)
        assert result["ok"] is True
        assert result["same_session"] is True
        assert result["ticket_rotated"] is True
        assert result["fresh_pcm_ack"] is True
        assert result["fresh_response"] is True
        assert result["server_closed_after_stop"] is True
        assert result["cursor"] == 9
        assert result["physical_microphone_acceptance"] == "not_run"
        assert len(sockets) == 2 and sockets[1].stop_seen
        serialized = json.dumps(result)
        assert "test-ticket" not in serialized and "test-cookie" not in serialized
    assert all(ws.closed for ws in sockets)
    assert http_calls[-1] == (
        "POST", "/api/live/conversation/sessions/session-fixed/stop",
    )
