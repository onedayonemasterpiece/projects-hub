# PH-VOICE R4 — speech admission, transcription hints and reply telemetry

Date: 2026-10-05

## Physical evidence on 0.1.29

Owner tests on physical Android reported:

- keyboard typing and a finger snap were recognized as short foreign-language user text;
- ordinary short pauses during long speech did not produce visible partial text;
- after one long input Mira appeared not to answer.

Correlated production sessions on backend 0.1.29 / a2e9390fdcdce651a24603a86aa377df3411d965 confirmed:

- repeated input_transcript events of length 5 from isolated non-speech sounds;
- latest long speech session live_7478ef22356a490cacae7dd6f712dafe produced a 212-character input transcript and then a 20-character input transcript; the user's shorter natural pauses did not cross the 2000 ms speech-end boundary;
- after that input, the provider did generate a 113-character output_transcript and turn_complete;
- later 5-character parasitic input turns still occurred;
- no provider/resource error and no tool call explained the missing perceived answer.

## Root causes / conclusions

### Non-speech sounds

The shared manual VAD opened provider activity as soon as one capture frame exceeded RMS 0.008. A keyboard click or finger snap can satisfy that condition. Once activity was opened, Gemini received a short semantic audio turn and could hallucinate a word.

Projects Hub also supplied input_audio_transcription as an empty object, so each short segment used automatic language detection.

### Progressive user text

The original product requirement remains open.

The Live API wire contract includes interimInputTranscription and the Projects Hub adapter already handles it, but real gemini-3.8-live runs used by Projects Hub have not emitted interim_input_transcript during uninterrupted speech. The general Gemini 3.8 Live capability example documents input audio transcription but shows finalized input_transcription; Google's dedicated low-latency interim documentation is centered on Live Transcribe.

The 2000 ms manual boundary is therefore only a finalized-segment fallback. It cannot satisfy "show what I am saying while I continue talking" when normal pauses are shorter than two seconds. Lowering activityEnd further is not acceptable because activityEnd is a semantic turn boundary and causes Mira/tools to act on incomplete thoughts.

No second ASR/model is introduced by R4.

## R4 implementation

Shared live-interaction 0.3.22:

- adds opt-in speechStartMs onset admission;
- Projects Hub will use 180 ms;
- short impulses are rejected before activityStart;
- accepted speech replays its buffered onset so first syllables are retained;
- default remains 0 for other consumers.

Projects Hub 0.1.30:

- pins shared live-interaction exact merge SHA bc4a25e8188df2c4ff293f0e14578330fd0c2c9c;
- uses speechStartMs=180;
- keeps speechEndSilenceMs=2000 and adaptive duplex echo rejection;
- configures input transcription with languageCodes [ru-RU, en-US], VERBATIM mode and bounded product vocabulary;
- records aggregate provider audio counts/bytes for each completed turn, without persisting audio content, so a future "Mira did not answer" report distinguishes provider-no-audio from client playback loss.

## Acceptance

Delivery gate: PR CI must install both Projects Hub and the exact shared Live dependency in a clean runner; local cached dependencies are not acceptance evidence.

Automated gates before merge:

- shared 0.3.22: 80/80 Node + 53/53 Python + GitHub contracts/native CI;
- Projects Hub web voice suite and production build;
- immutable shared release/provenance contract;
- Projects Hub backend CI including live-adapter tests.

Physical checks after deploy:

1. keyboard taps and finger snaps while otherwise silent do not create a user bubble/transcript;
2. normal Russian speech still begins without clipped first syllables;
3. deliberate real speech over Mira still works as barge-in;
4. if Mira appears silent after a turn, correlate turn_output_audio_events/bytes in backend logs;
5. progressive text during uninterrupted speech remains explicitly NOT accepted until real interim events are demonstrated.
