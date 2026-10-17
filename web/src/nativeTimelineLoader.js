/**
 * Refresh one canonical message from a small native revision hint. The queue
 * stores identities and revision watermarks, never timeline text or history.
 */
export function createNativeTimelineLoader({
  load,
  apply,
  isCurrent,
  onError,
  minIntervalMs = 500,
  maxPending = 8,
  clock = {
    now: () => globalThis.performance?.now() ?? Date.now(),
    setTimeout: (callback, delay) => globalThis.setTimeout(callback, delay),
    clearTimeout: timer => globalThis.clearTimeout(timer),
  },
}) {
  if (![load, apply, isCurrent, onError, clock.now, clock.setTimeout, clock.clearTimeout]
    .every(value => typeof value === 'function')) throw new TypeError('Invalid timeline loader callbacks');
  if (!Number.isFinite(minIntervalMs) || minIntervalMs < 0) throw new RangeError('Invalid refresh interval');
  if (!Number.isInteger(maxPending) || maxPending < 1 || maxPending > 8) throw new RangeError('Invalid pending limit');

  // Pending includes the in-flight identity. Retain only 256 recent revision
  // watermarks, including failed reads. An evicted identity may be read again
  // on a future hint; eviction itself never schedules work.
  const pending = new Map();
  const watermarks = new Map();
  const maxWatermarks = 256;
  let inFlight = null;
  let timer = null;
  let lastStarted = -Infinity;
  let closed = false;
  let overflowReported = false;

  function current(context) {
    try { return !closed && Boolean(isCurrent(context)); } catch { return false; }
  }

  function reportError() {
    if (closed) return;
    try { onError(); } catch { /* A UI callback cannot restart or strand the queue. */ }
  }

  function remember(entry, revision, context) {
    const previous = watermarks.get(entry.key);
    retainWatermark(entry.key, {
      revision: previous && current(previous.context) ? Math.max(previous.revision, revision) : revision,
      context,
    });
  }

  function retainWatermark(key, value) {
    watermarks.delete(key);
    watermarks.set(key, value);
    for (const oldest of watermarks.keys()) {
      if (watermarks.size <= maxWatermarks) break;
      // At most eight pending identities exist, so a completed entry is always
      // available for eviction when the 256-entry limit is exceeded.
      if (!pending.has(oldest)) watermarks.delete(oldest);
    }
  }

  function eligible(run) {
    return !closed && inFlight === run && run.entry.version === run.version
      && current(run.context) && current(run.entry.context);
  }

  function finish(run) {
    if (closed || inFlight !== run) return;
    inFlight = null;
    const entry = run.entry;
    pending.delete(entry.key);
    if (current(entry.context) && entry.hint.revision > entry.attemptedRevision) {
      // Give other messages their turn before a newer revision of this one.
      pending.set(entry.key, entry);
    }
    pump();
  }

  async function read(run) {
    try {
      const message = await load(run.hint, run.controller.signal);
      if (!eligible(run)) return;
      if (!validHint(message) || message.id !== run.hint.id
        || message.conversation_id !== run.hint.conversation_id
        || message.revision < run.hint.revision) {
        reportError();
        return;
      }
      // A newer hint can arrive while HTTP is reading an older snapshot.
      if (message.revision < run.entry.hint.revision) return;
      run.entry.attemptedRevision = Math.max(run.entry.attemptedRevision, message.revision);
      remember(run.entry, message.revision, run.context);
      apply(message, run.context);
    } catch {
      if (eligible(run)) reportError();
    } finally {
      finish(run);
    }
  }

  function pump() {
    if (closed || inFlight) return;
    if (timer !== null) {
      clock.clearTimeout(timer);
      timer = null;
    }
    for (const [key, entry] of pending) {
      if (!current(entry.context) || entry.hint.revision <= entry.attemptedRevision) {
        pending.delete(key);
        continue;
      }
      const delay = Math.max(0, lastStarted + minIntervalMs - clock.now());
      if (delay > 0) {
        timer = clock.setTimeout(() => { timer = null; pump(); }, delay);
        return;
      }
      const run = {
        entry,
        hint: entry.hint,
        context: entry.context,
        version: entry.version,
        controller: new AbortController(),
      };
      entry.attemptedRevision = run.hint.revision;
      remember(entry, run.hint.revision, run.context);
      lastStarted = clock.now();
      inFlight = run;
      void read(run);
      return;
    }
  }

  function request(hint, context) {
    if (!current(context) || !validHint(hint)) return false;
    const latest = Object.freeze({ id: hint.id, conversation_id: hint.conversation_id, revision: hint.revision });
    const key = JSON.stringify([latest.conversation_id, latest.id]);
    let entry = pending.get(key);
    if (entry && !current(entry.context)) {
      // A changed actor/session may reuse an identity. Keep the single-flight
      // slot until its aborted read settles, but fence its result with version.
      entry.hint = latest;
      entry.context = context;
      entry.attemptedRevision = -1;
      entry.version += 1;
      watermarks.delete(key);
      if (inFlight?.entry === entry) inFlight.controller.abort();
      pump();
      return true;
    }
    const previous = watermarks.get(key);
    if (previous && current(previous.context) && latest.revision <= previous.revision) {
      retainWatermark(key, previous);
      return true;
    }
    if (previous && !current(previous.context)) watermarks.delete(key);
    if (entry) {
      if (latest.revision > entry.hint.revision) {
        entry.hint = latest;
        entry.context = context;
      }
      pump();
      return true;
    }
    for (const [pendingKey, queued] of pending) {
      if (queued !== inFlight?.entry && !current(queued.context)) pending.delete(pendingKey);
    }
    if (pending.size >= maxPending) {
      if (!overflowReported) {
        overflowReported = true;
        reportError();
      }
      return false;
    }
    entry = { key, hint: latest, context, attemptedRevision: -1, version: 0 };
    pending.set(key, entry);
    pump();
    return true;
  }

  function close() {
    if (closed) return;
    closed = true;
    if (timer !== null) clock.clearTimeout(timer);
    timer = null;
    inFlight?.controller.abort();
    inFlight = null;
    pending.clear();
    watermarks.clear();
  }

  return { request, close };
}

function validHint(value) {
  return value !== null && typeof value === 'object'
    && typeof value.id === 'string' && value.id.length > 0
    && typeof value.conversation_id === 'string' && value.conversation_id.length > 0
    && Number.isSafeInteger(value.revision) && value.revision >= 0;
}
