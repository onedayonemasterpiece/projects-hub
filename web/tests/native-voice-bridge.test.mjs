import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createNativeVoiceAdapter, nativeVoiceSupported } from '../src/nativeVoiceBridge.js';

const capabilities = { protocol: 'wl-live-v2', native_capture: true, compressed_input: true, compressed_output: true };
const state = (overrides = {}) => ({ owner_id: 'owner_1', revision: 1, status: 'idle', capturing: false,
  playing: false, conversation_id: null, session_id: null, source_id: null, queued_audio_ms: 0, ...overrides });
function setup(options = {}) {
  const sent = [], states = [], events = [];
  const bridge = { onmessage: null, postMessage: raw => sent.push(JSON.parse(raw)) };
  const adapter = createNativeVoiceAdapter({ bridge, onState: value => states.push(value),
    onEvent: value => events.push(value), ...options });
  const send = message => bridge.onmessage?.({ data: JSON.stringify({ version: 1, ...message }) });
  const ack = (request, fields = {}) => send({ type: 'ack', request_id: request.request_id, ok: true,
    state: state(), capabilities, ...fields });
  return { adapter, bridge, sent, states, events, send, ack };
}

test('real handshake requires both compressed directions and native ownership', async () => {
  assert.equal(nativeVoiceSupported(null), false);
  assert.equal(nativeVoiceSupported({ ...capabilities, protocol: 'wl-live-v1' }), false);
  assert.equal(nativeVoiceSupported({ ...capabilities, compressed_output: false }), false);
  const t = setup();
  const attached = t.adapter.attach();
  assert.equal(t.sent[0].type, 'attach');
  t.ack(t.sent[0]);
  assert.equal((await attached).supported, true);
  t.adapter.detach();
});

test('unsupported native handshake never silently starts the browser or a PCM profile', async () => {
  const t = setup();
  const start = t.adapter.start({ conversationId: 'conversation_A' });
  t.ack(t.sent[0], { capabilities: { ...capabilities, compressed_input: false }, state: state({ status: 'unsupported' }) });
  await assert.rejects(start, { code: 'NATIVE_VOICE_UNSUPPORTED' });
  assert.deepEqual(t.sent.map(message => message.type), ['attach']);
  t.adapter.detach();
});

test('fresh Stop does not wait for handshake and invalidates pending Start intent', async () => {
  const t = setup();
  const start = t.adapter.start({ conversationId: 'conversation_A' });
  const stop = t.adapter.stop();
  assert.deepEqual(t.sent.map(message => message.type), ['attach', 'stop']);
  t.ack(t.sent[1]);
  await stop;
  t.ack(t.sent[0]);
  assert.equal(await start, false);
  assert.equal(t.sent.some(message => message.type === 'start'), false);
  t.adapter.detach();
});

test('start acknowledgement is accepted intent; actual capture comes from state', async () => {
  const t = setup();
  const attached = t.adapter.attach(); t.ack(t.sent[0]); await attached;
  const start = t.adapter.start({ conversationId: 'conversation_A' });
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(Object.keys(t.sent[1]).sort(), ['conversation_id', 'request_id', 'type', 'version']);
  t.ack(t.sent[1], { state: state({ revision: 2, status: 'starting', conversation_id: 'conversation_A' }) });
  assert.equal(await start, true);
  assert.equal(t.adapter.state.capturing, false);
  t.send({ type: 'state', state: state({ revision: 3, status: 'recording_only', capturing: true,
    conversation_id: 'conversation_A', source_id: 'local_source', queued_audio_ms: 1200 }) });
  assert.equal(t.adapter.state.status, 'recording_only');
  assert.equal(t.adapter.state.capturing, true);
  t.adapter.detach();
});

test('reload detaches only UI, rejects pending calls, and allows a new UI owner', async () => {
  const t = setup();
  const read = t.adapter.getState();
  const rejected = assert.rejects(read, { code: 'NATIVE_VOICE_DETACHED' });
  assert.throws(() => createNativeVoiceAdapter({ bridge: t.bridge }), { code: 'NATIVE_VOICE_UI_ALREADY_ATTACHED' });
  t.adapter.detach();
  await rejected;
  assert.deepEqual(t.sent.map(message => message.type), ['state', 'detach']);
  const next = createNativeVoiceAdapter({ bridge: t.bridge });
  next.detach();
});

test('late state or retired native owner cannot roll UI back', async () => {
  const t = setup();
  t.send({ type: 'state', state: state({ revision: 5, status: 'live', capturing: true }) });
  t.send({ type: 'state', state: state({ revision: 4, status: 'idle' }) });
  assert.equal(t.adapter.state.status, 'live');
  t.send({ type: 'state', state: state({ owner_id: 'owner_2', status: 'idle' }) });
  t.send({ type: 'state', state: state({ revision: 6, status: 'live', capturing: true }) });
  assert.equal(t.adapter.state.owner_id, 'owner_2');
  assert.equal(t.states.length, 2);
  t.adapter.detach();
});

test('bridge carries semantic events, never encoded audio through base64 JSON', () => {
  const t = setup();
  t.send({ type: 'event', event: { type: 'transcript', text: 'Тест' } });
  t.send({ type: 'event', event: { type: 'audio', data: 'AA==' } });
  t.send({ type: 'event', event: { type: 'model_event', audio: 'AA==' } });
  assert.deepEqual(t.events, [{ type: 'transcript', text: 'Тест' }]);
  t.adapter.detach();
});

test('finish turn delegates the boundary without stopping capture or sending speech text', async () => {
  const t = setup();
  const finished = t.adapter.finishTurn();
  assert.equal(t.sent[0].type, 'finish_turn');
  assert.deepEqual(Object.keys(t.sent[0]).sort(), ['request_id', 'type', 'version']);
  t.ack(t.sent[0], { state: state({ status: 'live', capturing: true }) });
  assert.equal(await finished, true);
  assert.equal(t.adapter.state.capturing, true);
  assert.equal(t.sent.some(message => message.type === 'stop'), false);
  t.adapter.detach();
});

test('bridge timeout is explicit and an expired ACK cannot complete the next request', async () => {
  const t = setup({ timeoutMs: 15 });
  const first = t.adapter.getState();
  await assert.rejects(first, { code: 'NATIVE_VOICE_BRIDGE_TIMEOUT' });
  const second = t.adapter.getState();
  t.ack(t.sent[0], { state: state({ revision: 99, status: 'live', capturing: true }) });
  assert.equal(t.adapter.state, null);
  t.ack(t.sent[1]);
  assert.equal((await second).status, 'idle');
  t.adapter.detach();
});
