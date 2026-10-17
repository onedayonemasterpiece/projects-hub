export type VoiceSourceSetup = {
  state: 'ready' | 'source_binding_required' | 'unavailable';
  can_connect_source: boolean;
  source: { repository: string; prefix: string; access_mode: 'read_only' };
  reason?: string;
};
export type VoiceSourceSetupState = {
  setup: VoiceSourceSetup | null;
  busy: boolean;
  connected: boolean;
  notice: string | null;
};
export function canConnectVoiceSource(setup: VoiceSourceSetup | null): boolean;
export function createVoiceSourceSetup(
  request: (path: string, options?: RequestInit) => Promise<VoiceSourceSetup>,
  onChange: (state: VoiceSourceSetupState) => void,
): { load(): Promise<void>; connect(): Promise<void>; dispose(): void };
