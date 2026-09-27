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

  export type LiveClient = {
    readonly sessionId: string | null;
    readonly starting: boolean;
    start(args: {
      url: string;
      body?: Record<string, unknown>;
      authorize?: () => Promise<void>;
      microphone?: boolean;
      captureDuringStart?: boolean;
    }): Promise<void>;
    stop(args?: { reason?: string; keepalive?: boolean; preservePlayback?: boolean }): void;
    input(message: Record<string, unknown>): Promise<unknown>;
  };

  export function createLiveClient(options: {
    onEvent?: (event: LiveEvent, generation: number) => void;
    onState?: (state: string, detail?: Record<string, unknown>) => void;
    onNotice?: (notice: string, error?: unknown) => void;
    onTiming?: (event: string, metrics?: Record<string, unknown>) => void;
    onWait?: (wait: null | { elapsed_ms: number; stage: string; can_restart: boolean }) => void;
  }): LiveClient;
}
