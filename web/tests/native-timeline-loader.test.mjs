import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createNativeTimelineLoader } from '../src/nativeTimelineLoader.js';

function fakeClock() {
  let time = 0;
  let nextId = 1;
  const timers = new Map();
  return {
    now: () => time,
    setTimeout(callback, delay) {
      const id = nextId++;
      timers.set(id, { at: time + delay, callback });
      return id;
    },
    clearTimeout: id => timers.delete(id),
    advance(duration) {
      const until = time + duration;
      for (;;) {
        const next = [...timers].filter(([, value]) => value.at <= until)
          .sort((a, b) => a[1].at - b[1].at || a[0] - b[0])[0];
        if (!next) break;
        const [id, value] = next;
        timers.delete(id);
        time = value.at;
        value.callback();
      }
      time = until;
    },
    get pendingTimers() { return timers.size; },
  };
}

async function flush() {
  for (let i = 0; i < 8; i += 1) await Promise.resolve();
}

const hint = (revision, id = 'message_1', conversation_id = 'conversation_1') => ({ id, conversation_id, revision });
const message = (revision, id = 'message_1', conversation_id = 'conversation_1') => ({
  ...hint(revision, id, conversation_id), text: `canonical revision ${revision}`,
});

function setup(t, options = {}) {
  const clock = fakeClock();
  const reads = [];
  const applied = [];
  const context = { scope: 'session_1', placeholderId: 'placeholder_1' };
  let scope = context.scope;
  let errors = 0;
  let active = 0;
  let maxActive = 0;
  const loader = createNativeTimelineLoader({
    clock,
    load(request, signal) {
      active += 1;
      maxActive = Math.max(maxActive, active);
      let resolve, reject;
      const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
      reads.push({ hint: request, signal, resolve, reject, at: clock.now() });
      return promise.finally(() => { active -= 1; });
    },
    apply: (value, capturedContext) => applied.push({ message: value, context: capturedContext }),
    isCurrent: candidate => candidate.scope === scope,
    onError: () => { errors += 1; },
    ...options,
  });
  t.after(() => loader.close());
  return {
    loader, clock, reads, applied, context,
    changeScope: value => { scope = value; },
    get errors() { return errors; },
    get maxActive() { return maxActive; },
  };
}

test('a slow read cannot apply below the newest hint; intermediate revisions coalesce', async t => {
  const s = setup(t);
  const laterContext = { ...s.context, placeholderId: 'placeholder_2' };
  assert.equal(s.loader.request(hint(1), s.context), true);
  s.loader.request(hint(2), laterContext);
  s.loader.request(hint(3), laterContext);
  s.loader.request(hint(2), laterContext);
  s.clock.advance(499);
  assert.equal(s.reads.length, 1);
  s.reads[0].resolve(message(1));
  await flush();
  assert.equal(s.applied.length, 0);
  assert.equal(s.reads.length, 1);
  s.clock.advance(1);
  assert.deepEqual(s.reads.map(value => value.hint.revision), [1, 3]);
  const fullMessage = { ...message(3), text: 'я'.repeat(120000) };
  s.reads[1].resolve(fullMessage);
  await flush();
  assert.deepEqual(s.applied, [{ message: fullMessage, context: laterContext }]);
  s.loader.request(hint(3), laterContext);
  s.clock.advance(10000);
  assert.equal(s.reads.length, 2);
  assert.equal(s.maxActive, 1);
  assert.equal(s.errors, 0);
});

test('a newer HTTP revision satisfies hints and applies with the read-start placeholder', async t => {
  const s = setup(t);
  const laterContext = { ...s.context, placeholderId: 'next_turn' };
  s.loader.request(hint(1), s.context);
  s.loader.request(hint(3), laterContext);
  s.reads[0].resolve(message(5));
  await flush();
  assert.equal(s.applied[0].context, s.context);
  for (const revision of [1, 3, 4, 5]) s.loader.request(hint(revision), laterContext);
  s.clock.advance(500);
  assert.equal(s.reads.length, 1);
  s.loader.request(hint(6), laterContext);
  assert.equal(s.reads.length, 2);
  s.reads[1].resolve(message(6));
  await flush();
  assert.equal(s.applied[1].context, laterContext);
  assert.equal(s.errors, 0);
});

for (const [description, response] of [
  ['wrong message', message(1, 'another_message')],
  ['wrong conversation', message(1, 'message_1', 'another_conversation')],
  ['old revision', message(0)],
  ['invalid revision', { ...message(1), revision: 1.5 }],
]) {
  test(`${description} is rejected without retrying the same hint`, async t => {
    const s = setup(t);
    s.loader.request(hint(1), s.context);
    s.reads[0].resolve(response);
    await flush();
    for (let i = 0; i < 20; i += 1) s.loader.request(hint(1), s.context);
    s.clock.advance(10000);
    assert.equal(s.applied.length, 0);
    assert.equal(s.errors, 1);
    assert.equal(s.reads.length, 1);
    assert.equal(s.clock.pendingTimers, 0);
    s.loader.request(hint(2), s.context);
    assert.equal(s.reads.length, 2);
    s.reads[1].resolve(message(2));
    await flush();
    assert.equal(s.applied.length, 1);
  });
}

