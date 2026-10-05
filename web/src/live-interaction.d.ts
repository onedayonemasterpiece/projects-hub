declare module "@onedayonemasterpiece/live-interaction/browser" {
  export type LiveEvent = {
    type: string;
    seq?: number;
    text?: string;
    name?: string;
    id?: string;
    status?: string;
    code?: string;
    [key: string]: unknown;
  };

  export type DurableAudioMessage =
    | { pcm: Int16Array; sample_rate: 16000; captured_at_ms: number }
    | { audio_stream_end: true; captured_at_ms: number };

  export type LiveClient = {
    readonly sessionId: string | null;
    readonly starting: boolean;
    readonly microphoneEnabled: boolean;
    readonly playingCount: number;
    start(args: {
      url: string;
      body?: Record<string, unknown>;
      authorize?: () => Promise<void>;
      microphone?: boolean;
      captureDuringStart?: boolean;
    }): Promise<Record<string, unknown> | undefined>;
    stop(args?: { reason?: string; keepalive?: boolean; preservePlayback?: boolean }): void;
    input(message: Record<string, unknown>): Promise<unknown>;
    enableMicrophone(args?: Record<string, unknown>): Promise<boolean>;
    disableMicrophone(): void;
  };

  export type DurableMicrophoneCapture = {
    start(options?: Record<string, unknown>): Promise<boolean>;
    stop(): Promise<void>;
    stats(): Record<string, number | boolean>;
    readonly running: boolean;
  };

  export function createLiveClient(options: {
    transport?: "http" | "wss";
    onEvent?: (event: LiveEvent, generation: number) => void;
    onState?: (state: string, detail?: Record<string, unknown>) => void;
    onNotice?: (notice: string, error?: unknown) => void;
    onTiming?: (event: string, metrics?: Record<string, unknown>) => void;
    onWait?: (wait: null | { elapsed_ms: number; stage: string; can_restart: boolean }) => void;
    persistAudio?: ((message: DurableAudioMessage) => Promise<void> | void) | null;
    voiceControl?: null | {
      isStop: (text: string) => boolean;
      confirmation: (text: string) => "confirm" | "cancel" | null;
    };
    suppressCaptureDuringPlayback?: boolean | "adaptive" | (() => boolean | "adaptive");
    speechEndSilenceMs?: number;
    speechStartMs?: number;
    longSpeechEndSilenceMs?: number | null;
    longSpeechAfterMs?: number | null;
    manualActivityDetection?: boolean;
    continuousCapture?: boolean;
  }): LiveClient;

  export function createDurableMicrophoneCapture(options: {
    persist: (message: DurableAudioMessage) => Promise<void> | void;
    onTiming?: (event: string, metrics?: Record<string, unknown>) => void;
    onError?: (error: unknown) => void;
    batchMs?: number;
    maxQueueMs?: number;
    maxAgeMs?: number;
  }): DurableMicrophoneCapture;
}
