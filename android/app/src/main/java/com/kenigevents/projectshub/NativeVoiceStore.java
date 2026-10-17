package com.kenigevents.projectshub;

import org.onedayonemasterpiece.live.DurableSourceStore;
import org.onedayonemasterpiece.live.DurableOutputStore;
import org.onedayonemasterpiece.live.VoiceAudioV2;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.DataInputStream;
import java.io.DataOutputStream;
import java.io.File;
import java.io.IOException;
import java.io.RandomAccessFile;
import java.nio.channels.FileChannel;
import java.nio.channels.FileLock;
import java.nio.channels.OverlappingFileLockException;
import java.nio.file.Files;
import java.nio.file.StandardOpenOption;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Product-owned, bounded persistence of the SDK's original compressed sources.
 * Use a private no-backup directory. No microphone, decoder or transport lives
 * here. A journal record and its identity are synced before a receipt escapes.
 */
public final class NativeVoiceStore implements DurableSourceStore, DurableOutputStore {
    private static final int MAGIC = 0x50485632;
    private static final int FORMAT = 1;
    private static final int MAX_RECORD_BYTES = VoiceAudioV2.MAX_ENCODED_BYTES
            + VoiceAudioV2.MAX_INIT_BYTES + 2048;
    private static final int SOURCE = 1, EPOCH = 2, CHUNK = 3, COMMIT = 4, SEAL = 5, DISPOSITION = 6,
            OUTPUT_SOURCE = 7, OUTPUT_CHUNK = 8, PLAYED = 9, SERVER_SEALED = 10;

    public record Scope(String actorId, String workspaceId, String conversationId) {
        public Scope {
            VoiceAudioV2.id(actorId);
            VoiceAudioV2.id(workspaceId);
            VoiceAudioV2.id(conversationId);
        }
    }

    public record Limits(long maxJournalBytes, int maxSources, long maxSourceDurationUs) {
        public Limits {
            if (maxJournalBytes < MAX_RECORD_BYTES || maxSources < 1 || maxSourceDurationUs < 1)
                throw new IllegalArgumentException("VOICE_STORE_LIMITS");
        }
    }

    public static final class StoreException extends IOException {
        private final String code;
        StoreException(String code) { super(code); this.code = code; }
        public String code() { return code; }
    }

    private record ChunkIndex(long bodyOffset, int bodyBytes, int encodedBytes, int durationUs) {}

    private static final class Source {
        final File path;
        RandomAccessFile journal;
        VoiceAudioV2.SourceDescriptor descriptor;
        final Map<String, VoiceAudioV2.CodecEpoch> epochs = new LinkedHashMap<>();
        final List<ChunkIndex> chunks = new ArrayList<>();
        long committed, validEndUs, capturedDurationUs, committedDurationUs, playedUs, originalEventSeq;
        int endTrimSamples;
        boolean sealed, serverSealed, output;
        String disposition;

        Source(File path) throws IOException {
            this.path = path;
            journal = new RandomAccessFile(path, "rw");
        }
        SourceStatus status() {
            return new SourceStatus(descriptor, chunks.size(), committed, sealed, serverSealed,
                    validEndUs, endTrimSamples, disposition);
        }
    }

    @FunctionalInterface private interface Writer { void write(DataOutputStream out) throws IOException; }

    private final File root, scopeDirectory;
    private final Scope scope;
    private final Limits limits;
    private final Map<String, Source> sources = new LinkedHashMap<>();
    private Source activeJournal;
    private RandomAccessFile lockFile;
    private FileLock lock;
    private long journalBytes;
    private int sourceCount;
    private boolean closed, failed;

