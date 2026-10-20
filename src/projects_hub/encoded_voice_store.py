"""Projects Hub's durable, actor-bound compressed source store.

The shared framework owns codecs and wire framing. This adapter commits original
bytes and their immutable metadata in the existing SQLite/WAL transaction. There
is no second audio file to fsync and no permanent decoded PCM copy. The receipt
means durability only; source delivery never grants permission to run a command.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any


log = logging.getLogger("projects_hub.live")


def initialize_encoded_voice_schema(db) -> None:
    db.executescript("""
        CREATE TABLE IF NOT EXISTS encoded_voice_sources(
            source_id TEXT PRIMARY KEY REFERENCES sources(id),
            started_at_unix_ms INTEGER NOT NULL,
            contiguous_seq INTEGER NOT NULL DEFAULT 0,
            sealed_seq INTEGER,
            capture_end_us INTEGER,
            complete INTEGER NOT NULL DEFAULT 0,
            played_us INTEGER NOT NULL DEFAULT 0,
            output_cancelled INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS encoded_voice_epochs(
            source_id TEXT NOT NULL REFERENCES sources(id),
            epoch_id TEXT NOT NULL,
            descriptor_json TEXT NOT NULL,
            descriptor_sha256 TEXT NOT NULL,
            end_seq INTEGER,
            PRIMARY KEY(source_id,epoch_id)
        );
        CREATE TABLE IF NOT EXISTS encoded_voice_chunks(
            source_id TEXT NOT NULL REFERENCES sources(id),
            chunk_seq INTEGER NOT NULL,
            epoch_id TEXT NOT NULL,
            capture_start_us INTEGER NOT NULL,
            duration_us INTEGER NOT NULL,
            flags INTEGER NOT NULL,
            payload_sha256 TEXT NOT NULL,
            payload BLOB NOT NULL,
            delivery_state TEXT NOT NULL DEFAULT 'stored',
            projected_bytes INTEGER NOT NULL DEFAULT 0,
            original_event_seq INTEGER,
            PRIMARY KEY(source_id,chunk_seq),
            FOREIGN KEY(source_id,epoch_id)
                REFERENCES encoded_voice_epochs(source_id,epoch_id)
        );
        CREATE TABLE IF NOT EXISTS encoded_voice_storage(
            slot INTEGER PRIMARY KEY CHECK(slot=1),
            encoded_bytes INTEGER NOT NULL DEFAULT 0,
            chunks INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS encoded_voice_epoch_chunks_idx
            ON encoded_voice_chunks(source_id,epoch_id,chunk_seq);
        CREATE TABLE IF NOT EXISTS encoded_voice_projections(
            source_id TEXT NOT NULL,
            chunk_seq INTEGER NOT NULL,
            capture_start_us INTEGER NOT NULL,
            end_us INTEGER NOT NULL,
            sample_count INTEGER NOT NULL,
            pcm_sha256 TEXT NOT NULL,
            PRIMARY KEY(source_id,chunk_seq,capture_start_us,sample_count),
            FOREIGN KEY(source_id,chunk_seq)
                REFERENCES encoded_voice_chunks(source_id,chunk_seq)
        );
        CREATE INDEX IF NOT EXISTS encoded_voice_projection_end_idx
            ON encoded_voice_projections(source_id,end_us);
        INSERT OR IGNORE INTO encoded_voice_storage(slot) VALUES(1);
    """)
    # Candidate databases can be reopened across additive codec-store revisions.
    for table, additions in {
        "encoded_voice_sources": {"played_us": "INTEGER NOT NULL DEFAULT 0",
                                  "output_cancelled": "INTEGER NOT NULL DEFAULT 0"},
        "encoded_voice_chunks": {"original_event_seq": "INTEGER"},
    }.items():
        columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        for column, definition in additions.items():
            if column not in columns:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


@dataclass(frozen=True)
class EncodedStorageLimits:
    # Initial candidate bounds, not a claim about server capacity. Existing
    # originals remain stored when a bound is reached; capture must surface full.
    source_bytes: int = 64 * 1024 * 1024
    total_bytes: int = 2 * 1024 * 1024 * 1024
    source_duration_us: int = 8 * 60 * 60 * 1_000_000
    # Native Opus emits as little as one original chunk per 20 ms. A single
    # 90-minute uninterrupted source may therefore contain 270,000 chunks;
    # the former 250,000 ceiling guaranteed failure around minute 83.
    # This remains bounded by the 4M total, 64 MiB per-source, and duration.
    source_chunks: int = 400_000
    total_chunks: int = 4_000_000
    epochs_per_source: int = 32

    def __post_init__(self):
        if any(type(value) is not int or value < 1 for value in vars(self).values()):
            raise ValueError("Encoded source bounds must be positive integers")


class EncodedVoiceIO:
    """One bounded disk lane shared by all voice sessions of this product host."""

    def __init__(self, *, workers: int = 2, pending: int = 8):
        self.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="voice-source")
        self.slots = asyncio.Semaphore(pending)
        self.closed = False

    async def run(self, operation, *args):
        if self.closed:
            raise RuntimeError("Encoded voice storage is closed")
        await self.slots.acquire()
        try:
            future = asyncio.get_running_loop().run_in_executor(self.executor, operation, *args)
        except BaseException:
            self.slots.release()
            raise

        def finished(done):
            self.slots.release()
            # If the waiter was cancelled, still consume a storage error. The
            # device retains its unacknowledged bytes and queries the cursor.
            if not done.cancelled():
                done.exception()

        future.add_done_callback(finished)
        # Cancellation cannot cancel an in-flight SQLite commit or falsely free
        # its queue slot. The actual completion callback owns the slot release.
        return await asyncio.shield(future)

    def close(self):
        self.closed = True
        self.executor.shutdown(wait=True, cancel_futures=False)


class ProductEncodedAudioStore:
    """Shared EncodedAudioStore contract bound to authenticated product identity."""

    def __init__(self, store, *, actor_id: str, conversation_id: str,
                 io: EncodedVoiceIO, limits: EncodedStorageLimits | None = None,
                 direction: str = "uplink", output_session_id: str | None = None):
        if direction not in {"uplink", "downlink"}:
            raise ValueError("Unknown compressed source direction")
        if output_session_id is not None and (type(output_session_id) is not str
                or re.fullmatch(r"[A-Za-z0-9_-]{8,128}", output_session_id) is None):
            raise ValueError("Invalid Live output session identity")
        self.store = store
        self.actor_id = actor_id
        self.conversation_id = conversation_id
        self.io = io
        self.limits = limits or EncodedStorageLimits()
        self.direction = direction
        self.output_session_id = output_session_id

    def _source(self, source_id):
        from .store import StoreError
        source = self.store.get_source(self.actor_id, source_id)
        if source["conversation_id"] != self.conversation_id:
            raise StoreError("FORBIDDEN", "Compressed source belongs to another conversation")
        return source

    def _bound(self, bound):
        from .store import StoreError
        source = self._source(bound.server_source_id)
        if (source.get("client_source_id") != bound.source.source_id or source.get("audio_format") != "encoded-v2"
                or source.get("audio_direction") != self.direction):
            raise StoreError("SOURCE_BINDING", "Compressed source binding does not match")
        return source

    def _complete(self, source_id: str, prefix: int, last_seq, capture_end_us) -> bool:
        if last_seq is None or prefix != last_seq:
            return False
        if last_seq == 0:
            return capture_end_us == 0
        last = self.store.db.execute(
            "SELECT capture_start_us+duration_us AS end_us FROM encoded_voice_chunks WHERE source_id=? AND chunk_seq=?",
            (source_id, last_seq),
        ).fetchone()
        unfinished = self.store.db.execute(
            """SELECT 1 FROM encoded_voice_epochs e WHERE e.source_id=?
               AND e.end_seq IS NULL AND EXISTS(
                   SELECT 1 FROM encoded_voice_chunks c
                   WHERE c.source_id=e.source_id AND c.epoch_id=e.epoch_id) LIMIT 1""",
            (source_id,),
        ).fetchone()
        return bool(last and last["end_us"] == capture_end_us and not unfinished)

    async def open_source(self, descriptor):
        return await self.io.run(self._open_source, descriptor)

    def _open_source(self, descriptor):
        from live_interaction.audio_v2 import BoundSource, SourceDescriptor
        from .store import StoreError
        if not isinstance(descriptor, SourceDescriptor):
            raise StoreError("SOURCE_METADATA", "Typed source descriptor is required")
        with self.store._lock:
            source = self.store.create_source(self.actor_id, self.conversation_id, descriptor.source_id)
            row = self.store.db.execute("SELECT * FROM encoded_voice_sources WHERE source_id=?", (source["id"],)).fetchone()
            if row:
                if row["started_at_unix_ms"] != descriptor.started_at_unix_ms:
                    raise StoreError("SOURCE_CONFLICT", "Original capture time is immutable")
                if source.get("audio_direction") != self.direction:
                    raise StoreError("SOURCE_BINDING", "A source cannot change its capture direction")
                return BoundSource(descriptor, source["id"], row["contiguous_seq"])
            if source["audio_bytes"] or source["audio_chunks"] or source["status"] in {"archived", "ephemeral_processed"}:
                raise StoreError("SOURCE_AUDIO_FORMAT", "An existing source cannot change its stored format")
            self.store.db.execute("BEGIN IMMEDIATE")
            try:
                self.store.db.execute("INSERT INTO encoded_voice_sources(source_id,started_at_unix_ms) VALUES(?,?)", (source["id"], descriptor.started_at_unix_ms))
                self.store.db.execute("UPDATE sources SET audio_format='encoded-v2',audio_direction=?,audio_path='',captured_at_ms=? WHERE id=?", (self.direction, descriptor.started_at_unix_ms, source["id"]))
                self.store.db.execute("COMMIT")
            except BaseException:
                self.store.db.execute("ROLLBACK")
                raise
            return BoundSource(descriptor, source["id"], 0)

    async def register_epoch(self, source, epoch):
        await self.io.run(self._register_epoch, source, epoch)

    def _register_epoch(self, bound, epoch):
        from live_interaction.audio_v2 import CodecEpoch
        from .store import StoreError
        rates = (16000,) if self.direction == "uplink" else (16000, 24000, 48000)
        if not isinstance(epoch, CodecEpoch) or epoch.decoded_sample_rate not in rates:
            raise StoreError("SOURCE_CODEC", "Source requires a typed codec profile for its direction")
        encoded = json.dumps(epoch.as_dict(), sort_keys=True, separators=(",", ":"))
        with self.store._lock:
            self._bound(bound)
            existing = self.store.db.execute("SELECT descriptor_json FROM encoded_voice_epochs WHERE source_id=? AND epoch_id=?", (bound.server_source_id, epoch.epoch_id)).fetchone()
            if existing:
                if existing["descriptor_json"] != encoded:
                    raise StoreError("SOURCE_EPOCH_CONFLICT", "A committed codec epoch cannot be reinterpreted")
                return
            count = self.store.db.execute("SELECT COUNT(*) FROM encoded_voice_epochs WHERE source_id=?", (bound.server_source_id,)).fetchone()[0]
            if count >= self.limits.epochs_per_source:
                raise StoreError("SOURCE_STORAGE_FULL", "Source codec epoch bound reached")
            self.store.db.execute("INSERT INTO encoded_voice_epochs(source_id,epoch_id,descriptor_json,descriptor_sha256) VALUES(?,?,?,?)", (bound.server_source_id, epoch.epoch_id, encoded, hashlib.sha256(encoded.encode()).hexdigest()))

    async def commit_chunk(self, source, epoch, chunk):
        if self.direction != "uplink":
            from .store import StoreError
            raise StoreError("SOURCE_OUTPUT", "Output originals require their canonical event identity")
        return await self.io.run(self._commit_chunk, source, epoch, chunk)

    async def commit_output(self, source, epoch, output):
        from live_interaction.audio_v2 import OutputChunk
        from .store import StoreError
        if self.direction != "downlink" or not isinstance(output, OutputChunk):
            raise StoreError("SOURCE_OUTPUT", "A typed output original is required")
        return await self.io.run(self._commit_chunk, source, epoch, output.chunk, output.original_event_seq)

    async def commit_outputs(self, source, epoch, outputs):
        """One bounded durable SQLite commit for a burst of original output pages.

        Atomic batch receipts are created only after the whole batch commits.
        The SDK may still use the single-output contract for providers without
        this product-specific optimization. Failed batches preserve earlier
        durable prefixes and never manufacture an ACK.
        """
        from live_interaction.audio_v2 import OutputChunk
        from .store import StoreError
        if (self.direction != "downlink" or not isinstance(outputs, (list, tuple))
                or not 1 <= len(outputs) <= 6
                or any(not isinstance(value, OutputChunk) for value in outputs)):
            raise StoreError("SOURCE_OUTPUT", "Expected one to six typed output originals")
        return await self.io.run(self._commit_outputs, source, epoch, tuple(outputs))

    def _commit_outputs(self, source, epoch, outputs):
        from live_interaction.audio_v2 import EPOCH_END
        # The product's SQLite connection and lock are shared across all voice
        # sessions. A burst uses one fsync, rather than competing with fresh
        # microphone commits for the global lock once per encoded page.
        with self.store._lock:
            self.store.db.execute("BEGIN IMMEDIATE")
            try:
                receipts = tuple(
                    self._commit_chunk(source, epoch, output.chunk,
                                       output.original_event_seq, in_transaction=True)
                    for output in outputs
                )
                self.store.db.execute("COMMIT")
            except BaseException:
                self.store.db.execute("ROLLBACK")
                raise
        # Only a *committed* batch may be reported as durable.
        for output, receipt in zip(outputs, receipts):
            chunk = output.chunk
            if receipt.newly_committed and (chunk.chunk_seq == 1 or chunk.flags & EPOCH_END):
                log.info("live output durable", extra={
                    "event": "live_output_durable", "conversation_id": self.conversation_id,
                    "source_id": receipt.server_source_id, "status": "server_durable",
                    "kind": "terminal" if chunk.flags & EPOCH_END else "first_chunk",
                    "frame_seq": chunk.chunk_seq,
                    "duration_ms": (chunk.capture_start_us + chunk.duration_us) // 1000,
                })
        return receipts

    def _commit_chunk(self, bound, epoch, chunk, original_event_seq=None, *, in_transaction=False):
        from live_interaction.audio_v2 import DurableCommit, EncodedChunk, EPOCH_END
        from .store import StoreError, _now_ms
        if not isinstance(chunk, EncodedChunk) or chunk.source_id != bound.source.source_id or chunk.epoch_id != epoch.epoch_id:
            raise StoreError("SOURCE_BINDING", "Chunk does not belong to the source and codec epoch")
        metadata = (chunk.epoch_id, chunk.capture_start_us, chunk.duration_us, chunk.flags, chunk.payload_sha256)
        end_us = chunk.capture_start_us + chunk.duration_us
        if end_us > self.limits.source_duration_us or chunk.chunk_seq > self.limits.source_chunks:
            raise StoreError("SOURCE_STORAGE_FULL", "Source timeline or chunk bound reached")
        with self.store._lock:
            source = self._bound(bound)
            state = self.store.db.execute("SELECT * FROM encoded_voice_sources WHERE source_id=?", (source["id"],)).fetchone()
            old = self.store.db.execute("SELECT * FROM encoded_voice_chunks WHERE source_id=? AND chunk_seq=?", (source["id"], chunk.chunk_seq)).fetchone()
            if old:
                prior = tuple(old[key] for key in ("epoch_id", "capture_start_us", "duration_us", "flags", "payload_sha256"))
                if (prior != metadata or bytes(old["payload"]) != chunk.payload
                        or old["original_event_seq"] != original_event_seq):
                    raise StoreError("SOURCE_CHUNK_CONFLICT", "Stable chunk identity has different original bytes or metadata")
                return DurableCommit(chunk.source_id, source["id"], chunk.epoch_id, chunk.chunk_seq, chunk.payload_sha256, state["contiguous_seq"], False)
            if state["sealed_seq"] is not None and (chunk.chunk_seq > state["sealed_seq"] or end_us > state["capture_end_us"]):
                raise StoreError("SOURCE_SEALED", "Chunk exceeds the original source end")
            if chunk.chunk_seq == state["sealed_seq"] and end_us != state["capture_end_us"]:
                raise StoreError("SOURCE_SEAL_CONFLICT", "Terminal chunk must retain the declared source end")
            descriptor = self.store.db.execute("SELECT * FROM encoded_voice_epochs WHERE source_id=? AND epoch_id=?", (source["id"], epoch.epoch_id)).fetchone()
            canonical = json.dumps(epoch.as_dict(), sort_keys=True, separators=(",", ":"))
            if not descriptor or descriptor["descriptor_json"] != canonical:
                raise StoreError("SOURCE_EPOCH_CONFLICT", "Chunk requires its committed codec epoch")
            if descriptor["end_seq"] is not None and chunk.chunk_seq > descriptor["end_seq"]:
                raise StoreError("SOURCE_EPOCH_SEALED", "Codec epoch has already ended")
            if chunk.flags & EPOCH_END and descriptor["end_seq"] not in (None, chunk.chunk_seq):
                raise StoreError("SOURCE_EPOCH_CONFLICT", "Codec epoch has a different terminal chunk")
            epoch_extent = self.store.db.execute("SELECT MIN(chunk_seq),MAX(chunk_seq) FROM encoded_voice_chunks WHERE source_id=? AND epoch_id=?", (source["id"], epoch.epoch_id)).fetchone()
            if epoch_extent[0] is not None:
                if chunk.flags & EPOCH_END and epoch_extent[1] > chunk.chunk_seq:
                    raise StoreError("SOURCE_EPOCH_CONFLICT", "Codec end precedes already committed epoch audio")
                low, high = min(epoch_extent[0], chunk.chunk_seq), max(epoch_extent[1], chunk.chunk_seq)
                interleaved = self.store.db.execute("SELECT 1 FROM encoded_voice_chunks WHERE source_id=? AND chunk_seq BETWEEN ? AND ? AND epoch_id<>? LIMIT 1", (source["id"], low, high, epoch.epoch_id)).fetchone()
                if interleaved:
                    raise StoreError("SOURCE_EPOCH_CONFLICT", "Codec epochs cannot interleave within one captured source")
            previous = self.store.db.execute("SELECT chunk_seq,epoch_id,flags,capture_start_us,duration_us FROM encoded_voice_chunks WHERE source_id=? AND chunk_seq<? ORDER BY chunk_seq DESC LIMIT 1", (source["id"], chunk.chunk_seq)).fetchone()
            following = self.store.db.execute("SELECT chunk_seq,epoch_id,capture_start_us FROM encoded_voice_chunks WHERE source_id=? AND chunk_seq>? ORDER BY chunk_seq ASC LIMIT 1", (source["id"], chunk.chunk_seq)).fetchone()
            if ((previous and previous["capture_start_us"] + previous["duration_us"] > chunk.capture_start_us)
                    or (following and end_us > following["capture_start_us"])):
                raise StoreError("SOURCE_TIMELINE_CONFLICT", "Chunk order overlaps the original capture timeline")
            if previous and previous["chunk_seq"] == chunk.chunk_seq - 1:
                if previous["epoch_id"] == chunk.epoch_id:
                    if previous["capture_start_us"] + previous["duration_us"] != chunk.capture_start_us:
                        raise StoreError("SOURCE_TIMELINE_CONFLICT", "A codec epoch must have a continuous decoded timeline")
                elif not previous["flags"] & EPOCH_END:
                    raise StoreError("SOURCE_EPOCH_CONFLICT", "A codec transition requires its original terminal packet")
            if following and following["chunk_seq"] == chunk.chunk_seq + 1:
                if following["epoch_id"] == chunk.epoch_id:
                    if end_us != following["capture_start_us"]:
                        raise StoreError("SOURCE_TIMELINE_CONFLICT", "A codec epoch must have a continuous decoded timeline")
                elif not chunk.flags & EPOCH_END:
                    raise StoreError("SOURCE_EPOCH_CONFLICT", "A codec transition requires its original terminal packet")
            budget = self.store.db.execute("SELECT * FROM encoded_voice_storage WHERE slot=1").fetchone()
            if (source["audio_bytes"] + len(chunk.payload) > self.limits.source_bytes
                    or budget["encoded_bytes"] + len(chunk.payload) > self.limits.total_bytes
                    or budget["chunks"] >= self.limits.total_chunks):
                raise StoreError("SOURCE_STORAGE_FULL", "Compressed source storage is full; saved originals remain available")
            if not in_transaction:
                self.store.db.execute("BEGIN IMMEDIATE")
            try:
                self.store.db.execute("INSERT INTO encoded_voice_chunks(source_id,chunk_seq,epoch_id,capture_start_us,duration_us,flags,payload_sha256,payload,original_event_seq) VALUES(?,?,?,?,?,?,?,?,?)", (source["id"], chunk.chunk_seq, *metadata[:-1], metadata[-1], chunk.payload, original_event_seq))
                if chunk.flags & EPOCH_END:
                    self.store.db.execute("UPDATE encoded_voice_epochs SET end_seq=? WHERE source_id=? AND epoch_id=?", (chunk.chunk_seq, source["id"], epoch.epoch_id))
                prefix = state["contiguous_seq"]
                # The index lookup advances only across actually committed rows.
                # Rows beyond a hole neither advance the ACK nor make it durable.
                while self.store.db.execute("SELECT 1 FROM encoded_voice_chunks WHERE source_id=? AND chunk_seq=?", (source["id"], prefix + 1)).fetchone():
                    prefix += 1
                complete = self._complete(source["id"], prefix, state["sealed_seq"], state["capture_end_us"])
                self.store.db.execute("UPDATE encoded_voice_sources SET contiguous_seq=?,complete=? WHERE source_id=?", (prefix, int(complete), source["id"]))
                self.store.db.execute("UPDATE sources SET audio_bytes=audio_bytes+?,audio_chunks=audio_chunks+1,audio_duration_us=audio_duration_us+?,updated_at_ms=? WHERE id=?", (len(chunk.payload), chunk.duration_us, _now_ms(), source["id"]))
                self.store.db.execute("UPDATE encoded_voice_storage SET encoded_bytes=encoded_bytes+?,chunks=chunks+1 WHERE slot=1", (len(chunk.payload),))
                if not in_transaction:
                    self.store.db.execute("COMMIT")
            except BaseException:
                if not in_transaction:
                    self.store.db.execute("ROLLBACK")
                raise
            if (not in_transaction and self.direction == "downlink"
                    and (chunk.chunk_seq == 1 or chunk.flags & EPOCH_END)):
                # A real device can be silent despite a successful provider audio
                # event. Log the durable output boundary separately from handset
                # playback; never log the payload, spoken text or codec bytes.
                log.info("live output durable", extra={
                    "event": "live_output_durable", "conversation_id": self.conversation_id,
                    "source_id": source["id"], "status": "server_durable",
                    "kind": "terminal" if chunk.flags & EPOCH_END else "first_chunk",
                    "frame_seq": chunk.chunk_seq, "duration_ms": end_us // 1000,
                })
            return DurableCommit(chunk.source_id, source["id"], chunk.epoch_id, chunk.chunk_seq, chunk.payload_sha256, prefix, True)

    async def seal_source(self, source, last_chunk_seq: int, capture_end_us: int):
        return await self.io.run(self._seal_source, source, last_chunk_seq, capture_end_us)

    def _seal_source(self, bound, last_chunk_seq, capture_end_us):
        from .store import StoreError
        if (type(last_chunk_seq) is not int or not 0 <= last_chunk_seq <= self.limits.source_chunks
                or type(capture_end_us) is not int or not 0 <= capture_end_us <= self.limits.source_duration_us):
            raise StoreError("SOURCE_METADATA", "Source end exceeds its bounds")
        with self.store._lock:
            self._bound(bound)
            state = self.store.db.execute("SELECT * FROM encoded_voice_sources WHERE source_id=?", (bound.server_source_id,)).fetchone()
            if state["sealed_seq"] is not None and (state["sealed_seq"], state["capture_end_us"]) != (last_chunk_seq, capture_end_us):
                raise StoreError("SOURCE_SEAL_CONFLICT", "Original source end is immutable")
            excess = self.store.db.execute("SELECT 1 FROM encoded_voice_chunks WHERE source_id=? AND (chunk_seq>? OR capture_start_us+duration_us>?) LIMIT 1", (bound.server_source_id, last_chunk_seq, capture_end_us)).fetchone()
            if excess:
                raise StoreError("SOURCE_SEAL_CONFLICT", "Source end precedes already committed audio")
            last = self.store.db.execute("SELECT capture_start_us+duration_us AS end_us FROM encoded_voice_chunks WHERE source_id=? AND chunk_seq=?", (bound.server_source_id, last_chunk_seq)).fetchone()
            if (last_chunk_seq == 0 and capture_end_us != 0) or (last and last["end_us"] != capture_end_us):
                raise StoreError("SOURCE_SEAL_CONFLICT", "Source end must match its last valid captured sample")
            complete = self._complete(bound.server_source_id, state["contiguous_seq"], last_chunk_seq, capture_end_us)
            self.store.db.execute("UPDATE encoded_voice_sources SET sealed_seq=?,capture_end_us=?,complete=? WHERE source_id=?", (last_chunk_seq, capture_end_us, int(complete), bound.server_source_id))
            return {"server_durable_seq": state["contiguous_seq"], "complete": complete}

    def claim_projection(self, projection) -> dict[str, Any]:
        """Fence duplicate provider admission before queueing an internal PCM view.

        A crash after this claim is an unknown semantic delivery, never permission
        to replay it as a fresh command. The compressed original stays recoverable.
        """
        from live_interaction.audio_v2 import DurablePCMProjection
        from .store import StoreError
        if not isinstance(projection, DurablePCMProjection):
            raise StoreError("SOURCE_PROJECTION", "Internal durable audio projection is required")
        if self.direction != "uplink":
            raise StoreError("SOURCE_PROJECTION", "Playback audio cannot become live user input")
        commit = projection.commit
        with self.store._lock:
            source = self._source(commit.server_source_id)
            row = self.store.db.execute("SELECT * FROM encoded_voice_chunks WHERE source_id=? AND chunk_seq=?", (source["id"], commit.chunk_seq)).fetchone()
            state = self.store.db.execute("SELECT contiguous_seq FROM encoded_voice_sources WHERE source_id=?", (source["id"],)).fetchone()
            if (source.get("client_source_id") != commit.source_id or not row or not state
                    or row["epoch_id"] != commit.epoch_id or row["payload_sha256"] != commit.payload_sha256
                    or state["contiguous_seq"] < commit.chunk_seq):
                raise StoreError("SOURCE_PROJECTION", "Projection lacks its matching contiguous durable source")
            samples = len(projection.pcm16) // 2
            start = projection.capture_start_us
            end = start + (samples * 1_000_000 + 15_999) // 16_000
            pcm_hash = hashlib.sha256(projection.pcm16).hexdigest()
            prior = self.store.db.execute("SELECT pcm_sha256 FROM encoded_voice_projections WHERE source_id=? AND chunk_seq=? AND capture_start_us=? AND sample_count=?", (source["id"], commit.chunk_seq, start, samples)).fetchone()
            if prior:
                if prior["pcm_sha256"] != pcm_hash:
                    raise StoreError("SOURCE_PROJECTION", "A projection identity cannot change its decoded bytes")
                raise StoreError("SOURCE_DELIVERY_UNKNOWN", "This decoded range already crossed the provider admission fence")
            # A stateful resampler can emit a previous packet's delayed tail
            # together with this packet. The contiguous receipt covers those
            # dependencies; claim precise nonoverlapping sample ranges, not one
            # bit for the whole encoded packet. Only the PCM hash is persisted.
            origin = self.store.db.execute("SELECT MIN(capture_start_us) FROM encoded_voice_chunks WHERE source_id=? AND epoch_id=?", (source["id"], commit.epoch_id)).fetchone()[0]
            if start < origin or end > row["capture_start_us"] + row["duration_us"] + 63:
                raise StoreError("SOURCE_PROJECTION", "Decoded projection exceeds its durable capture span")
            overlap = self.store.db.execute("SELECT 1 FROM encoded_voice_projections WHERE source_id=? AND end_us>? AND capture_start_us<? LIMIT 1", (source["id"], start + 63, end - 63)).fetchone()
            if overlap:
                raise StoreError("SOURCE_DELIVERY_UNKNOWN", "This decoded capture span overlaps an earlier admission")
            self.store.db.execute("BEGIN IMMEDIATE")
            try:
                self.store.db.execute("INSERT INTO encoded_voice_projections(source_id,chunk_seq,capture_start_us,end_us,sample_count,pcm_sha256) VALUES(?,?,?,?,?,?)", (source["id"], commit.chunk_seq, start, end, samples, pcm_hash))
                self.store.db.execute("UPDATE encoded_voice_chunks SET delivery_state='claimed',projected_bytes=projected_bytes+? WHERE source_id=? AND chunk_seq=?", (len(projection.pcm16), source["id"], commit.chunk_seq))
                self.store.db.execute("UPDATE sources SET audio_projection_bytes=audio_projection_bytes+? WHERE id=?", (len(projection.pcm16), source["id"]))
                self.store.db.execute("COMMIT")
            except BaseException:
                self.store.db.execute("ROLLBACK")
                raise
            return {"audio_bytes": source["audio_projection_bytes"] + len(projection.pcm16), "audio_chunks": source["audio_chunks"]}

    def read_source_chunks(self, server_source_id: str, *, after: int = 0, max_bytes: int = 65_536,
                    max_duration_us: int = 2_000_000) -> list[dict[str, Any]]:
        """Bounded source-analysis read; never a whole recording in RAM."""
        from .store import StoreError
        if (type(after) is not int or after < 0 or type(max_bytes) is not int or not 1 <= max_bytes <= 262_144
                or type(max_duration_us) is not int or not 1 <= max_duration_us <= 5_000_000):
            raise StoreError("SOURCE_METADATA", "Invalid source read bounds")
        with self.store._lock:
            self._source(server_source_id)
            rows = self.store.db.execute("SELECT * FROM encoded_voice_chunks WHERE source_id=? AND chunk_seq>? ORDER BY chunk_seq LIMIT 64", (server_source_id, after))
            result, size, duration = [], 0, 0
            for row in rows:
                if size + len(row["payload"]) > max_bytes or duration + row["duration_us"] > max_duration_us:
                    break
                result.append(dict(row))
                size += len(row["payload"])
                duration += row["duration_us"]
            return result

    async def resume_source(self, source_id: str):
        return await self.io.run(self._resume_source, source_id)

    def _resume_source(self, source_id):
        from live_interaction.audio_v2 import BoundSource, SourceDescriptor
        from .store import StoreError
        with self.store._lock:
            source = self.store.get_source_by_client(self.actor_id, self.conversation_id, source_id)
            state = self.store.db.execute("SELECT * FROM encoded_voice_sources WHERE source_id=?", (source["id"],)).fetchone()
            if not state:
                raise StoreError("SOURCE_AUDIO_FORMAT", "Source is not an encoded original")
            bound = BoundSource(SourceDescriptor(source_id, state["started_at_unix_ms"]), source["id"], state["contiguous_seq"])
            self._bound(bound)
            return bound

    async def epoch(self, source, epoch_id: str):
        return await self.io.run(self._epoch, source, epoch_id)

    def _epoch(self, source, epoch_id):
        from live_interaction.audio_v2 import CodecEpoch
        from .store import StoreError
        with self.store._lock:
            self._bound(source)
            row = self.store.db.execute("SELECT descriptor_json FROM encoded_voice_epochs WHERE source_id=? AND epoch_id=?", (source.server_source_id, epoch_id)).fetchone()
            if not row:
                raise StoreError("SOURCE_EPOCH_CONFLICT", "Original codec initialization is unavailable")
            return CodecEpoch.from_dict(json.loads(row["descriptor_json"]))

    async def read_chunks(self, source, *, after_seq: int = 0, max_encoded_bytes: int = 65_536,
                          max_duration_us: int = 2_000_000):
        def read():
            from live_interaction.audio_v2 import EncodedChunk
            with self.store._lock:
                self._bound(source)
                rows = self.read_source_chunks(source.server_source_id, after=after_seq,
                                               max_bytes=max_encoded_bytes, max_duration_us=max_duration_us)
                return [EncodedChunk(source.source.source_id, row["epoch_id"], row["chunk_seq"],
                                     row["capture_start_us"], row["duration_us"], row["flags"],
                                     bytes(row["payload"])) for row in rows]
        return await self.io.run(read)

    async def mark_played(self, source, played_us: int):
        return await self.io.run(self._mark_played, source, played_us)

    def _mark_played(self, source, played_us):
        from .store import StoreError
        if self.direction != "downlink" or type(played_us) is not int or played_us < 0:
            raise StoreError("SOURCE_PLAYBACK", "A playback receipt requires an output source and a valid position")
        with self.store._lock:
            self._bound(source)
            state = self.store.db.execute("SELECT * FROM encoded_voice_sources WHERE source_id=?", (source.server_source_id,)).fetchone()
            last = self.store.db.execute("SELECT capture_start_us+duration_us AS end_us FROM encoded_voice_chunks WHERE source_id=? AND chunk_seq=?", (source.server_source_id, state["contiguous_seq"])).fetchone()
            if played_us > (last["end_us"] if last else 0) + 1000:
                raise StoreError("SOURCE_PLAYBACK", "Playback receipt exceeds the durably stored response")
            position = max(state["played_us"], played_us)
            self.store.db.execute("UPDATE encoded_voice_sources SET played_us=? WHERE source_id=?", (position, source.server_source_id))
            if position > state["played_us"] and (state["played_us"] == 0
                    or position // 1_000_000 > state["played_us"] // 1_000_000
                    or bool(last and position >= last["end_us"])):
                # This proves a client-reported playhead reached the durable store;
                # it does NOT prove the phone's speaker was audible to the owner.
                log.info("live output playhead reported", extra={
                    "event": "live_output_playhead", "conversation_id": self.conversation_id,
                    "source_id": source.server_source_id, "status": "client_reported",
                    "kind": "playback_position", "duration_ms": position // 1000,
                })
            return {"played_us": position}

    async def read_output(self, source, *, after_seq: int = 0, max_encoded_bytes: int = 65_536,
                          max_duration_us: int = 2_000_000):
        def read():
            from live_interaction.audio_v2 import EncodedChunk, OutputChunk
            from .store import StoreError
            if self.direction != "downlink":
                raise StoreError("SOURCE_OUTPUT", "Playback requires an output source")
            with self.store._lock:
                self._bound(source)
                prefix = self.store.db.execute(
                    "SELECT contiguous_seq FROM encoded_voice_sources WHERE source_id=?",
                    (source.server_source_id,),
                ).fetchone()[0]
                rows = self.read_source_chunks(source.server_source_id, after=after_seq,
                                               max_bytes=max_encoded_bytes, max_duration_us=max_duration_us)
                return [OutputChunk(EncodedChunk(source.source.source_id, row["epoch_id"], row["chunk_seq"],
                                                row["capture_start_us"], row["duration_us"], row["flags"],
                                                bytes(row["payload"])), row["original_event_seq"]) for row in rows
                        if row["chunk_seq"] <= prefix]
        return await self.io.run(read)

    async def output_status(self, source):
        return await self.io.run(self._output_status, source)

    def _output_status(self, source):
        from live_interaction.audio_v2 import BoundSource, OutputResume
        from .store import StoreError
        if self.direction != "downlink":
            raise StoreError("SOURCE_OUTPUT", "Playback status requires an output source")
        with self.store._lock:
            self._bound(source)
            row = self.store.db.execute("SELECT * FROM encoded_voice_sources WHERE source_id=?", (source.server_source_id,)).fetchone()
            last = self.store.db.execute("SELECT capture_start_us+duration_us AS end_us FROM encoded_voice_chunks WHERE source_id=? AND chunk_seq=?", (source.server_source_id, row["contiguous_seq"])).fetchone()
            current = BoundSource(source.source, source.server_source_id, row["contiguous_seq"])
            return OutputResume(current, row["played_us"], bool(row["output_cancelled"]),
                                last["end_us"] if last else 0, bool(row["complete"]))

    async def pending_outputs(self, *, limit: int = 8):
        def read():
            from live_interaction.audio_v2 import BoundSource, SourceDescriptor
            from .store import StoreError
            if self.direction != "downlink" or type(limit) is not int or not 1 <= limit <= 8:
                raise StoreError("SOURCE_OUTPUT", "Invalid output manifest bound")
            with self.store._lock:
                # Authenticate the conversation even if the bounded manifest is empty.
                self.store.get_conversation(self.actor_id, self.conversation_id)
                # The manifest is an AUTOPLAY offer, never the conversation's
                # historical audio archive. SDK output source IDs contain the
                # immutable Live session ID. Reattaching a socket uses that same
                # session; a new Start must not resurrect yesterday's response.
                # Explicit resume_source/read_output still allow authorized
                # archival replay without destroying originals or played cursors.
                prefix = f"out_{self.output_session_id}_" if self.output_session_id else ""
                rows = self.store.db.execute("""
                    SELECT e.*,s.client_source_id FROM encoded_voice_sources e
                    JOIN sources s ON s.id=e.source_id
                    LEFT JOIN encoded_voice_chunks c ON c.source_id=e.source_id AND c.chunk_seq=e.contiguous_seq
                    WHERE s.actor_id=? AND s.conversation_id=? AND s.audio_direction='downlink'
                    AND e.output_cancelled=0
                    AND (?='' OR substr(s.client_source_id,1,length(?))=?)
                    AND (e.complete=0 OR e.played_us<COALESCE(c.capture_start_us+c.duration_us,0))
                    ORDER BY s.captured_at_ms DESC,s.id DESC LIMIT ?
                """, (self.actor_id, self.conversation_id, prefix, prefix, prefix, limit)).fetchall()
                return [self._output_status(BoundSource(SourceDescriptor(row["client_source_id"], row["started_at_unix_ms"]),
                                                       row["source_id"], row["contiguous_seq"])) for row in rows]
        return await self.io.run(read)

    async def cancel_output(self, source):
        def cancel():
            from .store import StoreError
            if self.direction != "downlink":
                raise StoreError("SOURCE_OUTPUT", "Only output playback can be cancelled")
            with self.store._lock:
                self._bound(source)
                # Cancellation is a disposition, never a fabricated played receipt
                # and never deletion of the original response.
                self.store.db.execute("UPDATE encoded_voice_sources SET output_cancelled=1 WHERE source_id=?", (source.server_source_id,))
        await self.io.run(cancel)
