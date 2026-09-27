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

const TERMINAL = new Set(["archived", "ephemeral_processed"]);

function pcmBase64(buffer: ArrayBuffer): string {
  const bytes = new Uint8Array(buffer);
  let text = "";
  for (let offset = 0; offset < bytes.length; offset += 0x8000) {
    text += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
  }
  return btoa(text);
}

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
  onNotice?: (message: string) => void;
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
  const complete = new Promise<void>((resolve, reject) => {
    completeResolve = resolve;
    completeReject = reject;
  });
  const timeout = window.setTimeout(() => {
    if (!completed) completeReject?.(new Error("Live не завершила сохранённую запись вовремя."));
  }, 120_000);

  const client = createLiveClient({
    onState: state => callbacks.onState?.(state),
    onNotice: (kind, error) => {
      if (kind === "connection_error" || kind === "transport_error" || kind === "start_error") {
        const reason = error instanceof Error ? error : new Error(kind);
        if (!completed) completeReject?.(reason);
      }
    },
    onEvent: event => {
      callbacks.onEvent?.(event);
      if (event.type === "turn_complete" && !completed) {
        completed = true;
        completeResolve?.();
      }
    },
  });

  try {
    const started = await client.start({
      url: `/api/live/${encodeURIComponent(source.conversation_id)}/sessions`,
      body: {
        audio_mode: "buffered",
        client_source_id: source.id,
      },
      microphone: false,
      authorize: async () => {},
    });
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
        audio_base64: pcmBase64(chunk.pcm),
      });
    }
    await client.input({ activity_end: true });
    await complete;

    const playbackDeadline = Date.now() + 10_000;
    while (client.playingCount > 0 && Date.now() < playbackDeadline) {
      await new Promise(resolve => window.setTimeout(resolve, 50));
    }

    const receipt = await serverSource(source);
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
    window.clearTimeout(timeout);
    client.stop({ reason: "buffered_replay_complete", preservePlayback: true });
  }
}
