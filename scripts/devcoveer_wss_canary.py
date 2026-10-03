#!/usr/bin/env python3
"""Deployed Projects Hub WSS acceptance canary.

This is a bounded runtime check, not physical microphone acceptance. It can:
- prove real provider-ready WSS roundtrip including binary PCM/ACK and no HTTP fallback;
- prove same-actor concurrent admission/fairness without creating extra test users.

Public-edge WSS can be checked by bootstrapping through loopback HTTP while
overriding --ws-base/--origin after DNS/TLS is available.
"""
from __future__ import annotations

import argparse
import asyncio
import http.cookiejar
import json
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed

INPUT_MAGIC = 0x574C4131


class HttpSession:
    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float = 45.0,
        expected_error: int | None = None,
    ) -> tuple[int, dict[str, Any]]:
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            urllib.parse.urljoin(self.base + "/", path.lstrip("/")),
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with self.opener.open(request, timeout=timeout) as response:
                value = json.load(response)
                if not isinstance(value, dict):
                    raise RuntimeError(f"{method} {path} returned non-object JSON")
                return response.status, value
        except urllib.error.HTTPError as exc:
            body = exc.read(4096).decode("utf-8", "replace")
            if expected_error is not None and exc.code == expected_error:
                try:
                    value = json.loads(body)
                except ValueError:
                    value = {"raw": body}
                return exc.code, value
            raise RuntimeError(
                f"HTTP {exc.code} for {path}: {body[:500]}"
            ) from None

    def cookie_header(self) -> str:
        return "; ".join(f"{item.name}={item.value}" for item in self.jar)


def _ws_base(http_base: str) -> str:
    parsed = urllib.parse.urlsplit(http_base)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urllib.parse.urlunsplit((scheme, parsed.netloc, "", "", "")).rstrip("/")


def _origin(http_base: str) -> str:
    parsed = urllib.parse.urlsplit(http_base)
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", "")).rstrip("/")


def login_and_conversation(http: HttpSession, display_name: str) -> tuple[str, str]:
    _, login = http.request(
        "POST",
        "/api/dev/login",
        {"display_name": display_name},
    )
    workspace_id = str((login.get("workspace") or {}).get("id") or "")
    if not workspace_id:
        raise RuntimeError("dev login did not return workspace")
    _, conversation = http.request(
        "POST",
        "/api/conversations",
        {"workspace_id": workspace_id, "focus_project_id": None},
    )
    conversation_id = str(conversation.get("id") or "")
    if not conversation_id:
        raise RuntimeError("conversation creation did not return id")
    return workspace_id, conversation_id


def start_live(
    http: HttpSession,
    conversation_id: str,
    attempt_id: str,
) -> dict[str, Any]:
    _, started = http.request(
        "POST",
        f"/api/live/{conversation_id}/sessions",
        {"transport": "wss", "attempt_id": attempt_id},
        timeout=60.0,
    )
    required = {
        "session_id",
        "socket_url",
        "socket_ticket",
        "attempt_id",
        "transport_protocol",
    }
    if required - set(started):
        raise RuntimeError("Live start omitted WSS bootstrap fields")
    return started


