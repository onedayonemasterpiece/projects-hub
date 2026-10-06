import {
  createLiveClient,
  type LiveEvent,
} from "@onedayonemasterpiece/live-interaction/browser";
import { ApiError, getSourceByClient, type SourceReceipt } from "./api";
import {
  acknowledgeDeliveredSource,
  loadVoiceChunks,
  markLocalSource,
  type LocalVoiceSource,
} from "./offlineSources";

import { createReplayCompletion } from "./replayCompletion.js";

const TERMINAL = new Set(["archived", "ephemeral_processed"]);

async function serverSource(
  source: LocalVoiceSource,
): Promise<SourceReceipt | null> {
  try {
    return (await getSourceByClient(source.conversation_id, source.id)).source;
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) return null;
    throw error;
  }
}

export type ReplayCallbacks = {
  onState?: (state: string) => void;
  onEvent?: (event: LiveEvent) => void;
  onPreferenceEvent?: (event: LiveEvent, isCurrent: () => boolean) => void;
  onNotice?: (message: string) => void;
  signal?: AbortSignal;
};

export type ReplayResult = {
  source_id: string;
  status: "delivered" | "pending";
  server_status?: string;
  skipped_provider: boolean;
};

export async function replayLocalVoiceSource(
  source: LocalVoiceSource,
  callbacks: ReplayCallbacks = {},
): Promise<ReplayResult> {
  const existing = await serverSource(source);
  if (existing && TERMINAL.has(existing.status)) {
    await acknowledgeDeliveredSource(source.id);
    return {
      source_id: source.id,
      status: "delivered",
      server_status: existing.status,
      skipped_provider: true,
    };
  }

  const chunks = await loadVoiceChunks(source.id);
  if (!chunks.length) {
    await markLocalSource(source.id, "failed", "empty_source");
    throw new Error("Сохранённая запись не содержит аудио.");
  }

  await markLocalSource(source.id, "replaying");
  let completeResolve: (() => void) | null = null;
  let completeReject: ((reason?: unknown) => void) | null = null;
  let completed = false;
  const completion = createReplayCompletion();
  let confirmedReceipt: SourceReceipt | null = null;
  let readingDisposition = false;
  let disposed = false;
  const complete = new Promise<void>((resolve, reject) => {
    completeResolve = resolve;
    completeReject = reject;
  });
  // Observe failures immediately even while bootstrap/backpressure is awaiting.
  void complete.catch(() => {});
  const timeout = window.setTimeout(() => {
    if (!completed) completeReject?.(new Error("Live не завершила сохранённую запись вовремя."));
  }, 120_000);

  const client = createLiveClient({
    transport: "wss",
    onState: state => callbacks.onState?.(state),
    onNotice: (kind, error) => {
      if (kind === "connection_error" || kind === "transport_error" || kind === "start_error") {
        const reason = error instanceof Error ? error : new Error(kind);
        if (!completed) completeReject?.(reason);
      }
    },
    onEvent: (event, generation) => {
      callbacks.onPreferenceEvent?.(event, () => client.generation === generation && client.sessionId === event.session_id && source.conversation_id === event.conversation_id);
      callbacks.onEvent?.(event);
      if (client.generation === generation) completion.event(event);
    },
  });

  const abort = () => {
    client.stop({ reason: "actor_change" });
    completeReject?.(new Error("Replay client identity changed."));
  };
  callbacks.signal?.addEventListener("abort", abort, { once: true });
  const dispositionTimer = window.setInterval(() => {
    if (disposed || completed || readingDisposition || !client.sessionId || !completion.canReadDisposition) return;
    readingDisposition = true;
    const epoch = completion.epoch;
    void serverSource(source).then(receipt => {
      if (!disposed && receipt && completion.confirmed(receipt.status, epoch)) {
        confirmedReceipt = receipt;
        completed = true;
        completeResolve?.();
      }
    }).catch(error => completeReject?.(error)).finally(() => { readingDisposition = false; });
  }, 100);

  try {
    if (callbacks.signal?.aborted) throw new Error("Replay client identity changed.");
    const started = await Promise.race([client.start({
      url: `/api/live/${encodeURIComponent(source.conversation_id)}/sessions`,
      body: {
        audio_mode: "buffered",
        client_source_id: source.id,
      },
      microphone: false,
      authorize: async () => {},
    }), complete.then(() => { throw new Error("Replay completed during startup."); })]);
    if (!client.sessionId) {
      throw new Error("Не удалось открыть Live для сохранённой записи.");
    }

    if (started?.source_terminal === true) {
      const terminalStatus = String(started.source_status ?? "");
      client.stop({ reason: "source_already_terminal" });
      await acknowledgeDeliveredSource(source.id);
      return {
        source_id: source.id,
        status: "delivered",
        server_status: terminalStatus,
        skipped_provider: true,
      };
    }

    await client.input({ activity_start: true });
    for (const chunk of chunks) {
      await client.input({
        pcm: new Int16Array(chunk.pcm),
        sample_rate: 16000,
      });
    }
    await client.input({ activity_end: true });
    await complete;

    const playbackDeadline = Date.now() + 10_000;
    while (client.playingCount > 0 && Date.now() < playbackDeadline) {
      await new Promise(resolve => window.setTimeout(resolve, 50));
    }

    const receipt = confirmedReceipt ?? await serverSource(source);
    if (receipt && TERMINAL.has(receipt.status)) {
      await acknowledgeDeliveredSource(source.id);
      return {
        source_id: source.id,
        status: "delivered",
        server_status: receipt.status,
        skipped_provider: false,
      };
    }

    await markLocalSource(
      source.id,
      "pending",
      receipt ? `server_status:${receipt.status}` : "server_receipt_missing",
    );
    callbacks.onNotice?.(
      "Запись услышана Live, но окончательное disposition ещё не подтверждено. Локальная копия сохранена.",
    );
    return {
      source_id: source.id,
      status: "pending",
      server_status: receipt?.status,
      skipped_provider: false,
    };
  } catch (error) {
    await markLocalSource(
      source.id,
      "pending",
      error instanceof Error ? error.message.slice(0, 180) : "replay_failed",
    );
    throw error;
  } finally {
    disposed = true;
    window.clearTimeout(timeout);
    window.clearInterval(dispositionTimer);
    callbacks.signal?.removeEventListener("abort", abort);
    client.stop({ reason: "buffered_replay_complete", preservePlayback: true });
  }
}
