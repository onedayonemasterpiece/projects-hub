package com.kenigevents.projectshub;

import org.junit.Rule;
import org.junit.Test;
import org.junit.rules.TemporaryFolder;
import org.onedayonemasterpiece.live.DurableSourceStore;
import org.onedayonemasterpiece.live.DurableOutputStore;
import org.onedayonemasterpiece.live.VoiceAudioV2;

import java.io.File;
import java.io.RandomAccessFile;
import java.nio.file.Files;
import java.util.List;
import java.util.stream.Collectors;

import static org.junit.Assert.*;

public final class NativeVoiceStoreTest {
    @Rule public TemporaryFolder temporary = new TemporaryFolder();
    private static final NativeVoiceStore.Scope SCOPE = new NativeVoiceStore.Scope("actor_A", "workspace_A", "conversation_A");
    private static final NativeVoiceStore.Limits LIMITS = new NativeVoiceStore.Limits(1024 * 1024, 8, 120 * 60_000_000L);
    private static final VoiceAudioV2.SourceDescriptor SOURCE = new VoiceAudioV2.SourceDescriptor("local_12345678901234567890123456789012", 1700000000000L);
    private static final VoiceAudioV2.CodecEpoch AAC = new VoiceAudioV2.CodecEpoch("epoch_1", "aac", "adts", 1, 1, 16000, 16000, 0, new byte[0]);
    private static final VoiceAudioV2.SourceDescriptor OUTPUT = new VoiceAudioV2.SourceDescriptor("out_session_7_42", 0);

    private NativeVoiceStore open(File directory) throws Exception {
        return new NativeVoiceStore(directory, SCOPE, LIMITS);
    }
    private static VoiceAudioV2.EncodedChunk chunk(long sequence, int bytes) {
        byte[] payload = new byte[bytes];
        java.util.Arrays.fill(payload, (byte) sequence);
        return new VoiceAudioV2.EncodedChunk(SOURCE.sourceId(), AAC.epochId(), sequence,
                (sequence - 1) * 20000, 20000, 0, payload);
    }
    private static void initialize(NativeVoiceStore store) throws Exception {
        store.open(SOURCE);
        store.registerEpoch(SOURCE.sourceId(), AAC);
    }
    private static VoiceAudioV2.OutputChunk output(long sequence, long eventSeq, int flags) {
        return new VoiceAudioV2.OutputChunk(new VoiceAudioV2.EncodedChunk(OUTPUT.sourceId(), AAC.epochId(),
                sequence, (sequence - 1) * 20000, 20000, flags, new byte[]{(byte) sequence, 9}), eventSeq);
    }
    private static File journal(File directory) throws Exception {
        try (var paths = Files.walk(directory.toPath())) {
            return paths.filter(path -> path.toString().endsWith(".wvs")).findFirst().orElseThrow().toFile();
        }
    }
    private static void code(String expected, Throwing action) throws Exception {
        try { action.run(); fail("Expected " + expected); }
        catch (NativeVoiceStore.StoreException failure) { assertEquals(expected, failure.code()); }
    }
    @FunctionalInterface private interface Throwing { void run() throws Exception; }

