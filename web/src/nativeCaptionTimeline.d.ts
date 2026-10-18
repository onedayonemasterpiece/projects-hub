export type NativeCaptionScope = {
  session_id: string; conversation_id: string; source_id: string;
  utterance_id: string; turn_id: string; utterance_sequence: number;
};
export type NativeCaptionState = {
  sessionId: string | null; conversationId: string | null;
  active: null | { scope: NativeCaptionScope; ended: boolean; canonical: boolean; canonicalRevision: number };
  highestSequence: number; awaitingAcceptedStart: boolean; text: string; unavailable: boolean;
};
export function createNativeCaptionState(): NativeCaptionState;
export function reduceNativeCaption(state: NativeCaptionState, event: Record<string, unknown>,
  context: { sessionId: string | null; conversationId: string | null }): NativeCaptionState;