async def recv_until(
    ws,
    result: dict[str, Any],
    predicate,
    *,
    timeout: float,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = max(0.1, deadline - time.monotonic())
        raw = await asyncio.wait_for(ws.recv(), min(10.0, remaining))
        if isinstance(raw, bytes):
            result["event_types"].append("audio_binary")
            if predicate("audio_binary", {}):
                return
            continue
        payload = json.loads(raw)
        kind = str(payload.get("type") or "")
        if kind == "event":
            event = payload.get("event") or {}
            event_kind = str(event.get("type") or "")
            if event_kind:
                result["event_types"].append(event_kind)
            if predicate(event_kind, event):
                return
        else:
            if kind:
                result["event_types"].append(kind)
            if predicate(kind, payload):
                return
    raise RuntimeError("WSS condition timed out")


async def open_socket(
    *,
    started: dict[str, Any],
    cookie_header: str,
    ws_base: str,
    origin: str,
):
    return websockets.connect(
        ws_base.rstrip("/") + str(started["socket_url"]),
        origin=origin,
        subprotocols=[
            "wl-live-v1",
            "wl-ticket." + str(started["socket_ticket"]),
        ],
        additional_headers={"Cookie": cookie_header},
        open_timeout=10,
        close_timeout=5,
        max_size=2 * 1024 * 1024,
    )


async def hello(ws, started: dict[str, Any], generation: int = 1) -> None:
    await ws.send(
        json.dumps(
            {
                "type": "hello",
                "protocol": "wl-live-v1",
                "attempt_id": started["attempt_id"],
                "connection_generation": generation,
                "cursor": 0,
            }
        )
    )
    response = json.loads(await asyncio.wait_for(ws.recv(), 5))
    if response.get("type") != "hello_ack":
        raise RuntimeError(f"expected hello_ack, got {response}")


async def wait_server_close(ws) -> bool:
    try:
        while True:
            await asyncio.wait_for(ws.recv(), 5)
    except ConnectionClosed:
        return True


async def roundtrip(args) -> dict[str, Any]:
    http = HttpSession(args.http_base)
    _, conversation_id = login_and_conversation(http, args.display_name)
    started = start_live(http, conversation_id, "deployed_wss_roundtrip")
    result: dict[str, Any] = {
        "ok": False,
        "mode": "roundtrip",
        "session_id": started["session_id"],
        "transport": started.get("transport"),
        "transport_protocol": started.get("transport_protocol"),
        "hello_ack": False,
        "binary_pcm_ack": False,
        "http_fallback_rejected": False,
        "output_transcript": False,
        "binary_output_audio": False,
        "turn_complete": False,
        "server_closed_after_stop": False,
        "event_types": [],
    }
    try:
        async with await open_socket(
            started=started,
            cookie_header=http.cookie_header(),
            ws_base=args.ws_base,
            origin=args.origin,
        ) as ws:
            await hello(ws, started)
            result["hello_ack"] = True

            pcm = b"\x00\x00" * 1600
            await ws.send(struct.pack("!III", INPUT_MAGIC, 1, 0) + pcm)
            await recv_until(
                ws,
                result,
                lambda kind, payload: kind == "audio_ack"
                and payload.get("seq") == 1,
                timeout=10,
            )
            result["binary_pcm_ack"] = True

            status, body = http.request(
                "POST",
                f"/api/live/{conversation_id}/sessions/{started['session_id']}/input",
                {"text": "must_not_fallback"},
                expected_error=409,
            )
            result["http_fallback_rejected"] = (
                status == 409
                and (body.get("detail") or {}).get("code")
                == "LIVE_TRANSPORT_MISMATCH"
            )

            await ws.send(
                json.dumps(
                    {
                        "type": "input",
                        "message": {"audio_stream_end": True},
                    }
                )
            )
            await ws.send(
                json.dumps(
                    {
                        "type": "input",
                        "message": {
                            "text": (
                                "Служебный canary транспорта. Ничего не сохраняй "
                                "и не вызывай инструменты. Ответь одним словом: готово."
                            )
                        },
                    },
                    ensure_ascii=False,
                )
            )

            await recv_until(
                ws,
                result,
                lambda kind, _event: kind == "turn_complete",
                timeout=args.timeout,
            )
            result["turn_complete"] = True
            result["output_transcript"] = "output_transcript" in result["event_types"]
            result["binary_output_audio"] = "audio_binary" in result["event_types"]

            await ws.send(
                json.dumps({"type": "stop", "reason": "deployed_wss_roundtrip"})
            )
            result["server_closed_after_stop"] = await wait_server_close(ws)

        result["ok"] = all(
            (
                result["hello_ack"],
                result["binary_pcm_ack"],
                result["http_fallback_rejected"],
                result["output_transcript"],
                result["binary_output_audio"],
                result["turn_complete"],
                result["server_closed_after_stop"],
                result["transport"] == "wss",
                result["transport_protocol"] == "wl-live-v1",
            )
        )
        return result
    finally:
        try:
            http.request(
                "POST",
                f"/api/live/{conversation_id}/sessions/{started['session_id']}/stop",
                {},
                timeout=10,
            )
        except Exception:
            pass


async def _attach_stop(
    http: HttpSession,
    *,
    conversation_id: str,
    started: dict[str, Any],
    ws_base: str,
    origin: str,
) -> bool:
    _, renewed = http.request(
        "POST",
        f"/api/live/{conversation_id}/sessions/{started['session_id']}/socket-ticket",
        {},
    )
    attached = {**started, **renewed}
    async with await open_socket(
        started=attached,
        cookie_header=http.cookie_header(),
        ws_base=ws_base,
        origin=origin,
    ) as ws:
        await hello(ws, started)
        await ws.send(
            json.dumps({"type": "stop", "reason": "deployed_wss_concurrency"})
        )
        return await wait_server_close(ws)


async def concurrency(args) -> dict[str, Any]:
    http = HttpSession(args.http_base)
    _, first_conversation = login_and_conversation(http, args.display_name)
    conversations = [first_conversation]
    # Keep the same actor; this proves real-provider concurrency plus fairness
    # without creating disposable fake users in production state.
    _, login = http.request(
        "POST",
        "/api/dev/login",
        {"display_name": args.display_name},
    )
    workspace_id = str((login.get("workspace") or {}).get("id") or "")
    for _ in range(2):
        _, conversation = http.request(
            "POST",
            "/api/conversations",
            {"workspace_id": workspace_id, "focus_project_id": None},
        )
        conversations.append(str(conversation["id"]))

    active: list[tuple[str, dict[str, Any]]] = []
    started_at = time.monotonic()
    try:
        for index in range(2):
            active.append(
                (
                    conversations[index],
                    start_live(
                        http,
                        conversations[index],
                        f"deployed_wss_concurrency_{index + 1}",
                    ),
                )
            )

        status, body = http.request(
            "POST",
            f"/api/live/{conversations[2]}/sessions",
            {
                "transport": "wss",
                "attempt_id": "deployed_wss_concurrency_3",
            },
            timeout=10,
            expected_error=429,
        )
        third_rejected = (
            status == 429
            and (body.get("detail") or {}).get("code") == "LIVE_BUSY"
        )

        closes = await asyncio.gather(
            *[
                _attach_stop(
                    http,
                    conversation_id=conversation_id,
                    started=started,
                    ws_base=args.ws_base,
                    origin=args.origin,
                )
                for conversation_id, started in active
            ]
        )
        return {
            "ok": bool(third_rejected and all(closes)),
            "mode": "concurrency",
            "real_provider_sessions_started": len(active),
            "third_session_status": status,
            "third_session_code": (body.get("detail") or {}).get("code"),
            "both_wss_handshakes_and_server_closes": all(closes),
            "elapsed_ms": round((time.monotonic() - started_at) * 1000),
            "scope": "same_actor_real_provider_plus_deterministic_cross_actor_tests",
        }
    finally:
        for conversation_id, started in active:
            try:
                http.request(
                    "POST",
                    f"/api/live/{conversation_id}/sessions/{started['session_id']}/stop",
                    {},
                    timeout=10,
                )
            except Exception:
                pass


async def async_main(args) -> int:
    result = await (roundtrip(args) if args.mode == "roundtrip" else concurrency(args))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("ok") else 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--http-base", default="http://127.0.0.1:8196")
    parser.add_argument("--ws-base")
    parser.add_argument("--origin")
    parser.add_argument("--mode", choices=("roundtrip", "concurrency"), default="roundtrip")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--display-name", default="Pilot user")
    args = parser.parse_args()
    args.ws_base = args.ws_base or _ws_base(args.http_base)
    args.origin = args.origin or _origin(args.http_base)
    return asyncio.run(async_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