    public NativeVoiceStore(File root, Scope scope, Limits limits) throws IOException {
        this.root = root.getCanonicalFile();
        this.scope = scope;
        this.limits = limits;
        ensureDirectory(this.root);
        try {
            lockFile = new RandomAccessFile(new File(this.root, "owner.lock"), "rw");
            try { lock = lockFile.getChannel().tryLock(); }
            catch (OverlappingFileLockException busy) { throw error("VOICE_STORE_IN_USE"); }
            if (lock == null) throw error("VOICE_STORE_IN_USE");
            byte[] scopeBytes = body(SOURCE, out -> writeScope(out, scope));
            scopeDirectory = new File(this.root, VoiceAudioV2.sha256(scopeBytes));
            ensureDirectory(scopeDirectory);
            File[] files = scopeDirectory.listFiles((dir, name) -> name.endsWith(".wvs"));
            if (files == null) throw error("VOICE_STORE_UNAVAILABLE");
            Arrays.sort(files);
            for (File path : files) {
                Source source = new Source(path);
                try {
                    recover(source);
                    if (source.descriptor == null) {
                        source.journal.close();
                        Files.delete(path.toPath()); // no complete, receipted source header
                        syncDirectory(scopeDirectory);
                        continue;
                    }
                    if (!path.getName().equals(sourceName(source.descriptor.sourceId()))
                            || sources.put(source.descriptor.sourceId(), source) != null)
                        throw error("VOICE_STORE_CORRUPT");
                    source.journal.close();
                    source.journal = null;
                } catch (IOException | RuntimeException failure) {
                    if (source.journal != null) source.journal.close();
                    throw failure;
                }
            }
            // Quota covers every retained account/conversation, not only this scope.
            File[] directories = this.root.listFiles(File::isDirectory);
            if (directories == null) throw error("VOICE_STORE_UNAVAILABLE");
            for (File directory : directories) {
                File[] retained = directory.listFiles((dir, name) -> name.endsWith(".wvs"));
                if (retained == null) throw error("VOICE_STORE_UNAVAILABLE");
                for (File path : retained) { journalBytes += path.length(); sourceCount++; }
            }
        } catch (IOException | RuntimeException failure) {
            close();
            throw failure;
        }
    }

    @Override public synchronized List<SourceStatus> sources() throws IOException {
        checkOpen();
        List<SourceStatus> result = new ArrayList<>();
        for (Source source : sources.values()) if (!source.output) result.add(source.status());
        return result;
    }

    /** UI projection from metadata; never rereads or decodes the queued audio. */
    public synchronized long pendingDurationUs() throws IOException {
        checkOpen();
        long duration = 0;
        for (Source source : sources.values()) if (!source.output)
            duration += source.capturedDurationUs - source.committedDurationUs;
        return duration;
    }

    @Override public synchronized SourceStatus open(VoiceAudioV2.SourceDescriptor descriptor)
            throws IOException {
        return openSource(descriptor, false).status();
    }

    @Override public synchronized void openOutput(VoiceAudioV2.SourceDescriptor descriptor)
            throws IOException {
        openSource(descriptor, true);
    }

    private Source openSource(VoiceAudioV2.SourceDescriptor descriptor, boolean output) throws IOException {
        checkOpen();
        Source existing = sources.get(descriptor.sourceId());
        if (existing != null) {
            if (existing.output != output || !existing.descriptor.equals(descriptor)) throw error("VOICE_SOURCE_CONFLICT");
            return existing;
        }
        if (sourceCount >= limits.maxSources()) throw error("VOICE_STORE_QUOTA");
        byte[] record = body(output ? OUTPUT_SOURCE : SOURCE, out -> {
            out.writeInt(FORMAT);
            writeScope(out, scope);
            out.writeUTF(descriptor.sourceId());
            out.writeLong(descriptor.startedAtUnixMs());
        });
        checkCapacity(record.length + 36L);
        Source source = new Source(new File(scopeDirectory, sourceName(descriptor.sourceId())));
        try {
            appendRecord(source, record);
            syncDirectory(scopeDirectory);
            sources.put(descriptor.sourceId(), source);
            sourceCount++;
            return source;
        } catch (IOException | RuntimeException failure) {
            source.journal.close();
            failed = true;
            throw failure;
        }
    }

    @Override public synchronized void registerEpoch(String sourceId, VoiceAudioV2.CodecEpoch epoch)
            throws IOException {
        registerEpoch(requireSource(sourceId, false), epoch);
    }

    @Override public synchronized void registerOutputEpoch(String sourceId, VoiceAudioV2.CodecEpoch epoch)
            throws IOException {
        registerEpoch(requireSource(sourceId, true), epoch);
    }

    private void registerEpoch(Source source, VoiceAudioV2.CodecEpoch epoch) throws IOException {
        VoiceAudioV2.CodecEpoch existing = source.epochs.get(epoch.epochId());
        if (existing != null) {
            if (!existing.equals(epoch)) throw error("VOICE_SOURCE_CONFLICT");
            return;
        }
        if (source.sealed) throw error("VOICE_SOURCE_SEALED");
        if (source.output && !source.epochs.isEmpty()) throw error("VOICE_SOURCE_CONFLICT");
        appendRecord(source, body(EPOCH, out -> writeEpoch(out, epoch)));
    }