    @Test public void alternatingMicAndSpeakerJournalsKeepBoundedHandlesAndDurableOriginals() throws Exception {
        File directory = temporary.newFolder();
        try (NativeVoiceStore store = open(directory)) {
            initialize(store);
            store.openOutput(OUTPUT);
            store.registerOutputEpoch(OUTPUT.sourceId(), AAC);
            // Capture, output decoding, playback position and server ACK all
            // use this store concurrently. Repeated source switching must not
            // close/reopen the same two journals on every sample/receipt.
            for (int n = 1; n <= 24; n++) {
                assertEquals(n, store.append(chunk(n, 32)).chunkSeq());
                assertEquals(n, store.appendOutput(output(n, n, n == 24 ? VoiceAudioV2.EPOCH_END : 0)).chunkSeq());
                store.markPlayed(OUTPUT.sourceId(), n * 20000L - 10000L);
                store.markServerCommitted(new VoiceAudioV2.DurablePrefix(SOURCE.sourceId(), n));
                assertEquals("duplex journals stay open", 2, store.openJournalCountForTesting());
            }
            // Many historical sources may be retained for crash recovery; open
            // descriptors must remain bounded independently of source count.
            for (int n = 0; n < 5; n++) {
                var extra = new VoiceAudioV2.SourceDescriptor("local_extra_" + n, 1700000001000L + n);
                store.open(extra);
                store.registerEpoch(extra.sourceId(), AAC);
                store.append(new VoiceAudioV2.EncodedChunk(extra.sourceId(), AAC.epochId(),
                        1, 0, 20000, 0, new byte[]{(byte)n, 9}));
                assertTrue("journal descriptor cap", store.openJournalCountForTesting() <= 4);
            }
            // Reopening an evicted source does not alter its original bytes.
            assertEquals(List.of(output(24, 24, VoiceAudioV2.EPOCH_END)),
                    store.readOutput(OUTPUT.sourceId(), 23, 8192, 20000));
            assertTrue(store.openJournalCountForTesting() <= 4);
        }
        try (NativeVoiceStore reopened = open(directory)) {
            assertEquals(7, reopened.sources().size() + reopened.playbackPositions().size());
            assertEquals(24, reopened.playbackPositions().get(0).receivedThrough());
            assertEquals(470000, reopened.playbackPositions().get(0).playedUs());
            assertEquals(24, reopened.sources().stream()
                    .filter(s -> s.source().equals(SOURCE)).findFirst().orElseThrow().serverCommittedThrough());
            assertEquals(List.of(output(24, 24, VoiceAudioV2.EPOCH_END)),
                    reopened.readOutput(OUTPUT.sourceId(), 23, 8192, 20000));
        }
    }

    @Test public void cancelledOutputSurvivesReopenAndLateTailWithoutInventingPlayback() throws Exception {
        File directory = temporary.newFolder();
        try (NativeVoiceStore store = open(directory)) {
            store.openOutput(OUTPUT); store.registerOutputEpoch(OUTPUT.sourceId(), AAC);
            store.appendOutput(output(1, 18, 0));
            store.markPlayed(OUTPUT.sourceId(), 7000);
            store.cancelPlayback(OUTPUT.sourceId());
            long once = journal(directory).length();
            store.cancelPlayback(OUTPUT.sourceId());
            assertEquals(once, journal(directory).length());
            // Cancellation does not discard a subsequently received exact original tail.
            store.appendOutput(output(2, 21, VoiceAudioV2.EPOCH_END));
        }
        try (NativeVoiceStore store = open(directory)) {
            var position = store.playbackPositions().get(0);
            assertTrue(position.playbackCancelled());
            assertEquals(7000, position.playedUs());
            assertEquals(40000, position.durationUs());
            assertEquals(2, position.receivedThrough());
            assertTrue(position.sealed());
            assertEquals(List.of(output(1, 18, 0), output(2, 21, VoiceAudioV2.EPOCH_END)),
                    store.readOutput(OUTPUT.sourceId(), 0, 8192, 240000));
            assertEquals(2, store.appendOutput(output(1, 18, 0)).chunkSeq());
            assertTrue(store.playbackPositions().get(0).playbackCancelled());
        }
    }

    @Test public void cancellingAnOpenedEmptyOutputSurvivesItsLateFirstChunk() throws Exception {
        File directory = temporary.newFolder();
        try (NativeVoiceStore store = open(directory)) {
            store.openOutput(OUTPUT);
            store.cancelPlayback(OUTPUT.sourceId());
            store.registerOutputEpoch(OUTPUT.sourceId(), AAC);
            store.appendOutput(output(1, 1, VoiceAudioV2.EPOCH_END));
        }
        try (NativeVoiceStore store = open(directory)) {
            assertTrue(store.playbackPositions().get(0).playbackCancelled());
            assertEquals(0, store.playbackPositions().get(0).playedUs());
            assertEquals(1, store.readOutput(OUTPUT.sourceId(), 0, 8192, 240000).size());
        }
    }

