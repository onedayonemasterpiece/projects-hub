#!/usr/bin/env python3
"""Real-provider PH-VOICE V07: recover PCM, then recall it in a fresh Live session."""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import time
import uuid

from devcoveer_voice_long_input_canary import (
    start_live_with_budget_retry,
    synthesize_from_provider,
    synthesize_local,
)
from devcoveer_wss_canary import (
    HttpSession,
    _origin,
    _ws_base,
    hello,
    login_and_conversation,
    open_socket,
)


async def fixture(args):
    local=synthesize_local()
    if local is not None:
        return local
    return await synthesize_from_provider(args)


async def start_recovery(http, conversation_id, client_source_id, timeout=120.0):
    deadline=time.monotonic()+timeout
    retries=0
    while True:
        try:
            _, value=http.request(
                "POST",
                f"/api/live/{conversation_id}/sessions",
                {
                    "transport":"wss",
                    "audio_mode":"buffered",
                    "recovery_only":True,
                    "client_source_id":client_source_id,
                    "attempt_id":f"ph_v07_recovery_{retries}_{int(time.time())}",
                },
                timeout=60.0,
            )
            return value,retries
        except RuntimeError as exc:
            if "RESOURCE_TOKEN_BUDGET" not in str(exc) or time.monotonic()>=deadline:
                raise
            retries+=1
            await asyncio.sleep(min(5.0,max(.2,deadline-time.monotonic())))


