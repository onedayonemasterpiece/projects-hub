/** Product UI projection only. Native SDK/service owns capture, codec and WSS. */
const owners = new WeakMap();
const statuses = new Set(['idle', 'starting', 'live', 'delayed', 'recording_only', 'stopping', 'error', 'unsupported']);
const stopReasons = new Set(['user', 'logout', 'account_changed']);
const idPattern = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$/;

export class NativeVoiceError extends Error {
  constructor(code) {
    super(code);
    this.name = 'NativeVoiceError';
    this.code = code;
  }
}

export function nativeVoiceSupported(capabilities) {
  return capabilities?.protocol === 'wl-live-v2'
    && capabilities.native_capture === true
    && capabilities.compressed_input === true
    && capabilities.compressed_output === true;
}

function validState(state) {
  return state && typeof state === 'object' && !Array.isArray(state)
    && typeof state.owner_id === 'string' && idPattern.test(state.owner_id)
    && Number.isSafeInteger(state.revision) && state.revision >= 0
    && statuses.has(state.status)
    && typeof state.capturing === 'boolean' && typeof state.playing === 'boolean'
    && Number.isSafeInteger(state.queued_audio_ms) && state.queued_audio_ms >= 0
    && ['conversation_id', 'session_id', 'source_id'].every(key => state[key] === null
      || (typeof state[key] === 'string' && idPattern.test(state[key])))
    && (state.code == null || (typeof state.code === 'string' && /^[A-Z0-9_]{1,96}$/.test(state.code)));
}

/**
 * Start resolves when the user request is accepted, not when a provider or a
 * microphone is ready. onState is the authority for actual capture and delay.
 * attach/getState are read-only; detaching a WebView never sends Stop.
 */
