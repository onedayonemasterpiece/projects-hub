export type LocalSourceStatus =
  | "capturing"
  | "sealed"
  | "replaying"
  | "pending"
  | "failed";

export type LocalVoiceSource = {
  id: string;
  workspace_id: string;
  conversation_id: string;
  status: LocalSourceStatus;
  chunk_count: number;
  audio_bytes: number;
  turn_markers: number;
  created_at_ms: number;
  updated_at_ms: number;
  last_error?: string;
};

export type LocalVoiceChunk = {
  source_id: string;
  index: number;
  pcm: ArrayBuffer;
  sha256: string;
  captured_at_ms: number;
};

export type DurableAudioMessage =
  | { pcm: Int16Array; sample_rate: 16000; captured_at_ms: number }
  | { audio_stream_end: true; captured_at_ms: number };

const DB_NAME = "projects-hub-voice-sources";
const DB_VERSION = 1;
const SOURCE_STORE = "sources";
const CHUNK_STORE = "chunks";

let databasePromise: Promise<IDBDatabase> | null = null;

function idbRequest<T>(request: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error ?? new Error("IndexedDB request failed"));
  });
}

function transactionDone(transaction: IDBTransaction): Promise<void> {
  return new Promise((resolve, reject) => {
    transaction.oncomplete = () => resolve();
    transaction.onabort = () => reject(transaction.error ?? new Error("IndexedDB transaction aborted"));
    transaction.onerror = () => reject(transaction.error ?? new Error("IndexedDB transaction failed"));
  });
}

function database(): Promise<IDBDatabase> {
  if (databasePromise) return databasePromise;
  databasePromise = new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION);
    request.onupgradeneeded = () => {
      const db = request.result;
      if (!db.objectStoreNames.contains(SOURCE_STORE)) {
        const sources = db.createObjectStore(SOURCE_STORE, { keyPath: "id" });
        sources.createIndex("by_workspace_status", ["workspace_id", "status"], { unique: false });
        sources.createIndex("by_workspace_created", ["workspace_id", "created_at_ms"], { unique: false });
      }
      if (!db.objectStoreNames.contains(CHUNK_STORE)) {
        const chunks = db.createObjectStore(CHUNK_STORE, {
          keyPath: ["source_id", "index"],
        });
        chunks.createIndex("by_source", "source_id", { unique: false });
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error ?? new Error("IndexedDB unavailable"));
  });
  return databasePromise;
}

function localId(): string {
  return "local_" + crypto.randomUUID().replaceAll("-", "").toLowerCase();
}

async function sha256(buffer: ArrayBuffer): Promise<string> {
  const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", buffer));
  return Array.from(digest, value => value.toString(16).padStart(2, "0")).join("");
}

async function putSource(source: LocalVoiceSource): Promise<void> {
  const db = await database();
  const tx = db.transaction(SOURCE_STORE, "readwrite");
  const done = transactionDone(tx);
  tx.objectStore(SOURCE_STORE).put(source);
  await done;
}

async function allSources(): Promise<LocalVoiceSource[]> {
  const db = await database();
  const tx = db.transaction(SOURCE_STORE, "readonly");
  const done = transactionDone(tx);
  const request = idbRequest<LocalVoiceSource[]>(tx.objectStore(SOURCE_STORE).getAll());
  const result = await request;
  await done;
  return result;
}

export async function createLocalVoiceSource(
  workspaceId: string,
  conversationId: string,
): Promise<LocalVoiceSource> {
  const now = Date.now();
  const source: LocalVoiceSource = {
    id: localId(),
    workspace_id: workspaceId,
    conversation_id: conversationId,
    status: "capturing",
    chunk_count: 0,
    audio_bytes: 0,
    turn_markers: 0,
    created_at_ms: now,
    updated_at_ms: now,
  };
  await putSource(source);
  return source;
}

export async function getLocalVoiceSource(id: string): Promise<LocalVoiceSource | null> {
  const db = await database();
  const tx = db.transaction(SOURCE_STORE, "readonly");
  const done = transactionDone(tx);
  const request = idbRequest<LocalVoiceSource | undefined>(
    tx.objectStore(SOURCE_STORE).get(id),
  );
  const value = await request;
  await done;
  return value ?? null;
}

async function updateSource(
  id: string,
  update: (source: LocalVoiceSource) => LocalVoiceSource,
): Promise<LocalVoiceSource> {
  const source = await getLocalVoiceSource(id);
  if (!source) throw new Error("Локальная голосовая запись не найдена.");
  const next = update(source);
  await putSource(next);
  return next;
}

