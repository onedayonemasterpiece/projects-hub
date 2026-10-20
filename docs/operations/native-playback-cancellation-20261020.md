# Native playback cancellation — 20 October 2026

Continue the single primary Android application and its in-app update feed. SDK rc12 is the immutable release `8823d89c100cd76af89d503749f420c65944ab49`, archive SHA-256 `d687e85c686380518e8439630fbb39c0cee90a0110f03d09efaa337329cc0f92` (192511 bytes).

## Corrected mechanism

Previously Android remembered Stop/barge-in output suppression only in the current Run. Reopening the runtime offered stored output for recovery without the cancelled state. The shared SDK now fences known output bindings, including a socket-outage case, and persists the decision on the existing ordered output worker before Stop finalization. Product NativeVoiceStore implements `cancelPlayback` as append-only record 11, synced before in-memory acknowledgement. Restoring the store restores that marker. Late original chunks cannot clear it.

Cancellation is NOT playback: original bytes and actual played cursor remain unchanged. Old journals without a marker are not silently deleted or guessed cancelled. Ordinary network loss does not cancel pending output. Existing scope authorization, certificate/package, updater, codecs, quotas and the sole semantic Live agent are preserved.

Tests cover reopening, late tail/empty-source writes, repeat cancellation without extra records, retained original bytes and exact played positions, unknown/input/foreign sources, and the shared detached-bindings fence.

## Native provider evidence remains a separate gate

The retained rc11 native-v2 check in `/home/dev/artifacts/projects-hub/20261020T101444Z-native-v2-57723de86d3c` exercised the installed environment using the original synthetic Android CI source. All 763 chunks reached the server durable prefix and the source was complete. Both response turns reached a terminal result (one completed output, one explicitly interrupted output). The first of two full-phrase transcript assertions failed; the wait therefore timed out. Report SHA-256 `c2a82315c8cdffff401fe536e7cc24f91da84923ac5f70416b7ee59b2cb095f7`. Cleanup left zero host sessions and caption sidecars. No secondary ASR qualification was requested.

Do not call this a passed native-provider gate, packet loss, or a proved root cause of historical Samsung symptoms. rc12 changes local Android cancellation, not the Python ASR path; it does not claim to fix the transcription failure. Keep strict waveform/full-phrase checks and the failed evidence. Physical Samsung playback/Stop, long conversations, noisy routes, and concurrency still need measured acceptance on the actual installed primary package.