    @Test public void oldUncancelledJournalsRemainRecoverableAndCancellationIsScopeBound() throws Exception {
        File directory = temporary.newFolder();
        try (NativeVoiceStore store = open(directory)) {
            initialize(store); store.append(chunk(1, 64));
            store.openOutput(OUTPUT); store.registerOutputEpoch(OUTPUT.sourceId(), AAC);
            store.appendOutput(output(1, 1, 0));
            code("VOICE_SOURCE_DIRECTION", () -> store.cancelPlayback(SOURCE.sourceId()));
            code("VOICE_SOURCE_UNKNOWN", () -> store.cancelPlayback("out_unknown"));
        }
        try (NativeVoiceStore store = open(directory)) {
            assertFalse(store.playbackPositions().get(0).playbackCancelled());
            assertEquals(List.of(chunk(1, 64)), store.readPending(SOURCE.sourceId(), 0, 8192, 240000));
            store.cancelPlayback(OUTPUT.sourceId());
        }
        var foreign = new NativeVoiceStore.Scope("actor_B", SCOPE.workspaceId(), SCOPE.conversationId());
        try (NativeVoiceStore store = new NativeVoiceStore(directory, foreign, LIMITS)) {
            assertTrue(store.playbackPositions().isEmpty());
            code("VOICE_SOURCE_UNKNOWN", () -> store.cancelPlayback(OUTPUT.sourceId()));
        }
    }

    @Test public void reopenedSourceRetainsOriginalEncodedBytesAndIndependentDurableCursors() throws Exception {
        File directory = temporary.newFolder();
        try (NativeVoiceStore store = open(directory)) {
            initialize(store);
            assertEquals(1, store.append(chunk(1, 120)).chunkSeq());
            assertEquals(2, store.append(chunk(2, 80)).chunkSeq());
            store.markServerCommitted(new VoiceAudioV2.DurablePrefix(SOURCE.sourceId(), 1));
        }
        try (NativeVoiceStore store = open(directory)) {
            DurableSourceStore.SourceStatus state = store.sources().get(0);
            assertEquals(SOURCE, state.source());
            assertEquals(2, state.deviceDurableThrough());
            assertEquals(1, state.serverCommittedThrough());
            assertFalse(state.sealed());
            assertEquals(AAC, store.epoch(SOURCE.sourceId(), AAC.epochId()));
            assertEquals(List.of(chunk(2, 80)), store.readPending(SOURCE.sourceId(), 0, 8192, 240000));
        }
    }

    @Test public void retryIsIdempotentAndConflictingBytesOrTimelineCannotReuseSequence() throws Exception {
        try (NativeVoiceStore store = open(temporary.newFolder())) {
            initialize(store);
            store.append(chunk(1, 32));
            store.append(chunk(2, 32));
            assertEquals(2, store.append(chunk(1, 32)).chunkSeq());
            code("VOICE_SOURCE_CONFLICT", () -> store.append(chunk(1, 33)));
            code("VOICE_SOURCE_CONFLICT", () -> store.append(new VoiceAudioV2.EncodedChunk(
                    SOURCE.sourceId(), AAC.epochId(), 1, 1000, 20000, 0, chunk(1, 32).payload())));
            code("VOICE_SOURCE_GAP", () -> store.append(chunk(4, 32)));
            assertEquals(2, store.sources().get(0).deviceDurableThrough());
        }
    }

    @Test public void epochChangePreservesSourceSequenceAndRejectsMutableCodecInit() throws Exception {
        try (NativeVoiceStore store = open(temporary.newFolder())) {
            initialize(store);
            store.append(chunk(1, 30));
            VoiceAudioV2.CodecEpoch opus = new VoiceAudioV2.CodecEpoch("epoch_2", "opus", "ogg", 1, 1,
                    16000, 48000, 312, new byte[]{1, 2, 3});
            store.registerEpoch(SOURCE.sourceId(), opus);
            store.registerEpoch(SOURCE.sourceId(), opus);
            code("VOICE_SOURCE_CONFLICT", () -> store.registerEpoch(SOURCE.sourceId(),
                    new VoiceAudioV2.CodecEpoch("epoch_2", "opus", "ogg", 1, 1,
                            16000, 48000, 312, new byte[]{1, 2, 4})));
            store.append(new VoiceAudioV2.EncodedChunk(SOURCE.sourceId(), opus.epochId(), 2,
                    20000, 20000, VoiceAudioV2.EPOCH_END, new byte[]{9}));
            assertEquals(List.of(1L, 2L), store.readPending(SOURCE.sourceId(), 0, 8192, 240000)
                    .stream().map(VoiceAudioV2.EncodedChunk::chunkSeq).collect(Collectors.toList()));
        }
    }

