import {
  createLiveClient,
  type LiveEvent,
} from "@onedayonemasterpiece/live-interaction/browser";

export type ServerRecoveryCallbacks = {
  onState?: (state: string) => void;
  onEvent?: (event: LiveEvent) => void;
  onNotice?: (message: string) => void;
};

export type ServerRecoveryResult = {
  original_source_id: string;
  recovered_source_id: string;
  client_source_id: string;
};

function recoveryClientSourceId(sourceId: string) {
  const match = /^src_([0-9a-f]{32})$/.exec(sourceId);
  if (!match) throw new Error("Некорректный идентификатор сохранённой записи.");
  return "local_" + match[1];
}

export async function recoverServerVoiceSource(
  conversationId: string,
  sourceId: string,
  options: {
    clientVersion?: string | null;
    clientTimezone?: string | null;
    callbacks?: ServerRecoveryCallbacks;
  } = {},
): Promise<ServerRecoveryResult> {
  const response = await fetch(
    `/api/conversations/${encodeURIComponent(conversationId)}/sources/${encodeURIComponent(sourceId)}/audio`,
    { credentials: "same-origin", cache: "no-store" },
  );
  if (!response.ok) {
    throw new Error("Сохранённое аудио недоступно для восстановления.");
  }
  const raw = await response.arrayBuffer();
  if (!raw.byteLength || raw.byteLength % 2) {
    throw new Error("Сохранённое аудио повреждено или пусто.");
  }
  const pcm = new Int16Array(raw);
  const callbacks = options.callbacks ?? {};
  const clientSourceId = recoveryClientSourceId(sourceId);
  let completed = false;
  let resolveComplete: (() => void) | null = null;
  let rejectComplete: ((reason?: unknown) => void) | null = null;
  const complete = new Promise<void>((resolve, reject) => {
    resolveComplete = resolve;
    rejectComplete = reject;
  });
  const timeout = window.setTimeout(() => {
    if (!completed) rejectComplete?.(new Error("Восстановление не завершилось вовремя."));
  }, 180_000);

  const client = createLiveClient({
    transport: "wss",
    onState: state => callbacks.onState?.(state),
    onNotice: (kind, error) => {
      if (["connection_error", "transport_error", "start_error", "resource_denial", "provider_failure"].includes(kind)) {
        if (!completed) {
          rejectComplete?.(error instanceof Error ? error : new Error(kind));
        }
      }
    },
    onEvent: event => {
      callbacks.onEvent?.(event);
      if (event.type === "turn_complete" && !completed) {
        completed = true;
        resolveComplete?.();
      }
    },
  });

  try {
    const started = await client.start({
      url: `/api/live/${encodeURIComponent(conversationId)}/sessions`,
      body: {
        audio_mode: "buffered",
        recovery_only: true,
        client_source_id: clientSourceId,
        ...(options.clientVersion ? { client_version: options.clientVersion } : {}),
        ...(options.clientTimezone ? { client_timezone: options.clientTimezone } : {}),
      },
      microphone: false,
      authorize: async () => {},
    });
    const recoveredSourceId = String(started?.source_id ?? "");
    if (!client.sessionId || !recoveredSourceId) {
      throw new Error("Live не открыла recovery-сессию.");
    }

    await client.input({ activity_start: true });
    const samplesPerChunk = 4000;
    for (let offset = 0; offset < pcm.length; offset += samplesPerChunk) {
      await client.input({
        pcm: pcm.slice(offset, Math.min(pcm.length, offset + samplesPerChunk)),
        sample_rate: 16000,
      });
    }
    await client.input({ activity_end: true });
    await complete;

    callbacks.onNotice?.(
      "Сохранённая реплика повторно обработана той же Live-моделью без выполнения старых действий.",
    );
    return {
      original_source_id: sourceId,
      recovered_source_id: recoveredSourceId,
      client_source_id: clientSourceId,
    };
  } finally {
    window.clearTimeout(timeout);
    client.stop({ reason: "source_recovery_complete", preservePlayback: true });
  }
}