export function createNativeVoiceAdapter({
  bridge = globalThis.window?.projectsHubVoice,
  onState = () => {}, onEvent = () => {}, onError = () => {},
  timeoutMs = 8000,
} = {}) {
  if (!bridge || typeof bridge.postMessage !== 'function') throw new NativeVoiceError('NATIVE_VOICE_UNAVAILABLE');
  if (owners.has(bridge)) throw new NativeVoiceError('NATIVE_VOICE_UI_ALREADY_ATTACHED');
  if (!Number.isFinite(timeoutMs) || timeoutMs < 1) throw new TypeError('timeoutMs must be positive');
  const previousListener = bridge.onmessage;
  const pending = new Map();
  let detached = false, state = null, capabilities = null, attaching = null, intentGeneration = 0;
  const retiredOwners = new Set();
  const owner = {};
  owners.set(bridge, owner);

  function report(code) {
    try { onError(new NativeVoiceError(code)); } catch { /* UI callback cannot own native lifecycle. */ }
  }

  function acceptState(value) {
    if (!validState(value)) return false;
    if (retiredOwners.has(value.owner_id)) return false;
    if (state?.owner_id === value.owner_id && value.revision < state.revision) return false;
    if (state && state.owner_id !== value.owner_id) retiredOwners.add(state.owner_id);
    state = Object.freeze({ ...value });
    try { onState(state); } catch { report('NATIVE_VOICE_UI_CALLBACK_FAILED'); }
    return true;
  }

  const dispatch = event => {
    if (detached || typeof event?.data !== 'string' || event.data.length > 262144) return;
    let message;
    try { message = JSON.parse(event.data); } catch { return; }
    if (!message || message.version !== 1 || typeof message.type !== 'string') return;
    if (message.type === 'ack') {
      const request = pending.get(message.request_id);
      if (!request || typeof message.ok !== 'boolean') return;
      pending.delete(message.request_id);
      clearTimeout(request.timer);
      if (message.state !== undefined) acceptState(message.state);
      if (message.capabilities && typeof message.capabilities === 'object')
        capabilities = Object.freeze({ ...message.capabilities });
      if (message.ok) request.resolve(message);
      else request.reject(new NativeVoiceError(
        typeof message.code === 'string' && /^[A-Z0-9_]{1,96}$/.test(message.code)
          ? message.code : 'NATIVE_VOICE_REQUEST_FAILED'));
    } else if (message.type === 'state') {
      acceptState(message.state);
    } else if (message.type === 'event' && message.event && typeof message.event === 'object'
        && !Array.isArray(message.event) && typeof message.event.type === 'string') {
      // Audio is delivered inside the SDK; it must never take a base64 UI detour.
      if (message.event.type === 'audio' || message.event.type === 'audio_chunk'
          || message.event.audio !== undefined || message.event.audio_data !== undefined) return;
      try { onEvent(message.event); } catch { report('NATIVE_VOICE_UI_CALLBACK_FAILED'); }
    }
  };
  bridge.onmessage = dispatch;

  function request(type, fields = {}) {
    if (detached) return Promise.reject(new NativeVoiceError('NATIVE_VOICE_DETACHED'));
    if (pending.size >= 16) return Promise.reject(new NativeVoiceError('NATIVE_VOICE_UI_BACKPRESSURE'));
    const requestId = globalThis.crypto.randomUUID();
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        pending.delete(requestId);
        reject(new NativeVoiceError('NATIVE_VOICE_BRIDGE_TIMEOUT'));
      }, timeoutMs);
      pending.set(requestId, { resolve, reject, timer });
      try { bridge.postMessage(JSON.stringify({ version: 1, type, request_id: requestId, ...fields })); }
      catch {
        clearTimeout(timer);
        pending.delete(requestId);
        reject(new NativeVoiceError('NATIVE_VOICE_BRIDGE_FAILED'));
      }
    });
  }

  async function attach() {
    if (!attaching) attaching = request('attach').then(message => {
      if (!validState(message.state)) throw new NativeVoiceError('NATIVE_VOICE_INVALID_STATE');
      return { state, capabilities, supported: nativeVoiceSupported(capabilities) };
    }).catch(error => { attaching = null; throw error; });
    return attaching;
  }

  async function start({ conversationId }) {
    if (typeof conversationId !== 'string' || !idPattern.test(conversationId))
      throw new NativeVoiceError('NATIVE_VOICE_INVALID_CONVERSATION');
    const generation = ++intentGeneration;
    await attach();
    if (generation !== intentGeneration || detached) return false;
    if (!nativeVoiceSupported(capabilities)) throw new NativeVoiceError('NATIVE_VOICE_UNSUPPORTED');
    await request('start', { conversation_id: conversationId });
    return true;
  }

  async function stop({ reason = 'user' } = {}) {
    if (!stopReasons.has(reason)) throw new NativeVoiceError('NATIVE_VOICE_INVALID_STOP_REASON');
    ++intentGeneration;
    // Stop never waits for an attach, network recovery or pending Start ACK.
    await request('stop', { reason });
  }

  async function getState() {
    const message = await request('state');
    if (!validState(message.state)) throw new NativeVoiceError('NATIVE_VOICE_INVALID_STATE');
    return state;
  }

  async function finishTurn() {
    await request('finish_turn');
    return true;
  }

  function detach() {
    if (detached) return;
    detached = true;
    ++intentGeneration;
    try { bridge.postMessage(JSON.stringify({ version: 1, type: 'detach', request_id: globalThis.crypto.randomUUID() })); }
    catch { /* The native owner remains alive even when a WebView disappears. */ }
    for (const { timer, reject } of pending.values()) {
      clearTimeout(timer);
      reject(new NativeVoiceError('NATIVE_VOICE_DETACHED'));
    }
    pending.clear();
    if (bridge.onmessage === dispatch) bridge.onmessage = previousListener;
    if (owners.get(bridge) === owner) owners.delete(bridge);
  }

  return { attach, start, stop, finishTurn, getState, detach,
    get state() { return state; },
    get sessionId() { return state?.session_id ?? null; },
    get conversationId() { return state?.conversation_id ?? null; },
    get capabilities() { return capabilities; },
  };
}
