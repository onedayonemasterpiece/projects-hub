export type NativeVoiceStatus = 'idle' | 'starting' | 'live' | 'delayed' | 'recording_only' | 'stopping' | 'error' | 'unsupported';
export type NativeVoiceState = Readonly<{
  owner_id: string;
  revision: number;
  status: NativeVoiceStatus;
  capturing: boolean;
  playing: boolean;
  conversation_id: string | null;
  session_id: string | null;
  source_id: string | null;
  /** Local durable spool identity; API/timeline references use source_id above. */
  local_source_id?: string | null;
  queued_audio_ms: number;
  code?: string | null;
}>;
export type NativeVoiceCapabilities = Readonly<{
  protocol: string;
  native_capture: boolean;
  compressed_input: boolean;
  compressed_output: boolean;
}>;
export type NativeVoiceBridge = {
  postMessage(message: string): void;
  onmessage: ((event: { data: string }) => void) | null;
};
export class NativeVoiceError extends Error { readonly code: string; constructor(code: string); }
export function nativeVoiceSupported(capabilities: unknown): boolean;
export function createNativeVoiceAdapter(options?: {
  bridge?: NativeVoiceBridge;
  onState?: (state: NativeVoiceState) => void;
  onEvent?: (event: Record<string, unknown> & { type: string }) => void;
  onError?: (error: NativeVoiceError) => void;
  timeoutMs?: number;
}): {
  attach(): Promise<{ state: NativeVoiceState; capabilities: NativeVoiceCapabilities | null; supported: boolean }>;
  /** Accepted user intent; actual capture/readiness comes from onState. */
  start(options: { conversationId: string }): Promise<boolean>;
  stop(options?: { reason?: 'user' | 'logout' | 'account_changed' }): Promise<void>;
  /** Closes the current speech portion; capture and conversation continue. */
  finishTurn(): Promise<boolean>;
  getState(): Promise<NativeVoiceState>;
  /** Releases this UI subscription, never the native voice owner. */
  detach(): void;
  readonly state: NativeVoiceState | null;
  readonly sessionId: string | null;
  readonly conversationId: string | null;
  readonly capabilities: NativeVoiceCapabilities | null;
};