    @Test public void readBudgetsKeepOrderedPrefixAndServerReceiptCannotOutrunDevice() throws Exception {
        try (NativeVoiceStore store = open(temporary.newFolder())) {
            initialize(store);
            store.append(chunk(1, 100));
            store.append(chunk(2, 10));
            assertTrue(store.readPending(SOURCE.sourceId(), 0, 99, 240000).isEmpty());
            assertEquals(List.of(chunk(1, 100)), store.readPending(SOURCE.sourceId(), 0, 109, 240000));
            assertEquals(List.of(chunk(1, 100)), store.readPending(SOURCE.sourceId(), 0, 8192, 20000));
            code("VOICE_SOURCE_RECEIPT_RANGE", () -> store.markServerCommitted(new VoiceAudioV2.DurablePrefix(SOURCE.sourceId(), 3)));
            store.markServerCommitted(new VoiceAudioV2.DurablePrefix(SOURCE.sourceId(), 1));
            assertEquals(1, store.markServerCommitted(new VoiceAudioV2.DurablePrefix(SOURCE.sourceId(), 0)).chunkSeq());
        }
    }

    @Test public void tornUnreceiptedTailIsDiscardedWithoutLosingDurablePrefix() throws Exception {
        File directory = temporary.newFolder();
        try (NativeVoiceStore store = open(directory)) { initialize(store); store.append(chunk(1, 64)); }
        File path = journal(directory);
        long receiptedLength = path.length();
        try (RandomAccessFile file = new RandomAccessFile(path, "rw")) {
            file.seek(file.length());
            file.writeInt(256);
            file.write(new byte[17]);
            file.getFD().sync();
        }
        try (NativeVoiceStore store = open(directory)) {
            assertEquals(receiptedLength, path.length());
            assertEquals(List.of(chunk(1, 64)), store.readPending(SOURCE.sourceId(), 0, 8192, 240000));
            assertEquals(2, store.append(chunk(2, 64)).chunkSeq());
        }
    }

    @Test public void completeCorruptRecordFailsVisiblyAndIsNeverSilentlyTruncated() throws Exception {
        File directory = temporary.newFolder();
        try (NativeVoiceStore store = open(directory)) { initialize(store); store.append(chunk(1, 64)); }
        File path = journal(directory);
        long length = path.length();
        try (RandomAccessFile file = new RandomAccessFile(path, "rw")) {
            file.seek(length - 40);
            file.writeByte(55);
            file.getFD().sync();
        }
        code("VOICE_STORE_CORRUPT", () -> open(directory));
        assertEquals(length, path.length());
    }

    @Test public void quotaFailureRetainsExistingSourceAndNeverDropsUncommittedAudio() throws Exception {
        File directory = temporary.newFolder();
        NativeVoiceStore.Limits limits = new NativeVoiceStore.Limits(30000, 4, 120 * 60_000_000L);
        try (NativeVoiceStore store = new NativeVoiceStore(directory, SCOPE, limits)) {
            initialize(store);
            store.append(chunk(1, 8192)); store.append(chunk(2, 8192)); store.append(chunk(3, 8192));
            code("VOICE_STORE_QUOTA", () -> store.append(chunk(4, 8192)));
            code("VOICE_SOURCE_RETAINED", () -> store.release(SOURCE.sourceId()));
            assertEquals(3, store.sources().get(0).deviceDurableThrough());
        }
        try (NativeVoiceStore store = new NativeVoiceStore(directory, SCOPE, limits)) {
            assertEquals(3, store.readPending(SOURCE.sourceId(), 0, 3 * 8192, 240000).size());
        }
    }

