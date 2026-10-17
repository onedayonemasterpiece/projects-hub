export type NativeTimelineHint = Readonly<{
  id: string;
  conversation_id: string;
  revision: number;
}>;

/** Optional deterministic clock for offline tests. Production uses browser timers. */
export type NativeTimelineLoaderClock = {
  now(): number;
  setTimeout(callback: () => void, delayMs: number): unknown;
  clearTimeout(timer: unknown): void;
};

export function createNativeTimelineLoader<Context, Message extends NativeTimelineHint>(options: {
  load: (hint: NativeTimelineHint, signal: AbortSignal) => Promise<Message>;
  /** Receives the context captured when this particular read started. */
  apply: (message: Message, context: Context) => void;
  isCurrent: (context: Context) => boolean;
  onError: () => void;
  /** Minimum interval between read starts, including different messages; default 500. */
  minIntervalMs?: number;
  /** Includes the in-flight identity; integer 1..8, default 8. */
  maxPending?: number;
  clock?: NativeTimelineLoaderClock;
}): {
  /**
   * True means accepted or deduplicated, false means stale, invalid, closed, or full.
   * Deduplication retains 256 recent identities, protecting pending reads. A hint
   * for an evicted identity may trigger a new read; eviction never retries itself.
   */
  request(hint: NativeTimelineHint, context: Context): boolean;
  /** Aborts the active read and prevents all late callbacks from changing the UI. */
  close(): void;
};