test('actor/session changes fence a late response and discard stale queued work', async t => {
  const s = setup(t);
  s.loader.request(hint(1), s.context);
  s.loader.request(hint(1, 'message_2'), s.context);
  s.changeScope('session_2');
  assert.equal(s.loader.request(hint(2), s.context), false);
  s.reads[0].resolve(message(1));
  await flush();
  s.clock.advance(10000);
  assert.equal(s.applied.length, 0);
  assert.equal(s.reads.length, 1);
  assert.equal(s.errors, 0);
  const nextContext = { scope: 'session_2', placeholderId: null };
  s.loader.request(hint(1), nextContext);
  s.reads[1].resolve(message(1));
  await flush();
  assert.equal(s.applied[0].context, nextContext);
});

test('a reused identity aborts the stale scope but still waits for its read to settle', async t => {
  const s = setup(t);
  s.loader.request(hint(7), s.context);
  s.changeScope('session_2');
  const nextContext = { scope: 'session_2', placeholderId: 'new_actor_turn' };
  s.loader.request(hint(1), nextContext);
  assert.equal(s.reads[0].signal.aborted, true);
  s.clock.advance(5000);
  assert.equal(s.reads.length, 1);
  // This mock intentionally ignores abort, as an already-completed HTTP parse can.
  s.reads[0].resolve(message(99));
  await flush();
  assert.equal(s.applied.length, 0);
  assert.equal(s.reads.length, 2);
  assert.equal(s.reads[1].hint.revision, 1);
  s.reads[1].resolve(message(1));
  await flush();
  assert.equal(s.applied[0].context, nextContext);
  assert.equal(s.maxActive, 1);
});

test('close aborts the active read, drops queued work and fences a late result', async t => {
  const s = setup(t);
  s.loader.request(hint(1), s.context);
  s.loader.request(hint(1, 'message_2'), s.context);
  s.loader.close();
  s.loader.close();
  assert.equal(s.reads[0].signal.aborted, true);
  assert.equal(s.loader.request(hint(2), s.context), false);
  s.reads[0].resolve(message(1));
  await flush();
  s.clock.advance(10000);
  assert.equal(s.applied.length, 0);
  assert.equal(s.errors, 0);
  assert.equal(s.reads.length, 1);
  assert.equal(s.clock.pendingTimers, 0);
});

test('close cancels a scheduled refresh before it starts', async t => {
  const s = setup(t);
  s.loader.request(hint(1), s.context);
  s.reads[0].resolve(message(1));
  await flush();
  s.loader.request(hint(2), s.context);
  assert.equal(s.clock.pendingTimers, 1);
  s.loader.close();
  s.clock.advance(10000);
  assert.equal(s.reads.length, 1);
  assert.equal(s.clock.pendingTimers, 0);
});

test('eight includes in-flight, overflow reports once, and different messages share one interval', async t => {
  const s = setup(t);
  for (let i = 1; i <= 8; i += 1) {
    assert.equal(s.loader.request(hint(1, `message_${i}`), s.context), true);
  }
  for (let i = 9; i <= 20; i += 1) {
    assert.equal(s.loader.request(hint(1, `message_${i}`), s.context), false);
  }
  assert.equal(s.errors, 1);
  assert.equal(s.reads.length, 1);
  assert.equal(s.loader.request(hint(2, 'message_8'), s.context), true);
  s.clock.advance(10000);
  assert.equal(s.reads.length, 1);
  s.reads[0].resolve(message(1));
  await flush();
  assert.equal(s.reads[1].hint.id, 'message_2');
  assert.equal(s.loader.request(hint(1, 'message_9'), s.context), true);
  assert.equal(s.loader.request(hint(1, 'message_10'), s.context), false);
  assert.equal(s.errors, 1);
  for (let index = 1; index < 9; index += 1) {
    const read = s.reads[index];
    assert.ok(read, `read ${index} exists`);
    read.resolve(message(read.hint.revision, read.hint.id));
    await flush();
    if (index < 8) {
      assert.equal(s.reads.length, index + 1);
      s.clock.advance(499);
      assert.equal(s.reads.length, index + 1);
      s.clock.advance(1);
    }
  }
  assert.deepEqual(s.reads.map(value => value.hint.id), Array.from({ length: 9 }, (_, i) => `message_${i + 1}`));
  assert.equal(s.reads[7].hint.revision, 2);
  assert.equal(s.applied.length, 9);
  assert.equal(s.maxActive, 1);
  assert.equal(s.clock.pendingTimers, 0);
});