    @Override public synchronized VoiceAudioV2.CodecEpoch epoch(String sourceId, String epochId)
            throws IOException {
        return epoch(requireSource(sourceId, false), epochId);
    }

    @Override public synchronized VoiceAudioV2.CodecEpoch outputEpoch(String sourceId, String epochId)
            throws IOException {
        return epoch(requireSource(sourceId, true), epochId);
    }

    private VoiceAudioV2.CodecEpoch epoch(Source source, String epochId) throws IOException {
        VoiceAudioV2.CodecEpoch result = source.epochs.get(epochId);
        if (result == null) throw error("VOICE_EPOCH_UNKNOWN");
        return result;
    }

    @Override public synchronized VoiceAudioV2.DurablePrefix append(VoiceAudioV2.EncodedChunk chunk)
            throws IOException {
        return appendChunk(requireSource(chunk.sourceId(), false), chunk, 0);
    }

    @Override public synchronized VoiceAudioV2.DurablePrefix appendOutput(VoiceAudioV2.OutputChunk output)
            throws IOException {
        VoiceAudioV2.EncodedChunk chunk = output.chunk();
        return appendChunk(requireSource(chunk.sourceId(), true), chunk, output.originalEventSeq());
    }

    private VoiceAudioV2.DurablePrefix appendChunk(Source source, VoiceAudioV2.EncodedChunk chunk,
            long originalEventSeq) throws IOException {
        if (chunk.chunkSeq() <= source.chunks.size()) {
            int index = (int) chunk.chunkSeq() - 1;
            if (!readChunk(source, index).equals(chunk)
                    || source.output && readOutputChunk(source, index).originalEventSeq() != originalEventSeq)
                throw error("VOICE_SOURCE_CONFLICT");
            return new VoiceAudioV2.DurablePrefix(chunk.sourceId(), source.chunks.size());
        }
        if (source.sealed) throw error("VOICE_SOURCE_SEALED");
        if (!source.epochs.containsKey(chunk.epochId())) throw error("VOICE_EPOCH_UNKNOWN");
        if (chunk.chunkSeq() != source.chunks.size() + 1L) throw error("VOICE_SOURCE_GAP");
        if (chunk.captureStartUs() < source.validEndUs
                || chunk.captureStartUs() > VoiceAudioV2.MAX_SAFE_INTEGER - chunk.durationUs())
            throw error("VOICE_SOURCE_TIME");
        if (source.capturedDurationUs > limits.maxSourceDurationUs() - chunk.durationUs())
            throw error("VOICE_STORE_QUOTA");
        if (source.output && originalEventSeq < source.originalEventSeq) throw error("VOICE_SOURCE_EVENT_ORDER");
        appendRecord(source, body(source.output ? OUTPUT_CHUNK : CHUNK, out -> {
            writeChunk(out, chunk);
            if (source.output) out.writeLong(originalEventSeq);
        }));
        return new VoiceAudioV2.DurablePrefix(chunk.sourceId(), source.chunks.size());
    }

    @Override public synchronized List<VoiceAudioV2.EncodedChunk> readPending(
            String sourceId, long afterChunkSeq, int maxEncodedBytes, long maxDurationUs)
            throws IOException {
        Source source = requireSource(sourceId, false);
        if (afterChunkSeq < 0 || afterChunkSeq > source.chunks.size()
                || maxEncodedBytes < 1 || maxDurationUs < 1) throw error("VOICE_SOURCE_READ_RANGE");
        List<VoiceAudioV2.EncodedChunk> result = new ArrayList<>();
        long bytes = 0, duration = 0;
        for (int i = (int) Math.max(afterChunkSeq, source.committed); i < source.chunks.size(); i++) {
            ChunkIndex index = source.chunks.get(i);
            if (bytes + index.encodedBytes() > maxEncodedBytes
                    || duration + index.durationUs() > maxDurationUs) break;
            result.add(readChunk(source, i));
            bytes += index.encodedBytes();
            duration += index.durationUs();
        }
        return result;
    }

