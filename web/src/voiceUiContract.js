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
 * Sidecar captions are provisional UI only. Interim hypotheses may revise in
 * either direction, but a final sidecar result must never visibly truncate a
 * longer hypothesis that the user has already seen. Mira's canonical
 * input_transcript remains authoritative and may replace the whole caption.
 * @param {string} current
 * @param {string} fragment
 * @param {boolean} final
 * @returns {string}
 */
export function selectProvisionalCaption(current, fragment, final = false) {
  const clean = String(fragment ?? "").trim();
  if (!clean) return current;
  const visible = String(current ?? "").trim();
  if (final && visible.length > clean.length) return current;
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
 * A local VAD speech start is the authoritative UI boundary between user
 * utterances even when the provider emits only final input transcription.
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