test('failure has no retry storm; only a newer coalesced hint permits another read', async t => {
  const s = setup(t);
  s.loader.request(hint(1), s.context);
  s.reads[0].reject(new Error('offline'));
  await flush();
  for (let i = 0; i < 100; i += 1) s.loader.request(hint(1), s.context);
  s.clock.advance(100000);
  assert.equal(s.reads.length, 1);
  assert.equal(s.errors, 1);
  assert.equal(s.clock.pendingTimers, 0);
  s.loader.request(hint(2), s.context);
  s.loader.request(hint(3), s.context);
  s.loader.request(hint(4), s.context);
  s.reads[1].reject(new Error('still offline'));
  await flush();
  s.clock.advance(500);
  assert.deepEqual(s.reads.map(value => value.hint.revision), [1, 2, 4]);
  s.reads[2].reject(new Error('still offline'));
  await flush();
  for (let i = 0; i < 100; i += 1) s.loader.request(hint(4), s.context);
  s.clock.advance(100000);
  assert.equal(s.reads.length, 3);
  assert.equal(s.errors, 3);
  assert.equal(s.clock.pendingTimers, 0);
  assert.equal(s.maxActive, 1);
});

test('a repeatedly changing message yields its next read to other pending messages', async t => {
  const s = setup(t);
  s.loader.request(hint(1), s.context);
  s.loader.request(hint(2), s.context);
  s.loader.request(hint(1, 'message_2'), s.context);
  s.reads[0].resolve(message(1));
  await flush();
  s.clock.advance(500);
  assert.equal(s.reads[1].hint.id, 'message_2');
  s.reads[1].resolve(message(1, 'message_2'));
  await flush();
  s.clock.advance(500);
  assert.equal(s.reads[2].hint.id, 'message_1');
  assert.equal(s.reads[2].hint.revision, 2);
  s.reads[2].resolve(message(2));
  await flush();
  assert.equal(s.applied.length, 2);
});

test('hints are copied before a caller can mutate their identity or revision', async t => {
  const s = setup(t);
  const request = hint(1);
  s.loader.request(request, s.context);
  request.id = 'changed';
  request.revision = 99;
  assert.deepEqual(s.reads[0].hint, hint(1));
  s.reads[0].resolve(message(1));
  await flush();
  assert.equal(s.applied.length, 1);
});

test('a long session retains only the most recent 256 completed identities', async t => {
  const s = setup(t, { minIntervalMs: 0 });
  for (let i = 0; i < 320; i += 1) {
    s.loader.request(hint(1, `history_${i}`), s.context);
    s.reads.at(-1).resolve(message(1, `history_${i}`));
    await flush();
  }
  assert.equal(s.reads.length, 320);
  // The complete retained window deduplicates, including its oldest entry.
  for (let i = 64; i < 320; i += 1) s.loader.request(hint(1, `history_${i}`), s.context);
  assert.equal(s.reads.length, 320);
  s.loader.request(hint(1, 'history_63'), s.context);
  assert.equal(s.reads.length, 321, 'an entry just outside the window was evicted');
  s.reads.at(-1).resolve(message(1, 'history_63'));
  await flush();
  s.clock.advance(100000);
  assert.equal(s.reads.length, 321, 'eviction does not schedule a refresh');
  assert.equal(s.maxActive, 1);
  assert.equal(s.errors, 0);
});

test('LRU keeps a recently hinted failure and protects pending revisions during eviction', async t => {
  const s = setup(t, { minIntervalMs: 0 });
  s.loader.request(hint(1, 'failed'), s.context);
  s.reads[0].reject(new Error('offline'));
  await flush();
  for (let i = 0; i < 255; i += 1) {
    s.loader.request(hint(1, `history_${i}`), s.context);
    s.reads.at(-1).resolve(message(1, `history_${i}`));
    await flush();
  }
  // Refresh the oldest failed watermark by hint alone, without retrying HTTP.
  s.loader.request(hint(1, 'failed'), s.context);
  assert.equal(s.reads.length, 256);
  s.loader.request(hint(1, 'pending'), s.context); // Evicts history_0.
  s.loader.request(hint(3, 'pending'), s.context);
  s.loader.request(hint(1, 'next'), s.context);
  for (let i = 1; i < 255; i += 1) s.loader.request(hint(1, `history_${i}`), s.context);
  s.loader.request(hint(1, 'failed'), s.context);
  // Pending is now the least recently used watermark, but still owns work.
  s.reads.at(-1).resolve(message(1, 'pending'));
  await flush();
  assert.equal(s.reads.at(-1).hint.id, 'next');
  s.reads.at(-1).resolve(message(1, 'next'));
  await flush();
  assert.deepEqual(s.reads.at(-1).hint, hint(3, 'pending'));
  s.reads.at(-1).reject(new Error('offline again'));
  await flush();
  const completedReads = s.reads.length;
  for (let i = 0; i < 100; i += 1) {
    s.loader.request(hint(1, 'failed'), s.context);
    s.loader.request(hint(3, 'pending'), s.context);
  }
  assert.equal(s.reads.length, completedReads);
  assert.equal(s.errors, 2);
  // Insertion of next skipped pending and evicted the next oldest completed id.
  s.loader.request(hint(1, 'history_1'), s.context);
  assert.equal(s.reads.length, completedReads + 1);
  s.reads.at(-1).resolve(message(1, 'history_1'));
  await flush();
  assert.equal(s.maxActive, 1);
  assert.equal(s.clock.pendingTimers, 0);
});