    @Test public void sealAndFullServerReceiptAndDispositionAreAllRequiredBeforeRelease() throws Exception {
        File directory = temporary.newFolder();
        try (NativeVoiceStore store = open(directory)) {
            initialize(store); store.append(chunk(1, 64));
            code("VOICE_SOURCE_RETAINED", () -> store.release(SOURCE.sourceId()));
            store.seal(SOURCE.sourceId(), 20000, 16);
            store.seal(SOURCE.sourceId(), 20000, 16);
            code("VOICE_SOURCE_CONFLICT", () -> store.seal(SOURCE.sourceId(), 20000, 17));
            code("VOICE_SOURCE_SEALED", () -> store.append(chunk(2, 64)));
            store.disposition(SOURCE.sourceId(), "saved_as_note");
            code("VOICE_SOURCE_RETAINED", () -> store.release(SOURCE.sourceId()));
        }
        try (NativeVoiceStore store = open(directory)) {
            DurableSourceStore.SourceStatus state = store.sources().get(0);
            assertTrue(state.sealed()); assertEquals(16, state.endTrimSamples());
            assertEquals("saved_as_note", state.disposition());
            store.markServerCommitted(new VoiceAudioV2.DurablePrefix(SOURCE.sourceId(), 1));
            code("VOICE_SOURCE_RETAINED", () -> store.release(SOURCE.sourceId()));
            assertFalse(store.sources().get(0).serverSealed());
            store.markServerSealed(SOURCE.sourceId());
            assertTrue(store.sources().get(0).serverSealed());
            store.release(SOURCE.sourceId());
            assertTrue(store.sources().isEmpty());
        }
    }

    @Test public void scopeIsolationDoesNotResetDeviceWideQuotaAndSecondOwnerIsRejected() throws Exception {
        File directory = temporary.newFolder();
        NativeVoiceStore.Limits limits = new NativeVoiceStore.Limits(1024 * 1024, 1, 120 * 60_000_000L);
        try (NativeVoiceStore store = new NativeVoiceStore(directory, SCOPE, limits)) {
            initialize(store);
            code("VOICE_STORE_IN_USE", () -> open(directory));
            assertEquals(1, store.append(chunk(1, 64)).chunkSeq());
        }
        NativeVoiceStore.Scope other = new NativeVoiceStore.Scope("actor_B", "workspace_B", "conversation_B");
        try (NativeVoiceStore store = new NativeVoiceStore(directory, other, limits)) {
            assertTrue(store.sources().isEmpty());
            code("VOICE_SOURCE_UNKNOWN", () -> store.readPending(SOURCE.sourceId(), 0, 8192, 240000));
            code("VOICE_STORE_QUOTA", () -> store.open(SOURCE));
        }
    }

    @Test public void outputOriginalsAndActualPlayedPositionSurviveReopenIndependently() throws Exception {
        File directory = temporary.newFolder();
        try (NativeVoiceStore store = open(directory)) {
            store.openOutput(OUTPUT); store.registerOutputEpoch(OUTPUT.sourceId(), AAC);
            assertEquals(1, store.appendOutput(output(1, 18, 0)).chunkSeq());
            assertEquals(2, store.appendOutput(output(2, 21, VoiceAudioV2.EPOCH_END)).chunkSeq());
            assertEquals(0, store.playbackPositions().get(0).playedUs());
            store.markPlayed(OUTPUT.sourceId(), 17000); // scheduled/received audio remains ahead of hardware
            store.markPlayed(OUTPUT.sourceId(), 10000); // a stale callback cannot rewind durable progress
        }
        try (NativeVoiceStore store = open(directory)) {
            DurableOutputStore.PlaybackPosition position = store.playbackPositions().get(0);
            assertEquals(OUTPUT, position.source());
            assertEquals(2, position.receivedThrough()); assertEquals(21, position.originalEventSeq());
            assertEquals(17000, position.playedUs()); assertEquals(40000, position.durationUs());
            assertTrue(position.sealed());
            assertEquals(AAC, store.outputEpoch(OUTPUT.sourceId(), AAC.epochId()));
            assertEquals(List.of(output(1, 18, 0), output(2, 21, VoiceAudioV2.EPOCH_END)),
                    store.readOutput(OUTPUT.sourceId(), 0, 8192, 240000));
            assertEquals(2, store.appendOutput(output(1, 18, 0)).chunkSeq());
            code("VOICE_OUTPUT_PLAYED_RANGE", () -> store.markPlayed(OUTPUT.sourceId(), 40001));
            code("VOICE_SOURCE_SEALED", () -> store.appendOutput(output(3, 22, 0)));
            code("VOICE_SOURCE_RETAINED", () -> store.release(OUTPUT.sourceId()));
        }
    }

