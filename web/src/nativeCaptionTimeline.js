// Native captions are speculative display only. Accepted-audio identity comes
// from the server; local VAD and provider event arrival order cannot invent it.
export function createNativeCaptionState() {
  return { sessionId: null, conversationId: null, active: null, highestSequence: 0,
    awaitingAcceptedStart: false, text: "", unavailable: false };
}

const ids = ["session_id", "conversation_id", "source_id", "utterance_id", "turn_id"];
function scopeOf(value, context) {
  if (!value || typeof value !== "object" || ids.some(key => typeof value[key] !== "string"
      || !value[key] || value[key].length > 200)
      || !Number.isSafeInteger(value.utterance_sequence) || value.utterance_sequence < 1
      || value.session_id !== context.sessionId || value.conversation_id !== context.conversationId) return null;
  return Object.fromEntries([...ids, "utterance_sequence"].map(key => [key, value[key]]));
}
function sameScope(left, right) {
  return left && right && [...ids, "utterance_sequence"].every(key => left[key] === right[key]);
}

export function reduceNativeCaption(previous, event, context) {
  if (event.type === "caption_reset" || !context.sessionId || !context.conversationId) {
    return createNativeCaptionState();
  }
  const state = previous.sessionId === context.sessionId && previous.conversationId === context.conversationId
    ? previous : { ...createNativeCaptionState(), ...context };
  if (event.type === "caption_local_speech_start") {
    return { ...state, text: "", awaitingAcceptedStart: true };
  }
  if (event.type === "caption_unavailable") return { ...state, text: "", unavailable: true };
  if (event.type === "caption_ready") return { ...state, unavailable: false };
  if (event.type === "caption_canonical") {
    const item = event.timeline_message;
    const active = state.active;
    if (!active || item?.role !== "user" || item.conversation_id !== state.conversationId
        || item.turn_id !== active.scope.turn_id || item.source_id !== active.scope.source_id
        || !Number.isSafeInteger(item.revision) || item.revision <= active.canonicalRevision) return state;
    return { ...state, text: "", active: { ...active, canonical: true, canonicalRevision: item.revision } };
  }
  const scope = scopeOf(event.caption_scope, context);
  if (!scope) return state;
  if (event.type === "caption_turn_started") {
    if (scope.utterance_sequence <= state.highestSequence) return state;
    return { ...state, text: "", highestSequence: scope.utterance_sequence, awaitingAcceptedStart: false,
      active: { scope, ended: false, canonical: false, canonicalRevision: 0 } };
  }
  const active = state.active;
  if (!active || !sameScope(scope, active.scope)) return state;
  if (event.type === "caption_turn_ended") {
    return { ...state, text: active.canonical ? "" : state.text, active: { ...active, ended: true } };
  }
  if (!["caption_interim_transcript", "caption_final_transcript"].includes(event.type)
      || state.awaitingAcceptedStart || (active.ended && active.canonical)
      || typeof event.text !== "string" || !event.text.trim()) return state;
  const timing = event.caption_timing;
  if (timing?.offset_clock !== "connection_pcm" || timing.assigned !== true
      || !Number.isSafeInteger(timing.provider_connection_generation)
      || timing.provider_connection_generation < 1) return state;
  // A canonical fragment while accepted speech remains open does not finish
  // the whole utterance. Later aligned hypotheses for that open turn may update.
  return { ...state, text: event.text.trim(), unavailable: false };
}