    @Override public synchronized List<VoiceAudioV2.OutputChunk> readOutput(String sourceId,
            long afterChunkSeq, int maxEncodedBytes, long maxDurationUs) throws IOException {
        Source source = requireSource(sourceId, true);
        if (afterChunkSeq < 0 || afterChunkSeq > source.chunks.size()
                || maxEncodedBytes < 1 || maxDurationUs < 1) throw error("VOICE_SOURCE_READ_RANGE");
        List<VoiceAudioV2.OutputChunk> result = new ArrayList<>();
        long bytes = 0, duration = 0;
        for (int i = (int) afterChunkSeq; i < source.chunks.size(); i++) {
            ChunkIndex index = source.chunks.get(i);
            if (bytes + index.encodedBytes() > maxEncodedBytes
                    || duration + index.durationUs() > maxDurationUs) break;
            result.add(readOutputChunk(source, i));
            bytes += index.encodedBytes(); duration += index.durationUs();
        }
        return result;
    }

    @Override public synchronized void markPlayed(String sourceId, long playedUs) throws IOException {
        Source source = requireSource(sourceId, true);
        if (playedUs < 0 || playedUs > source.validEndUs) throw error("VOICE_OUTPUT_PLAYED_RANGE");
        if (playedUs > source.playedUs) appendRecord(source, body(PLAYED, out -> out.writeLong(playedUs)));
    }

    @Override public synchronized List<PlaybackPosition> playbackPositions() throws IOException {
        checkOpen();
        List<PlaybackPosition> result = new ArrayList<>();
        for (Source source : sources.values()) if (source.output)
            result.add(new PlaybackPosition(source.descriptor, source.chunks.size(), source.originalEventSeq,
                    source.playedUs, source.validEndUs, source.sealed));
        return result;
    }

    @Override public synchronized VoiceAudioV2.DurablePrefix markServerCommitted(
            VoiceAudioV2.DurablePrefix prefix) throws IOException {
        Source source = requireSource(prefix.sourceId(), false);
        if (prefix.chunkSeq() > source.chunks.size()) throw error("VOICE_SOURCE_RECEIPT_RANGE");
        if (prefix.chunkSeq() > source.committed)
            appendRecord(source, body(COMMIT, out -> out.writeLong(prefix.chunkSeq())));
        return new VoiceAudioV2.DurablePrefix(prefix.sourceId(), source.committed);
    }

    @Override public synchronized void seal(String sourceId, long validEndUs, int endTrimSamples)
            throws IOException {
        Source source = requireSource(sourceId, false);
        if (validEndUs != source.validEndUs || endTrimSamples < 0 || endTrimSamples > 65535)
            throw error("VOICE_SOURCE_SEAL_RANGE");
        if (source.sealed) {
            if (source.endTrimSamples != endTrimSamples) throw error("VOICE_SOURCE_CONFLICT");
            return;
        }
        appendRecord(source, body(SEAL, out -> {
            out.writeLong(validEndUs);
            out.writeInt(endTrimSamples);
        }));
    }

    @Override public synchronized void disposition(String sourceId, String disposition) throws IOException {
        Source source = requireSource(sourceId, false);
        if (!source.sealed || disposition == null || !disposition.matches("[a-z][a-z0-9_]{0,63}"))
            throw error("VOICE_SOURCE_DISPOSITION");
        if (source.disposition != null) {
            if (!source.disposition.equals(disposition)) throw error("VOICE_SOURCE_CONFLICT");
            return;
        }
        appendRecord(source, body(DISPOSITION, out -> out.writeUTF(disposition)));
    }

    @Override public synchronized void markServerSealed(String sourceId) throws IOException {
        Source source = requireSource(sourceId, false);
        if (!source.sealed || source.committed != source.chunks.size()) throw error("VOICE_SOURCE_SEAL_RECEIPT_RANGE");
        if (!source.serverSealed) appendRecord(source, body(SERVER_SEALED, out -> {}));
    }

    @Override public synchronized void release(String sourceId) throws IOException {
        checkOpen();
        Source source = sources.get(sourceId);
        if (source == null) return;
        if (source.output || !source.sealed || !source.serverSealed
                || source.committed != source.chunks.size() || source.disposition == null)
            throw error("VOICE_SOURCE_RETAINED");
        activateJournal(source);
        long size = source.journal.length();
        source.journal.close();
        source.journal = null;
        activeJournal = null;
        try {
            Files.delete(source.path.toPath());
            syncDirectory(scopeDirectory);
            sources.remove(sourceId);
            journalBytes -= size;
            sourceCount--;
        } catch (IOException failure) {
            failed = true;
            throw failure;
        }
    }

