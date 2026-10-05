# PH-VOICE R2 — progressive transcript chunks

Date: 2026-10-05

## Owner-observed regression

Physical Android tests showed two related UX failures:

- a later utterance could be appended to the previous user bubble;
- during a long spoken thought no model-derived user text became visible until the speech segment was finally closed.

The first issue was fixed in 0.1.27 by opening a new UI user turn from the local `speech_start` boundary.

## Findings

The transport is not the missing-transcript bottleneck:

- WSS continued accepting and ACKing PCM during the affected physical session;
- the same `gemini-3.8-live` session returned a normal `input_transcript` once a speech boundary was emitted;
- a controlled real-provider diagnostic emitted an intermediate `activityEnd` during a 12 s synthetic utterance and received `input_transcript` about 406 ms later, before the final end of the overall test stream.

The provider therefore can return useful text well before microphone/session stop, but with the current conversational model the proven reliable text is finalized per speech segment. The 300 s PH-VOICE canaries still observed no `interim_input_transcript` during truly uninterrupted speech.

The PH-VOICE R1 implementation changed realtime capture to explicit manual activity boundaries and configured:

- normal speech-end silence: 4000 ms;
- after 12 s of speech: 8000 ms.

That policy delayed transcript visibility by design. It was not needed to fix the original ~30 s termination, whose root cause was resource-grant over-reservation.

Street Story was used only as a behavioral reference: its native VAD closes speech after roughly 1.2 s of silence. Its voice stack is not copied into Projects Hub.

A one-second boundary canary confirmed the risk of copying that value directly: six short user turns produced four provider `interrupted` events as continued speech barged into Mira's replies. Very short boundaries can make the dialogue twitchy and could expose partial instructions to tools.

## R2 decision

Keep the mature Projects Hub voice architecture unchanged:

- one central Mira / `gemini-3.8-live`;
- authenticated same-origin WSS;
- manual client VAD;
- durable PCM/recovery;
- existing resource-control, multi-user isolation and barge-in behavior;
- no second ASR and no browser speech recognizer.

Change only the realtime speech-end policy:

- `speechEndSilenceMs = 2000`;
- remove the 4000/8000 ms long-speech split.

Two seconds is the shared live-audio framework's established conservative tail. It is intentionally less aggressive than Street Story's ~1.2 s boundary.

Expected UX: while Live remains enabled, a natural pause of about two seconds finalizes the current speech segment and its provider-derived text becomes visible shortly afterward. Truly uninterrupted speech is still not claimed to have low-latency interim transcription.

## Acceptance for this R2

Automated before merge:

- Projects Hub web voice/framework suite passes;
- production PWA build passes;
- PR backend/PWA/Android checks pass.

Real-provider evidence already established:

- explicit intermediate speech boundary -> `input_transcript` in ~406 ms;
- short-turn lifecycle/barge-in canary passes with no provider/resource errors.

Physical owner acceptance after deploy:

1. speak for at least 30–60 s with several natural pauses around two seconds;
2. confirm user text appears before the microphone/session is stopped;
3. confirm later speech after a completed exchange opens a new user bubble and is not appended to an old one;
4. confirm Mira can still be interrupted naturally and the microphone remains active;
5. confirm no ~30 s resource stop returns.

This R2 does not claim V03's original stricter target of interim text during completely uninterrupted speech.