async def main_async(args):
    pcm,fixture_name,markers=await fixture(args)
    http=HttpSession(args.http_base)
    _,conversation_id=login_and_conversation(http,"PH Voice V07 Recovery")
    client_source_id="local_"+uuid.uuid4().hex
    recovery,recovery_retries=await start_recovery(http,conversation_id,client_source_id)
    recovery_session=str(recovery["session_id"])
    source_id=str(recovery["source_id"])
    chunk_bytes=6400
    cursor=0
    recovery_input_transcript=False
    recovery_turn_complete=False
    recovery_tool_calls=[]
    try:
        http.request(
            "POST",
            f"/api/live/{conversation_id}/sessions/{recovery_session}/input",
            {"activity_start":True},
        )
        for offset in range(0,len(pcm),chunk_bytes):
            chunk=pcm[offset:offset+chunk_bytes]
            http.request(
                "POST",
                f"/api/live/{conversation_id}/sessions/{recovery_session}/input",
                {"audio_base64":base64.b64encode(chunk).decode("ascii")},
            )
            await asyncio.sleep(len(chunk)/32000*.35)
        http.request(
            "POST",
            f"/api/live/{conversation_id}/sessions/{recovery_session}/input",
            {"activity_end":True},
        )
        deadline=time.monotonic()+90
        while time.monotonic()<deadline:
            _,page=http.request(
                "GET",
                f"/api/live/{conversation_id}/sessions/{recovery_session}/events?after={cursor}",
                timeout=20,
            )
            for event in page.get("events") or []:
                kind=str(event.get("type") or "")
                if kind=="input_transcript":
                    recovery_input_transcript=True
                elif kind=="tool_call":
                    recovery_tool_calls.append(str(event.get("name") or ""))
                elif kind=="turn_complete":
                    recovery_turn_complete=True
            cursor=int(page.get("cursor") or cursor)
            if recovery_input_transcript and recovery_turn_complete:
                break
            await asyncio.sleep(.2)
    finally:
        try:
            http.request(
                "POST",
                f"/api/live/{conversation_id}/sessions/{recovery_session}/stop",
                {},
                timeout=10,
            )
        except Exception:
            pass

    _,source=http.request("GET",f"/api/sources/{source_id}")
    source_summary=source.get("source") or {}
    if not (
        recovery_input_transcript
        and recovery_turn_complete
        and not recovery_tool_calls
        and int(source_summary.get("transcript_revision") or 0)>0
        and source_summary.get("status")=="agent_disposition_pending"
    ):
        raise RuntimeError("recovery-only session did not produce a safe pending source")

    normal,normal_retries=await start_live_with_budget_retry(
        http,conversation_id,f"ph_v07_recall_{int(time.time())}"
    )
    normal_session=str(normal["session_id"])
    output=[]
    tool_calls=[]
    tool_results=[]
    errors=[]
    audio_after_read=0
    read_result_seen=False
    turn_complete_count=0
    turn_complete=False
    try:
        async with await open_socket(
            started=normal,
            cookie_header=http.cookie_header(),
            ws_base=args.ws_base,
            origin=args.origin,
        ) as ws:
            await hello(ws,normal)
            await ws.send(json.dumps({
                "type":"input",
                "message":{
                    "text":(
                        "There is a pending voice source from my previous interrupted utterance. "
                        "Use voice_source_read to read it. Do not call any mutation tool. "
                        "Answer only with the three color words associated with the beacon, "
                        "the compass, and the bridge, in that order, copied from the source "
                        "and separated by vertical bars."
                    )
                },
            }))
            deadline=time.monotonic()+90
            while time.monotonic()<deadline and not turn_complete:
                raw=await asyncio.wait_for(ws.recv(),min(10,deadline-time.monotonic()))
                if isinstance(raw,bytes):
                    if read_result_seen:
                        audio_after_read+=1
                    continue
                payload=json.loads(raw)
                if payload.get("type")!="event":
                    continue
                event=payload.get("event") or {}
                kind=str(event.get("type") or "")
                if kind=="tool_call":
                    for call in event.get("calls") or []:
                        name=str(call.get("name") or "")
                        if name:
                            tool_calls.append(name)
                elif kind=="tool_result":
                    item={
                        "name":str(event.get("name") or ""),
                        "status":str(event.get("status") or ""),
                    }
                    tool_results.append(item)
                    if item["name"]=="voice_source_read" and item["status"]=="ok":
                        read_result_seen=True
                elif kind=="output_transcript":
                    text=str(event.get("text") or "")
                    if read_result_seen and text:
                        output.append(text)
                elif kind=="error":
                    errors.append(str(event.get("code") or "LIVE_ERROR"))
                elif kind=="turn_complete":
                    turn_complete_count+=1
                    if read_result_seen and output:
                        turn_complete=True
            try:
                await ws.send(json.dumps({"type":"stop","reason":"ph_voice_v07_done"}))
            except Exception:
                pass
    finally:
        try:
            http.request(
                "POST",
                f"/api/live/{conversation_id}/sessions/{normal_session}/stop",
                {},
                timeout=10,
            )
        except Exception:
            pass

    answer=" ".join(output).lower()
    marker_hits=[marker.lower() in answer for marker in markers]
    mutation_calls=[
        name for name in tool_calls
        if name not in {"voice_source_read","runtime_versions_get","capabilities_list"}
    ]
    ok=bool(
        recovery_input_transcript
        and recovery_turn_complete
        and not recovery_tool_calls
        and "voice_source_read" in tool_calls
        and not mutation_calls
        and turn_complete
        and not errors
        and all(marker_hits)
    )
    result={
        "ok":ok,
        "fixture":fixture_name,
        "source_id":source_id,
        "recovery_budget_retries":recovery_retries,
        "normal_budget_retries":normal_retries,
        "recovery_input_transcript":recovery_input_transcript,
        "recovery_turn_complete":recovery_turn_complete,
        "recovery_tool_calls":recovery_tool_calls,
        "source_status":source_summary.get("status"),
        "source_transcript_revision":source_summary.get("transcript_revision"),
        "fresh_session_tool_calls":tool_calls,
        "fresh_session_tool_results":tool_results,
        "read_result_seen":read_result_seen,
        "mutation_calls":mutation_calls,
        "marker_hits":marker_hits,
        "answer_chars":len(answer),
        "audio_after_read":audio_after_read,
        "turn_complete_count":turn_complete_count,
        "turn_complete":turn_complete,
        "errors":errors,
        "physical_microphone_acceptance":"not_run",
    }
    print(json.dumps(result,ensure_ascii=False,sort_keys=True))
    return 0 if ok else 2


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--http-base",default="http://127.0.0.1:8196")
    parser.add_argument("--ws-base")
    parser.add_argument("--origin")
    args=parser.parse_args()
    args.ws_base=args.ws_base or _ws_base(args.http_base)
    args.origin=args.origin or _origin(args.http_base)
    return asyncio.run(main_async(args))


if __name__=="__main__":
    raise SystemExit(main())