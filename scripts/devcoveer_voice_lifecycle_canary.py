#!/usr/bin/env python3
"""Real-provider pause / playback / barge-in acceptance for PH-VOICE V05.

This validates transport and same-session microphone lifecycle with prepared PCM.
It does not claim physical acoustic echo acceptance; that remains part of V11.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import struct
import time
from typing import Any

from websockets.exceptions import ConnectionClosed

from devcoveer_voice_long_input_canary import (
    repeated_pcm,
    start_live_with_budget_retry,
    synthesize_from_provider,
    synthesize_local,
)
from devcoveer_wss_canary import (
    INPUT_MAGIC,
    HttpSession,
    _origin,
    _ws_base,
    hello,
    login_and_conversation,
    open_socket,
)


async def fixture(args) -> tuple[bytes, str]:
    local=synthesize_local()
    if local is not None:
        pcm,name,_markers=local
        return pcm,name
    pcm,name,_markers=await synthesize_from_provider(args)
    return pcm,name


async def run(args) -> dict[str, Any]:
    seed,fixture_name=await fixture(args)
    pcm=repeated_pcm(seed,args.turn_seconds)
    http=HttpSession(args.http_base)
    _,conversation_id=login_and_conversation(http,args.display_name)
    started,start_retries=await start_live_with_budget_retry(
        http,conversation_id,f"ph_voice_lifecycle_{int(time.time())}"
    )
    session_id=str(started["session_id"])
    state={
        "acks":0,
        "max_ack":0,
        "input_transcripts":[],
        "output_transcripts":[],
        "audio_binary":0,
        "turn_complete":0,
        "interrupted":0,
        "tool_calls":[],
        "errors":[],
        "resource_denials":[],
        "event_types":[],
    }
    queue: asyncio.Queue[dict[str, Any]]=asyncio.Queue()
    seq=0

    async def receiver(ws):
        try:
            while True:
                raw=await ws.recv()
                now=time.monotonic()
                if isinstance(raw,bytes):
                    state["audio_binary"]+=1
                    state["event_types"].append("audio_binary")
                    await queue.put({"kind":"audio_binary","at":now})
                    continue
                payload=json.loads(raw)
                kind=str(payload.get("type") or "")
                if kind=="audio_ack":
                    state["acks"]+=1
                    state["max_ack"]=max(state["max_ack"],int(payload.get("seq") or 0))
                    await queue.put({"kind":"audio_ack","at":now})
                    continue
                if kind!="event":
                    if kind:
                        state["event_types"].append(kind)
                    await queue.put({"kind":kind,"at":now,"payload":payload})
                    continue
                event=payload.get("event") or {}
                ek=str(event.get("type") or "")
                if ek:
                    state["event_types"].append(ek)
                if ek=="input_transcript":
                    state["input_transcripts"].append(str(event.get("text") or ""))
                elif ek=="output_transcript":
                    state["output_transcripts"].append(str(event.get("text") or ""))
                elif ek=="turn_complete":
                    state["turn_complete"]+=1
                elif ek=="interrupted":
                    state["interrupted"]+=1
                elif ek=="tool_call":
                    state["tool_calls"].append(str(event.get("name") or "unknown"))
                elif ek=="error":
                    state["errors"].append(str(event.get("code") or "LIVE_ERROR"))
                elif ek=="resource_budget" and str(event.get("code") or "").startswith("RESOURCE_"):
                    state["resource_denials"].append(str(event.get("code")))
                await queue.put({"kind":ek,"at":now,"event":event})
        except ConnectionClosed as exc:
            await queue.put({"kind":"connection_closed","code":int(exc.code),"at":time.monotonic()})

    async def wait_for(predicate,timeout:float,label:str):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            if predicate():
                return
            remaining=max(.05,deadline-time.monotonic())
            try:
                await asyncio.wait_for(queue.get(),min(2.0,remaining))
            except asyncio.TimeoutError:
                continue
        raise RuntimeError(f"timeout waiting for {label}")

    async def send_turn(ws,seconds:float):
        nonlocal seq
        await ws.send(json.dumps({"type":"input","message":{"activity_start":True}}))
        start=time.monotonic()
        target=start
        frame_bytes=3200
        turn=repeated_pcm(seed,seconds)
        first_seq=seq+1
        for offset in range(0,len(turn),frame_bytes):
            frame=turn[offset:offset+frame_bytes]
            seq+=1
            await ws.send(struct.pack("!III",INPUT_MAGIC,seq,0)+frame)
            target=start+min(len(turn),offset+len(frame))/32000.0
            delay=target-time.monotonic()
            if delay>0:
                await asyncio.sleep(delay)
        await ws.send(json.dumps({"type":"input","message":{"activity_end":True}}))
        return first_seq,seq

    try:
        async with await open_socket(
            started=started,
            cookie_header=http.cookie_header(),
            ws_base=args.ws_base,
            origin=args.origin,
        ) as ws:
            await hello(ws,started)
            recv_task=asyncio.create_task(receiver(ws))

            # Turn 1: get a response, then deliberately barge in while output audio is playing.
            t1_first,t1_last=await send_turn(ws,args.turn_seconds)
            await wait_for(lambda: len(state["input_transcripts"])>=1,30,"turn-1 input transcript")
            await wait_for(lambda: state["audio_binary"]>=1,30,"turn-1 playback")
            playback_frames_before=state["audio_binary"]
            turn_complete_before=state["turn_complete"]

            t2_started_at=time.monotonic()
            t2_first,t2_last=await send_turn(ws,args.turn_seconds)
            await wait_for(lambda: len(state["input_transcripts"])>=2,35,"barge-in turn transcript")
            await wait_for(
                lambda: state["turn_complete"]>turn_complete_before or state["interrupted"]>=1,
                35,
                "barge-in lifecycle completion",
            )
            bargein_elapsed_ms=round((time.monotonic()-t2_started_at)*1000)

            # Subsequent turns prove the same session remains usable after idle pauses.
            pause_results=[]
            for pause_s in (1,3,6,10):
                await wait_for(lambda: state["turn_complete"]>=1,30,"stable idle before pause")
                await asyncio.sleep(pause_s)
                transcript_before=len(state["input_transcripts"])
                complete_before=state["turn_complete"]
                first,last=await send_turn(ws,args.turn_seconds)
                await wait_for(
                    lambda b=transcript_before: len(state["input_transcripts"])>b,
                    35,
                    f"transcript after {pause_s}s pause",
                )
                await wait_for(
                    lambda b=complete_before: state["turn_complete"]>b,
                    45,
                    f"turn complete after {pause_s}s pause",
                )
                pause_results.append({
                    "pause_s":pause_s,
                    "frame_first":first,
                    "frame_last":last,
                    "transcript_chars":len(state["input_transcripts"][-1]),
                })

            expected_frames=seq
            await wait_for(lambda: state["max_ack"]>=expected_frames,15,"all audio ACKs")
            try:
                await ws.send(json.dumps({"type":"stop","reason":"ph_voice_lifecycle_canary"}))
            except Exception:
                pass
            recv_task.cancel()
            await asyncio.gather(recv_task,return_exceptions=True)

        ok=bool(
            state["max_ack"]>=seq
            and len(state["input_transcripts"])>=6
            and state["audio_binary"]>playback_frames_before
            and not state["tool_calls"]
            and not state["errors"]
            and not state["resource_denials"]
            and all(item["transcript_chars"]>0 for item in pause_results)
        )
        return {
            "ok":ok,
            "fixture":fixture_name,
            "session_id":session_id,
            "start_budget_retries":start_retries,
            "frames_sent":seq,
            "audio_ack_count":state["acks"],
            "max_audio_ack_seq":state["max_ack"],
            "input_transcript_count":len(state["input_transcripts"]),
            "output_transcript_count":len(state["output_transcripts"]),
            "audio_binary_frames":state["audio_binary"],
            "barge_in_started_during_playback":playback_frames_before>0,
            "barge_in_followup_transcript":len(state["input_transcripts"])>=2,
            "barge_in_elapsed_ms":bargein_elapsed_ms,
            "interrupted_events":state["interrupted"],
            "turn_complete_count":state["turn_complete"],
            "pause_results":pause_results,
            "tool_calls":state["tool_calls"],
            "errors":state["errors"],
            "resource_denials":state["resource_denials"],
            "physical_echo_acceptance":"not_run",
            "event_types":sorted(set(state["event_types"])),
            "turn_frames":{
                "first":[t1_first,t1_last],
                "barge_in":[t2_first,t2_last],
            },
        }
    finally:
        try:
            http.request(
                "POST",
                f"/api/live/{conversation_id}/sessions/{session_id}/stop",
                {},
                timeout=10.0,
            )
        except Exception:
            pass


async def async_main(args)->int:
    result=await run(args)
    print(json.dumps(result,ensure_ascii=False,sort_keys=True))
    return 0 if result["ok"] else 2


def main()->int:
    parser=argparse.ArgumentParser()
    parser.add_argument("--http-base",default="http://127.0.0.1:8196")
    parser.add_argument("--ws-base")
    parser.add_argument("--origin")
    parser.add_argument("--display-name",default="PH Voice Lifecycle Canary")
    parser.add_argument("--turn-seconds",type=float,default=3.0)
    args=parser.parse_args()
    if not 1.0<=args.turn_seconds<=8.0:
        raise SystemExit("--turn-seconds must be 1..8")
    args.ws_base=args.ws_base or _ws_base(args.http_base)
    args.origin=args.origin or _origin(args.http_base)
    return asyncio.run(async_main(args))


if __name__=="__main__":
    raise SystemExit(main())
