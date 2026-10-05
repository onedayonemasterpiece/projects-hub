# PH-VOICE R3 — Android self-echo / adaptive duplex

Date: 2026-10-05

## Physical evidence

After deploying 0.1.28 the owner deliberately remained silent while Mira was speaking, but the conversation showed parasitic user fragments and Mira's replies were cut off.

The correlated production session was:

- session: `live_cc8ab6afc2f247529d58af30e8ee36e8`;
- source: `src_afed932c183b47feb26feda6740557a2`;
- backend: 0.1.28 / `02638707937411b01b42e58626b0368f06666449`.

Observed pattern repeated four times:

1. Mira emitted `output_transcript`;
2. microphone PCM continued to be accepted while playback was active;
3. a tiny `input_transcript` appeared (several were only five characters);
4. provider emitted `interrupted` and the response ended early.

Because the owner was silent, this is physical evidence of residual playback audio being admitted as microphone speech, not normal user barge-in.

## Root cause

Projects Hub intentionally used `suppressCaptureDuringPlayback: false` to preserve real barge-in. The shared browser client therefore forwarded every captured microphone frame during Mira playback. Android/WebView AEC was requested (`echoCancellation`, `noiseSuppression`, `autoGainControl`), but it did not remove enough speaker echo on the physical device.

The capture AudioWorklet itself outputs zeros, so there is no software monitor loop.

The 0.1.28 two-second speech boundary did not create the echo. It made the residual echo finalize into visible short transcripts sooner.

## Fix

Shared `live-interaction` 0.3.21, exact merge SHA `330cbc2564c5561ccf36b1bf1de29666fbf637b9`, adds opt-in adaptive duplex admission:

- during Mira playback, captured PCM is not immediately sent to Live;
- a short calibration learns bounded residual echo level;
- isolated spikes remain suppressed;
- sustained independent speech above the learned echo floor is admitted as a real barge-in;
- playback is cancelled locally before buffered barge-in onset is sent to the normal ordered audio sender;
- products not opting into `"adaptive"` keep their previous behavior.

Shared verification before Projects Hub integration:

- Node: 78/78 PASS;
- Python: 53/53 PASS;
- GitHub shared contracts: PASS;
- native Gradle: PASS.

Projects Hub 0.1.29 opts into `suppressCaptureDuringPlayback: "adaptive"` while preserving the 0.1.28 progressive speech boundary (`speechEndSilenceMs=2000`), WSS, durable PCM/recovery, resource admission, user isolation and the user-bubble boundary fix.

## Physical acceptance

1. Stay silent while Mira gives a multi-sentence answer: no new user bubble/transcript and no provider `interrupted`.
2. Deliberately start speaking over Mira: her playback stops after a bounded onset and the user's phrase reaches Live.
3. Dictate a long thought with natural pauses around two seconds: provider-derived user text appears incrementally while Live remains enabled.
4. A later independent user utterance starts a new bubble.
5. No return of the old ~30-second resource stop.

Adaptive thresholds are deliberately conservative and remain subject to physical-device tuning; synthetic tests do not substitute for this acceptance.
