# Native playback cancellation — 20 October 2026

Continue the single primary Android application and its in-app update feed. SDK rc12 is the immutable release `8823d89c100cd76af89d503749f420c65944ab49`, archive SHA-256 `d687e85c686380518e8439630fbb39c0cee90a0110f03d09efaa337329cc0f92` (192511 bytes).

## Corrected mechanism

Previously Android remembered Stop/barge-in output suppression only in the current Run. Reopening the runtime offered stored output for recovery without the cancelled state. The shared SDK now fences known output bindings, including a socket-outage case, and persists the decision on the existing ordered output worker before Stop finalization. Product NativeVoiceStore implements `cancelPlayback` as append-only record 11, synced before in-memory acknowledgement. Restoring the store restores that marker. Late original chunks cannot clear it.

Cancellation is NOT playback: original bytes and actual played cursor remain unchanged. Old journals without a marker are not silently deleted or guessed cancelled. Ordinary network loss does not cancel pending output. Existing scope authorization, certificate/package, updater, codecs, quotas and the sole semantic Live agent are preserved.

Tests cover reopening, late tail/empty-source writes, repeat cancellation without extra records, retained original bytes and exact played positions, unknown/input/foreign sources, and the shared detached-bindings fence.

## Corrected interrupted-response attribution

The retained trace also showed a separate deterministic product bug: after capture B started, the provider sent interrupted A followed by turn_complete A. The adapter cleared A's response ownership on interrupted and wrongly committed B from A's terminal event, before B had ended. Two regression cases reproduce this on the previous code (B still open or already ended). Retain response ownership until turn_complete; immediate playback cancellation is unchanged. The focused adapter/durable/native suite passes 52 tests after this change. The event ordering is documented by [Google's server-content reference](https://ai.google.dev/api/live#BidiGenerateContentServerContent). This is a fix to semantic receipt attribution, not ASR accuracy. Follow-up review reproduced two more provider-boundary cases: interrupted output without its final completion could retain old ownership across provider reconnect/resume. Clear only that interrupted ownership at a provider boundary, not on an ordinary handset transport gap. Unanchored terminal events cannot commit fresh native speech. Four further red-then-green regressions plus the original focused suite pass (56 tests). Native PyAV integration remains gated by full CI because local system Python lacks PyAV.

Independent Claude Opus 5.5 review `dvt_036035f5da7d46db9d57bb3e3c0972b3` confirmed the original two changes and requested the above boundary check. SDK output source identity was verified to be `out_<session.id>_<response_id>`, not a single source reused for all answers. No downgrade/old journal purge is performed. Old unmarked sources remain unmodified rather than guessed cancelled.

## Emulator waveform investigation

Exact candidate 3811f7f Android run 39412276066 failed speaker PCM correlation 0.32836 < 0.45; all six instrumentation cases and eleven other audio checks passed. Artifact 12260332211 (8061023 bytes, SHA-256 8d976e86188e7c3eadb6c8be8bf20c3d6090591ccc8cd9a3c8d2967e213ac3cb) is retained. Independent FFT search across its entire speaker WAV also found no complete high-correlation reply; this is not merely the verifier selecting the wrong repeat. Short-window alignment shows variable intra-reply delay. The first Android track had all 89038 frames written/played and no reported underrun until the terminal exhausted buffer; this alone cannot establish where the monitor waveform changed.

The CI-only Pulse request was 5 ms, overriding the [upstream QEMU Pulse output target of 10 ms](https://github.com/aosp-mirror/platform_external_qemu/blob/emu-master-dev/audio/paaudio.c). [PulseAudio patch_buffer_attr](https://github.com/pulseaudio/pulseaudio/blob/77d25a1e613095bdf87a1d13b65e7c330565077a/src/pulse/stream.c) applies that environment override to the stream buffer. Test the existing runner at 10 ms with the 1000 Hz QEMU timer, formats, microphone input, fixtures and every waveform threshold unchanged. This is an isolated host-buffer configuration check, not a claimed fix for Samsung or a reason to alter product playback. Require the complete exact-source audio gate before publication.

The separately retained hybrid VAD experiment on installed rc11 (20261020T110804Z-native-v2-57723de86d3c, report SHA-256 4fc5f43f0b157db39e5b6bb98c7694354c472b1d581ceb3fcb1dc1e8d39d88a8) failed with no response. It changed configuration only in an isolated child process, not production, and is not adopted. Google recommendations must be qualified through the actual input path, not copied blindly.

## Native provider evidence remains a separate gate

The retained rc11 native-v2 check in `/home/dev/artifacts/projects-hub/20261020T101444Z-native-v2-57723de86d3c` exercised the installed environment using the original synthetic Android CI source. All 763 chunks reached the server durable prefix and the source was complete. Both response turns reached a terminal result (one completed output, one explicitly interrupted output). The first of two full-phrase transcript assertions failed; the wait therefore timed out. Report SHA-256 `c2a82315c8cdffff401fe536e7cc24f91da84923ac5f70416b7ee59b2cb095f7`. Cleanup left zero host sessions and caption sidecars. No secondary ASR qualification was requested.

Do not call this a passed native-provider gate, packet loss, or a proved root cause of historical Samsung symptoms. rc12 changes local Android cancellation, not the Python ASR path; it does not claim to fix the transcription failure. Keep strict waveform/full-phrase checks and the failed evidence. Physical Samsung playback/Stop, long conversations, noisy routes, and concurrency still need measured acceptance on the actual installed primary package.