    @Test public void outputCannotEnterUplinkRecoveryAndConflictingOriginalEventIsRejected() throws Exception {
        try (NativeVoiceStore store = open(temporary.newFolder())) {
            initialize(store); store.append(chunk(1, 32));
            store.openOutput(OUTPUT); store.registerOutputEpoch(OUTPUT.sourceId(), AAC);
            store.appendOutput(output(1, 5, 0));
            assertEquals(1, store.sources().size()); assertEquals(SOURCE, store.sources().get(0).source());
            assertEquals(20000, store.pendingDurationUs());
            code("VOICE_SOURCE_DIRECTION", () -> store.readPending(OUTPUT.sourceId(), 0, 8192, 240000));
            code("VOICE_SOURCE_DIRECTION", () -> store.readOutput(SOURCE.sourceId(), 0, 8192, 240000));
            code("VOICE_SOURCE_DIRECTION", () -> store.markServerCommitted(new VoiceAudioV2.DurablePrefix(OUTPUT.sourceId(), 1)));
            code("VOICE_SOURCE_CONFLICT", () -> store.open(OUTPUT));
            code("VOICE_SOURCE_CONFLICT", () -> store.appendOutput(output(1, 6, 0)));
            code("VOICE_SOURCE_EVENT_ORDER", () -> store.appendOutput(output(2, 4, 0)));
            store.markServerCommitted(new VoiceAudioV2.DurablePrefix(SOURCE.sourceId(), 1));
            assertEquals(0, store.pendingDurationUs());
        }
    }

    @Test public void outputReadIsBoundedAndASecondEpochCannotSilentlyReplaceOriginal() throws Exception {
        try (NativeVoiceStore store = open(temporary.newFolder())) {
            store.openOutput(OUTPUT); store.registerOutputEpoch(OUTPUT.sourceId(), AAC);
            store.appendOutput(output(1, 3, 0)); store.appendOutput(output(2, 4, VoiceAudioV2.EPOCH_END));
            assertTrue(store.readOutput(OUTPUT.sourceId(), 0, 1, 240000).isEmpty());
            assertEquals(List.of(output(1, 3, 0)), store.readOutput(OUTPUT.sourceId(), 0, 3, 240000));
            assertEquals(List.of(output(2, 4, VoiceAudioV2.EPOCH_END)), store.readOutput(OUTPUT.sourceId(), 1, 8192, 20000));
            code("VOICE_SOURCE_SEALED", () -> store.registerOutputEpoch(OUTPUT.sourceId(),
                    new VoiceAudioV2.CodecEpoch("replacement", "aac", "adts", 1, 1, 16000, 16000, 0, new byte[0])));
            assertEquals(0, store.playbackPositions().get(0).playedUs());
        }
    }

    @Test public void serverSealReceiptIsIndependentOfChunkReceiptAndSurvivesReopen() throws Exception {
        File directory = temporary.newFolder();
        try (NativeVoiceStore store = open(directory)) {
            initialize(store); store.append(chunk(1, 16)); store.seal(SOURCE.sourceId(), 20000, 0);
            code("VOICE_SOURCE_SEAL_RECEIPT_RANGE", () -> store.markServerSealed(SOURCE.sourceId()));
            store.markServerCommitted(new VoiceAudioV2.DurablePrefix(SOURCE.sourceId(), 1));
        }
        try (NativeVoiceStore store = open(directory)) {
            // A death after the chunk ACK must still leave a source_seal to recover.
            assertTrue(store.sources().get(0).sealed()); assertFalse(store.sources().get(0).serverSealed());
            store.markServerSealed(SOURCE.sourceId()); store.markServerSealed(SOURCE.sourceId());
        }
        try (NativeVoiceStore store = open(directory)) { assertTrue(store.sources().get(0).serverSealed()); }
    }
}