    private Source requireSource(String sourceId, boolean output) throws IOException {
        checkOpen();
        Source source = sources.get(sourceId);
        if (source == null) throw error("VOICE_SOURCE_UNKNOWN");
        if (source.output != output) throw error("VOICE_SOURCE_DIRECTION");
        activateJournal(source);
        return source;
    }

    private void activateJournal(Source source) throws IOException {
        if (activeJournal != null && activeJournal != source) {
            activeJournal.journal.close();
            activeJournal.journal = null;
            activeJournal = null;
        }
        if (source.journal == null) source.journal = new RandomAccessFile(source.path, "rw");
        activeJournal = source;
    }

    private void checkOpen() throws IOException {
        if (closed) throw error("VOICE_STORE_CLOSED");
        if (failed) throw error("VOICE_STORE_UNAVAILABLE");
    }

    private void checkCapacity(long bytes) throws IOException {
        if (bytes > limits.maxJournalBytes() - journalBytes) throw error("VOICE_STORE_QUOTA");
    }

    private void appendRecord(Source source, byte[] body) throws IOException {
        checkCapacity(body.length + 36L);
        activateJournal(source);
        long end = source.journal.length();
        try {
            source.journal.seek(end);
            source.journal.writeInt(body.length);
            source.journal.write(body);
            source.journal.write(digest(body));
            source.journal.getFD().sync();
            // The in-memory cursor cannot outrun its on-device receipt.
            applyRecord(source, body, end + 4);
            journalBytes += body.length + 36L;
        } catch (IOException | RuntimeException failure) {
            failed = true; // uncertain write: reopen/recover before another append
            throw failure;
        }
    }

    private void recover(Source source) throws IOException {
        long size = source.journal.length(), cursor = 0;
        while (cursor < size) {
            source.journal.seek(cursor);
            if (size - cursor < 4) { truncateTail(source, cursor); break; }
            int length = source.journal.readInt();
            if (length < 5 || length > MAX_RECORD_BYTES) throw error("VOICE_STORE_CORRUPT");
            if (size - cursor - 4 < length + 32L) { truncateTail(source, cursor); break; }
            byte[] record = new byte[length], expected = new byte[32];
            source.journal.readFully(record);
            source.journal.readFully(expected);
            if (!MessageDigest.isEqual(digest(record), expected)) throw error("VOICE_STORE_CORRUPT");
            try { applyRecord(source, record, cursor + 4); }
            catch (IllegalArgumentException failure) { throw error("VOICE_STORE_CORRUPT"); }
            cursor += length + 36L;
        }
    }

    private static void truncateTail(Source source, long length) throws IOException {
        source.journal.setLength(length);
        source.journal.getFD().sync();
    }