export function createLocalPersistSink(sourceId: string) {
  let nextIndex: number | null = null;
  let pending = Promise.resolve();

  async function persistOne(message: DurableAudioMessage): Promise<void> {
    const source = await getLocalVoiceSource(sourceId);
    if (!source || source.status !== "capturing") {
      throw new Error("Локальная голосовая запись уже закрыта.");
    }
    if (nextIndex === null) nextIndex = source.chunk_count;

    if ("audio_stream_end" in message) {
      await putSource({
        ...source,
        turn_markers: source.turn_markers + 1,
        updated_at_ms: Date.now(),
      });
      return;
    }
    if (message.sample_rate !== 16000 || !(message.pcm instanceof Int16Array)) {
      throw new Error("Некорректный формат локального PCM.");
    }

    const copy = message.pcm.slice();
    const bytes = copy.buffer.slice(0);
    const hash = await sha256(bytes);
    const index = nextIndex++;
    const chunk: LocalVoiceChunk = {
      source_id: sourceId,
      index,
      pcm: bytes,
      sha256: hash,
      captured_at_ms: Math.round(message.captured_at_ms),
    };

    const db = await database();
    const tx = db.transaction([SOURCE_STORE, CHUNK_STORE], "readwrite");
    const done = transactionDone(tx);
    tx.objectStore(CHUNK_STORE).add(chunk);
    tx.objectStore(SOURCE_STORE).put({
      ...source,
      chunk_count: source.chunk_count + 1,
      audio_bytes: source.audio_bytes + bytes.byteLength,
      updated_at_ms: Date.now(),
    } satisfies LocalVoiceSource);
    await done;
  }

  return {
    persist(message: DurableAudioMessage): Promise<void> {
      pending = pending.then(() => persistOne(message));
      return pending;
    },
    async drain(): Promise<void> {
      await pending;
    },
  };
}

export async function sealLocalVoiceSource(sourceId: string): Promise<LocalVoiceSource> {
  return updateSource(sourceId, source => ({
    ...source,
    status: source.chunk_count > 0 ? "sealed" : "failed",
    last_error: source.chunk_count > 0 ? undefined : "empty_source",
    updated_at_ms: Date.now(),
  }));
}

export async function markLocalSource(
  sourceId: string,
  status: LocalSourceStatus,
  lastError?: string,
): Promise<LocalVoiceSource> {
  return updateSource(sourceId, source => ({
    ...source,
    status,
    last_error: lastError,
    updated_at_ms: Date.now(),
  }));
}

export async function recoverInterruptedVoiceSources(workspaceId: string): Promise<number> {
  const candidates = (await allSources()).filter(
    source =>
      source.workspace_id === workspaceId &&
      (source.status === "capturing" || source.status === "replaying"),
  );
  if (!candidates.length) return 0;
  const db = await database();
  const tx = db.transaction(SOURCE_STORE, "readwrite");
  const done = transactionDone(tx);
  const store = tx.objectStore(SOURCE_STORE);
  for (const source of candidates) {
    store.put({
      ...source,
      status: source.chunk_count > 0 ? "sealed" : "failed",
      last_error: source.chunk_count > 0 ? "interrupted_capture" : "empty_source",
      updated_at_ms: Date.now(),
    } satisfies LocalVoiceSource);
  }
  await done;
  return candidates.length;
}

export async function listPendingVoiceSources(
  workspaceId: string,
): Promise<LocalVoiceSource[]> {
  return (await allSources())
    .filter(
      source =>
        source.workspace_id === workspaceId &&
        ["sealed", "pending", "failed"].includes(source.status) &&
        source.chunk_count > 0,
    )
    .sort((left, right) => left.created_at_ms - right.created_at_ms);
}

export async function loadVoiceChunks(sourceId: string): Promise<LocalVoiceChunk[]> {
  const db = await database();
  const tx = db.transaction(CHUNK_STORE, "readonly");
  const done = transactionDone(tx);
  const request = idbRequest<LocalVoiceChunk[]>(
    tx.objectStore(CHUNK_STORE).index("by_source").getAll(sourceId),
  );
  const chunks = await request;
  await done;
  return chunks.sort((left, right) => left.index - right.index);
}

export async function acknowledgeDeliveredSource(sourceId: string): Promise<void> {
  const chunks = await loadVoiceChunks(sourceId);
  const db = await database();
  const tx = db.transaction([SOURCE_STORE, CHUNK_STORE], "readwrite");
  const done = transactionDone(tx);
  const chunkStore = tx.objectStore(CHUNK_STORE);
  for (const chunk of chunks) chunkStore.delete([sourceId, chunk.index]);
  tx.objectStore(SOURCE_STORE).delete(sourceId);
  await done;
}
