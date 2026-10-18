/** Production voice-UI contracts shared with executable Node tests. */

/**
 * Merge cumulative/overlapping final transcript fragments without truncation.
 * Provider interim text is intentionally kept separate and is never fed here.
 * @param {string} current
 * @param {string} fragment
 * @returns {string}
 */
export function mergeTranscript(current, fragment) {
  const clean = String(fragment ?? "").trim();
  if (!clean) return current;
  if (!current) return clean;
  if (clean.startsWith(current)) return clean;
  if (current.endsWith(clean)) return current;
  let overlap = Math.min(current.length, clean.length);
  while (overlap >= 3 && current.slice(-overlap) !== clean.slice(0, overlap)) overlap -= 1;
  return overlap >= 3 ? current + clean.slice(overlap) : current + " " + clean;
}

/**
 * Sidecar captions are provisional UI only. Both interim hypotheses and final
 * results may correct earlier text in either direction. Mira's canonical
 * input_transcript remains authoritative and may replace the whole caption.
 * @param {string} current
 * @param {string} fragment
 * @param {boolean} _final
 * @returns {string}
 */
export function selectProvisionalCaption(current, fragment, _final = false) {
  const clean = String(fragment ?? "").trim();
  if (!clean) return current;
  return clean;
}

export const TERMINAL_VOICE_REASONS = Object.freeze([
  "resource_denial",
  "provider_failure",
  "connection_failure",
  "capture_error",
]);

/**
 * Preserve a typed terminal reason through shared-client cleanup instead of
 * rendering technical failure as a normal idle/off state.
 * @param {string} state
 * @param {unknown} detail
 * @returns {string}
 */
export function resolveTerminalVoiceState(state, detail) {
  const reason = detail && typeof detail === "object" && "reason" in detail
    ? String(detail.reason ?? "")
    : "";
  return state === "off" && TERMINAL_VOICE_REASONS.includes(reason) ? reason : "";
}


/**
 * Legacy PWA uses local VAD to reserve its next user message. Native capture
 * uses the same event for voice status; its message identities come from the server.
 * @param {string} event
 * @returns {boolean}
 */
export function speechStartsNewUserBubble(event) {
  return event === "speech_start";
}

/**
 * A filler utterance ending is not completion of the underlying operation.
 * Match receipts/cancellation by call id so a late old result cannot clear a
 * newer wait. This is presentation state; durable action receipts stay server-side.
 * @param {null | { id: string, elapsed_ms: number }} current
 * @param {{ type: string, id?: unknown, ids?: unknown, status?: unknown, elapsed_ms?: unknown }} event
 * @returns {null | { id: string, elapsed_ms: number }}
 */
export function reduceVoiceToolProgress(current, event) {
  if (event.type === "speech_start" || event.type === "voice_stopped" || event.type === "interrupted") return null;
  if (event.type === "tool_progress" && event.status === "pending"
      && typeof event.id === "string" && event.id.length > 0 && event.id.length <= 256
      && typeof event.elapsed_ms === "number" && Number.isFinite(event.elapsed_ms) && event.elapsed_ms >= 0) {
    return { id: event.id, elapsed_ms: event.elapsed_ms };
  }
  if (current && ((event.type === "tool_result" && event.id === current.id)
      || (event.type === "tool_cancelled" && Array.isArray(event.ids) && event.ids.includes(current.id)))) return null;
  return current;
}
/**
 * Translate a failed Live *session start* into user-facing status. A provider
 * capacity refusal is not a lost chat, completed task or failed development.
 * The voice model cannot speak while its session cannot be established.
 * @param {unknown} error
 * @returns {"resource_denial"|"provider_failure"}
 */
export function voiceStartupFailureState(error) {
  const code = error && typeof error === "object" && "code" in error ? String(error.code ?? "") : "";
  return code.startsWith("RESOURCE_") ? "resource_denial" : "provider_failure";
}

export function voiceStartupFailureNotice(error) {
  if (voiceStartupFailureState(error) === "resource_denial") {
    const retry = error && typeof error === "object" && "retry_after_ms" in error ? error.retry_after_ms : null;
    const wait = typeof retry === "number" && Number.isFinite(retry) && retry > 0
      ? ` Повторите подключение не раньше чем через ${Math.ceil(Math.min(86400000, retry) / 1000)} с.`
      : " Повторите подключение позже.";
    return "Подключение Миры отклонено ресурсным лимитом. Переписка сохранена." + wait;
  }
  const status = error && typeof error === "object" && "status" in error
    ? Number(error.status)
    : null;
  const message = error instanceof Error ? error.message : "";
  if (status !== 503 && !/\bHTTP\s+503\b/i.test(message)) return null;
  return "Голосовая модель Миры временно недоступна (503). Переписка сохранена. "
    + "Попробуйте подключиться позже. Если фраза прервалась, её запись можно "
    + "восстановить в этом же диалоге.";
}

/**
 * Android sends bounded error codes, not exception messages or HTTP statuses.
 * In particular a 401 becomes UNAUTHENTICATED through NativeSessionApi. Keep
 * these product/native codes visible without mistaking every failure for capture.
 * This is presentation only; the native state remains the authority for Stop.
 * @param {unknown} error
 * @returns {{state: string, notice: string, reauthenticate: boolean, microphoneSettings: boolean}}
 */