    private void applyRecord(Source source, byte[] record, long bodyOffset) throws IOException {
        DataInputStream in = new DataInputStream(new ByteArrayInputStream(record));
        if (in.readInt() != MAGIC) throw error("VOICE_STORE_CORRUPT");
        int type = in.readUnsignedByte();
        if (source.descriptor == null && type != SOURCE && type != OUTPUT_SOURCE) throw error("VOICE_STORE_CORRUPT");
        switch (type) {
            case SOURCE, OUTPUT_SOURCE -> {
                if (source.descriptor != null || in.readInt() != FORMAT || !readScope(in).equals(scope))
                    throw error("VOICE_STORE_CORRUPT");
                source.descriptor = new VoiceAudioV2.SourceDescriptor(in.readUTF(), in.readLong());
                source.output = type == OUTPUT_SOURCE;
            }
            case EPOCH -> {
                VoiceAudioV2.CodecEpoch epoch = readEpoch(in);
                if (source.sealed || source.output && !source.epochs.isEmpty()
                        || source.epochs.put(epoch.epochId(), epoch) != null)
                    throw error("VOICE_STORE_CORRUPT");
            }
            case CHUNK, OUTPUT_CHUNK -> {
                VoiceAudioV2.EncodedChunk chunk = readChunk(in);
                long eventSeq = type == OUTPUT_CHUNK ? in.readLong() : 0;
                if (source.output != (type == OUTPUT_CHUNK) || source.sealed
                        || !chunk.sourceId().equals(source.descriptor.sourceId())
                        || !source.epochs.containsKey(chunk.epochId())
                        || chunk.chunkSeq() != source.chunks.size() + 1L
                        || chunk.captureStartUs() < source.validEndUs
                        || chunk.captureStartUs() > VoiceAudioV2.MAX_SAFE_INTEGER - chunk.durationUs())
                    throw error("VOICE_STORE_CORRUPT");
                source.chunks.add(new ChunkIndex(bodyOffset, record.length, chunk.encodedBytes(), chunk.durationUs()));
                source.validEndUs = chunk.captureStartUs() + chunk.durationUs();
                source.capturedDurationUs += chunk.durationUs();
                if (source.output) {
                    new VoiceAudioV2.OutputChunk(chunk, eventSeq); // validate the canonical output identity
                    if (eventSeq < source.originalEventSeq) throw error("VOICE_STORE_CORRUPT");
                    source.originalEventSeq = eventSeq;
                    source.sealed = (chunk.flags() & VoiceAudioV2.EPOCH_END) != 0;
                }
            }
            case COMMIT -> {
                long through = in.readLong();
                if (source.output || through < source.committed || through > source.chunks.size()) throw error("VOICE_STORE_CORRUPT");
                for (int i = (int) source.committed; i < through; i++)
                    source.committedDurationUs += source.chunks.get(i).durationUs();
                source.committed = through;
            }
            case SEAL -> {
                long validEnd = in.readLong();
                int trim = in.readInt();
                if (source.output || source.sealed || validEnd != source.validEndUs || trim < 0 || trim > 65535)
                    throw error("VOICE_STORE_CORRUPT");
                source.sealed = true;
                source.endTrimSamples = trim;
            }
            case DISPOSITION -> {
                String disposition = in.readUTF();
                if (source.output || !source.sealed || source.disposition != null || !disposition.matches("[a-z][a-z0-9_]{0,63}"))
                    throw error("VOICE_STORE_CORRUPT");
                source.disposition = disposition;
            }
            case PLAYED -> {
                long played = in.readLong();
                if (!source.output || played < source.playedUs || played > source.validEndUs)
                    throw error("VOICE_STORE_CORRUPT");
                source.playedUs = played;
            }
            case SERVER_SEALED -> {
                if (source.output || !source.sealed || source.serverSealed || source.committed != source.chunks.size())
                    throw error("VOICE_STORE_CORRUPT");
                source.serverSealed = true;
            }
            default -> throw error("VOICE_STORE_CORRUPT");
        }
        if (in.available() != 0) throw error("VOICE_STORE_CORRUPT");
    }

    private VoiceAudioV2.EncodedChunk readChunk(Source source, int index) throws IOException {
        DataInputStream in = readChunkRecord(source, index);
        VoiceAudioV2.EncodedChunk chunk = readChunk(in);
        if (source.output) new VoiceAudioV2.OutputChunk(chunk, in.readLong());
        if (in.available() != 0) throw error("VOICE_STORE_CORRUPT");
        return chunk;
    }

    private VoiceAudioV2.OutputChunk readOutputChunk(Source source, int index) throws IOException {
        DataInputStream in = readChunkRecord(source, index);
        VoiceAudioV2.OutputChunk chunk = new VoiceAudioV2.OutputChunk(readChunk(in), in.readLong());
        if (in.available() != 0) throw error("VOICE_STORE_CORRUPT");
        return chunk;
    }

    private DataInputStream readChunkRecord(Source source, int index) throws IOException {
        ChunkIndex position = source.chunks.get(index);
        source.journal.seek(position.bodyOffset());
        byte[] record = new byte[position.bodyBytes()], expected = new byte[32];
        source.journal.readFully(record);
        source.journal.readFully(expected);
        if (!MessageDigest.isEqual(digest(record), expected)) throw error("VOICE_STORE_CORRUPT");
        DataInputStream in = new DataInputStream(new ByteArrayInputStream(record));
        if (in.readInt() != MAGIC || in.readUnsignedByte() != (source.output ? OUTPUT_CHUNK : CHUNK))
            throw error("VOICE_STORE_CORRUPT");
        return in;
    }

