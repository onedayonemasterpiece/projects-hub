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