export function nativeVoiceFailure(error) {
  const value = typeof error === "string" ? error
    : error && typeof error === "object" && "code" in error ? error.code : null;
  const status = error && typeof error === "object" && "status" in error ? Number(error.status) : null;
  const code = typeof value === "string" && /^[A-Z0-9_]{1,96}$/.test(value) ? value
    : Number.isInteger(status) ? `HTTP_${status}` : "";
  const failure = {
    state: "connection_error",
    notice: "Не удалось включить голосовую связь. Откройте приложение снова и повторите попытку. Переписка остаётся доступной.",
    reauthenticate: false,
    microphoneSettings: false,
  };
  if (code.startsWith("RESOURCE_")) return { ...failure, state: "resource_denial",
    notice: voiceStartupFailureNotice({ code }) ?? failure.notice };
  switch (code) {
    case "UNAUTHENTICATED":
    case "NATIVE_AUTH_REQUIRED":
    case "NATIVE_IDENTITY_CHANGED":
    case "NATIVE_VOICE_AUTH_REQUIRED":
    case "HTTP_401":
    case "HTTP_403":
      return { ...failure, reauthenticate: true,
        notice: "Не удалось подтвердить вход. Войдите снова, чтобы продолжить разговор." };
    case "NATIVE_VOICE_MICROPHONE_DENIED":
    case "NATIVE_VOICE_MICROPHONE_REQUIRED":
    case "LIVE_NATIVE_MICROPHONE_PERMISSION":
      return { ...failure, state: "microphone_unavailable", microphoneSettings: true,
        notice: "Нет доступа к микрофону. Разрешите микрофон в настройках приложения и проверьте системный переключатель доступа к нему." };
    case "NATIVE_VOICE_PROTOCOL_UNAVAILABLE":
      return { ...failure, state: "native_unsupported",
        notice: "Приложение и сервер используют несовместимые версии голосовой связи. Требуется обновление сервера и интерфейса; переписка остаётся доступной." };
    case "NATIVE_VOICE_UNSUPPORTED":
    case "NATIVE_VOICE_CODEC_UNAVAILABLE":
    case "LIVE_NATIVE_CODEC_PROBE_FAILED":
    case "LIVE_NATIVE_COMPRESSED_UNSUPPORTED":
      return { ...failure, state: "native_unsupported",
        notice: "На этом устройстве не удалось включить сжатую голосовую связь. Переписка остаётся доступной." };
    case "VOICE_STORE_QUOTA":
    case "VOICE_STORE_RECORD_LIMIT":
    case "SOURCE_STORAGE_FULL":
    case "NATIVE_STORAGE_FULL":
      return { ...failure, state: "capture_error",
        notice: "Место для голосовых записей заполнено. Новая запись остановлена; уже сохранённые фрагменты остаются доступными." };
    case "VOICE_STORE_CORRUPT":
    case "VOICE_STORE_UNAVAILABLE":
    case "VOICE_STORE_IN_USE":
    case "NATIVE_VOICE_STORAGE_UNAVAILABLE":
    case "LIVE_NATIVE_SOURCE_STORE_FAILED":
      return { ...failure, state: "capture_error",
        notice: "Не удалось сохранить голосовую запись. Проверьте свободное место и снова откройте приложение. Сохранённые записи автоматически не удаляются." };
    case "LIVE_NATIVE_STORAGE_BACKPRESSURE":
    case "LIVE_NATIVE_OUTPUT_STORAGE_BACKPRESSURE":
      return { ...failure, state: "capture_error",
        notice: "Устройство не успевает обрабатывать звук. Голосовой разговор остановлен; сохранённая часть записи остаётся в приложении. Подождите немного и попробуйте снова." };
    case "LIVE_NATIVE_SESSION_UNAVAILABLE":
    case "LIVE_NATIVE_SOCKET_UNAVAILABLE":
    case "NATIVE_VOICE_SESSION_BACKPRESSURE":
    case "HTTP_502":
    case "HTTP_503":
    case "HTTP_504":
      return { ...failure,
        notice: "Не удалось соединиться с сервером голосовой связи. Проверьте подключение и повторите попытку. Переписка остаётся доступной." };
    case "NATIVE_VOICE_FOREGROUND_REQUIRED":
      return { ...failure,
        notice: "Чтобы начать разговор, откройте приложение на экране и снова нажмите микрофон." };
    case "LIVE_NATIVE_AUDIO_FOCUS_DENIED":
    case "LIVE_NATIVE_AUDIO_FOCUS_LOST":
    case "LIVE_NATIVE_CAPTURE_START_FAILED":
    case "LIVE_NATIVE_CAPTURE_READ_FAILED":
    case "LIVE_NATIVE_CAPTURE_FAILED":
    case "LIVE_NATIVE_MICROPHONE_UNSUPPORTED":
    case "LIVE_NATIVE_CAPTURE_UNSUPPORTED":
      return { ...failure, state: "capture_error",
        notice: "Не удалось включить или продолжить запись с микрофона. Завершите другие звонки и снова начните разговор." };
    default:
      return failure;
  }
}