    private static byte[] body(int type, Writer writer) throws IOException {
        ByteArrayOutputStream bytes = new ByteArrayOutputStream();
        try (DataOutputStream out = new DataOutputStream(bytes)) {
            out.writeInt(MAGIC);
            out.writeByte(type);
            writer.write(out);
        }
        if (bytes.size() > MAX_RECORD_BYTES) throw error("VOICE_STORE_RECORD_LIMIT");
        return bytes.toByteArray();
    }

    private static void writeScope(DataOutputStream out, Scope scope) throws IOException {
        out.writeUTF(scope.actorId()); out.writeUTF(scope.workspaceId()); out.writeUTF(scope.conversationId());
    }
    private static Scope readScope(DataInputStream in) throws IOException {
        return new Scope(in.readUTF(), in.readUTF(), in.readUTF());
    }
    private static void writeEpoch(DataOutputStream out, VoiceAudioV2.CodecEpoch epoch) throws IOException {
        out.writeUTF(epoch.epochId()); out.writeUTF(epoch.codec()); out.writeUTF(epoch.container());
        out.writeInt(epoch.version()); out.writeInt(epoch.channels()); out.writeInt(epoch.decodedSampleRate());
        out.writeInt(epoch.clockRate()); out.writeInt(epoch.preSkip());
        byte[] init = epoch.init(); out.writeInt(init.length); out.write(init);
    }
    private static VoiceAudioV2.CodecEpoch readEpoch(DataInputStream in) throws IOException {
        String id = in.readUTF(), codec = in.readUTF(), container = in.readUTF();
        int version = in.readInt(), channels = in.readInt(), rate = in.readInt(), clock = in.readInt(), skip = in.readInt();
        return new VoiceAudioV2.CodecEpoch(id, codec, container, version, channels, rate, clock, skip,
                readBytes(in, VoiceAudioV2.MAX_INIT_BYTES));
    }
    private static void writeChunk(DataOutputStream out, VoiceAudioV2.EncodedChunk chunk) throws IOException {
        out.writeUTF(chunk.sourceId()); out.writeUTF(chunk.epochId()); out.writeLong(chunk.chunkSeq());
        out.writeLong(chunk.captureStartUs()); out.writeInt(chunk.durationUs()); out.writeInt(chunk.flags());
        byte[] payload = chunk.payload(); out.writeInt(payload.length); out.write(payload);
    }
    private static VoiceAudioV2.EncodedChunk readChunk(DataInputStream in) throws IOException {
        return new VoiceAudioV2.EncodedChunk(in.readUTF(), in.readUTF(), in.readLong(), in.readLong(),
                in.readInt(), in.readInt(), readBytes(in, VoiceAudioV2.MAX_ENCODED_BYTES));
    }
    private static byte[] readBytes(DataInputStream in, int limit) throws IOException {
        int length = in.readInt();
        if (length < 0 || length > limit || length > in.available()) throw error("VOICE_STORE_CORRUPT");
        byte[] bytes = new byte[length]; in.readFully(bytes); return bytes;
    }
    private static byte[] digest(byte[] value) {
        try { return MessageDigest.getInstance("SHA-256").digest(value); }
        catch (NoSuchAlgorithmException impossible) { throw new IllegalStateException(impossible); }
    }
    private static String sourceName(String sourceId) {
        VoiceAudioV2.id(sourceId);
        return VoiceAudioV2.sha256(sourceId.getBytes(java.nio.charset.StandardCharsets.UTF_8)) + ".wvs";
    }
    private static void ensureDirectory(File directory) throws IOException {
        if (directory.isDirectory()) return;
        File parent = directory.getParentFile();
        if (parent != null) ensureDirectory(parent);
        Files.createDirectory(directory.toPath());
        if (parent != null) syncDirectory(parent);
    }
    private static void syncDirectory(File directory) throws IOException {
        try (FileChannel channel = FileChannel.open(directory.toPath(), StandardOpenOption.READ)) {
            channel.force(true);
        }
    }
    private static StoreException error(String code) { return new StoreException(code); }

    @Override public synchronized void close() throws IOException {
        if (closed) return;
        closed = true;
        IOException first = null;
        for (Source source : sources.values()) {
            try { if (source.journal != null) source.journal.close(); }
            catch (IOException failure) { if (first == null) first = failure; }
        }
        try { if (lock != null) lock.release(); } catch (IOException failure) { if (first == null) first = failure; }
        try { if (lockFile != null) lockFile.close(); } catch (IOException failure) { if (first == null) first = failure; }
        if (first != null) throw first;
    }
}
