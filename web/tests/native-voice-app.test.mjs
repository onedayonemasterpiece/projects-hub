import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createNativeVoiceAdapter } from "../src/nativeVoiceBridge.js";
import { nativeVoiceFailure } from "../src/voiceUiContract.js";
import { createNativeCaptionState } from "../src/nativeCaptionTimeline.js";

const source = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
const initialization = source.match(/      const native = createNativeVoiceAdapter\(\{([\s\S]*?)\n      \}\);\n      nativeVoiceRef\.current = native;/)?.[0];
const toggleBody = source.match(/  async function toggleVoice\(\) \{([\s\S]*?)\n  \}\n\n  function openAndroidMicrophoneSettings/)?.[1];
const resetBody = source.match(/  const clearPrivateIdentity = useCallback\(\(reason: string, message: string \| null = null\) => \{([\s\S]*?)\n  \}, \[resetIdentity, setAndroidVoiceAudioFocus\]\);/)?.[1];
const inactiveLiteral = source.match(/const inactiveVoiceStates = new Set\(\[([\s\S]*?)\]\);/)?.[1];
assert.ok(initialization && toggleBody && resetBody && inactiveLiteral,
  "exercise the actual App native callbacks, Start/Stop state contract and identity reset");

const capabilities = { protocol: "wl-live-v2", native_capture: true, compressed_input: true, compressed_output: true };
const baseState = { owner_id: "native_owner", revision: 0, status: "idle", capturing: false,
  playing: false, conversation_id: "conversation_A", session_id: null, source_id: null,
  queued_audio_ms: 0, code: null };

function compile(body, env) {
  return new Function(...Object.keys(env), body)(...Object.values(env));
}

async function setup({ voiceState = "off" } = {}) {
  const sent = [], ui = { states: [], notices: [], resets: [], browserStarts: 0, busy: false, microphoneSettings: false };
  const inactiveVoiceStates = compile(`return new Set([${inactiveLiteral}]);`, {});
  let revision = 0;
  const noop = () => {};
  const activeIdentity = { current: true }, nativeVoiceRef = { current: null };
  const conversationRef = { current: { id: "conversation_A", actor_id: "actor_A", workspace_id: "workspace_A" } };
  const env = {
    nativeVoiceFailure, activeIdentity, nativeVoiceRef, conversationRef,
    createNativeCaptionState, nativeCaptionRef: { current: createNativeCaptionState() },
    setNativeCaptionUnavailable: noop,
    nativeStateRef: { current: null }, currentSourceIdRef: { current: null },
    boot: { actor: { id: "actor_A" }, workspace: { id: "workspace_A" } },
    nativeSurface: true, isAndroidApp: true, userStoppedVoiceRef: { current: false },
    voiceState, inactiveVoiceStates,
    setNativeSessionId: value => { ui.sessionId = value; },
    setVoiceState: value => { ui.states.push(value); ui.state = value; },
    setNotice: value => { ui.notices.push(value); ui.notice = value; },
    setMicrophoneSettingsAvailable: value => { ui.microphoneSettings = value; },
    setBusy: value => { ui.busy = value; },
    setSpeechActive: noop, setSpeechPending: noop, setInterimInputTranscript: noop, setWait: noop, setToolProgress: noop,
    getConversation: () => { throw new Error("unrelated conversation read"); }, setConversation: noop,
    localStorage: { setItem: noop }, applyVoiceTiming: noop, applyPreferenceEvent: noop, applyLiveEvent: noop,
    ensureConversation: async () => conversationRef.current,
    clientRef: { current: { start() { ui.browserStarts++; throw new Error("native Start reached browser capture"); }, stop: noop } },
    replayingRef: { current: false }, offlineCaptureRef: { current: null },
    identityAbort: { current: new AbortController() }, setAndroidVoiceAudioFocus: noop,
    themeRef: { current: { reset: async () => {} } },
    setCompletedDevelopment: noop, setCompletedDevelopmentActor: noop, setBoot: noop,
    setCollaborationCard: noop, resetIdentity: message => ui.resets.push(message),
  };
  env.clearPrivateIdentity = compile(`return (reason, message = null) => {${resetBody}};`, env);
  const sendState = fields => {
    env.nativeBridge.onmessage({ data: JSON.stringify({ version: 1, type: "state",
      state: { ...baseState, revision: ++revision, ...fields } }) });
  };
  let rejectStart = null;
  env.nativeBridge = {
    postMessage(text) {
      const request = JSON.parse(text); sent.push(request);
      if (request.type === "detach") return;
      queueMicrotask(() => {
        const rejected = request.type === "start" ? rejectStart : null;
        const state = { ...baseState, revision: ++revision,
          ...(request.type === "start" ? { status: "starting" } : {}), ...(rejected?.state ?? {}) };
        env.nativeBridge.onmessage?.({ data: JSON.stringify({ version: 1, type: "ack",
          request_id: request.request_id, ok: !rejected, state, capabilities,
          ...(rejected ? { code: rejected.code } : {}) }) });
      });
    },
  };
  env.createNativeVoiceAdapter = createNativeVoiceAdapter;
  const adapter = compile(`let attached = true; ${initialization.replaceAll(" as LiveEvent", "")} return native;`, env);
  await adapter.attach();
  const toggle = compile(`return async function toggleVoice() {${toggleBody}};`, env);
  return { adapter, toggle, ui, env, sent, sendState,
    rejectStart: value => { rejectStart = value; }, close: () => adapter.detach() };
}

test("actual paired App Start reaches the native bridge without requesting a browser microphone", async () => {
  let browserMicRequests = 0;
  const originalNavigator = Object.getOwnPropertyDescriptor(globalThis, "navigator");
  Object.defineProperty(globalThis, "navigator", { configurable: true, value: { onLine: true,
    mediaDevices: { getUserMedia: async () => { browserMicRequests++; throw new DOMException("Permission denied", "NotAllowedError"); } } } });
  const t = await setup();
  try {
    await t.toggle();
    assert.deepEqual(t.sent.map(message => message.type), ["attach", "start"]);
    assert.equal(t.sent[1].conversation_id, "conversation_A");
    assert.equal(t.ui.state, "starting");
    assert.equal(t.ui.busy, false);
    assert.equal(t.ui.browserStarts, 0);
    assert.equal(browserMicRequests, 0, "the APK deliberately denies WebView getUserMedia");
  } finally {
    t.close();
    if (originalNavigator) Object.defineProperty(globalThis, "navigator", originalNavigator);
    else delete globalThis.navigator;
  }
});

test("native microphone denial is visible with the app settings action", async () => {
  const t = await setup();
  try {
    t.sendState({ status: "error", code: "NATIVE_VOICE_MICROPHONE_DENIED" });
    assert.equal(t.ui.state, "microphone_unavailable");
    assert.equal(t.ui.microphoneSettings, true);
    assert.match(t.ui.notice, /Разрешите микрофон в настройках приложения/);
    assert.equal(t.ui.resets.length, 0);
  } finally { t.close(); }
});

test("actual native state dispatch distinguishes protocol, storage and connection failures", async () => {
  const t = await setup();
  try {
    for (const [code, state, notice] of [
      ["NATIVE_VOICE_PROTOCOL_UNAVAILABLE", "native_unsupported", /несовместимые версии/],
      ["VOICE_STORE_QUOTA", "capture_error", /Место для голосовых записей заполнено/],
      ["VOICE_STORE_CORRUPT", "capture_error", /Не удалось сохранить голосовую запись/],
      ["LIVE_NATIVE_STORAGE_BACKPRESSURE", "capture_error", /Устройство не успевает обрабатывать звук/],
      ["LIVE_NATIVE_OUTPUT_STORAGE_BACKPRESSURE", "capture_error", /Устройство не успевает обрабатывать звук/],
      ["LIVE_NATIVE_SESSION_UNAVAILABLE", "connection_error", /соединиться с сервером/],
      ["NATIVE_VOICE_UNAVAILABLE", "connection_error", /Откройте приложение снова/],
    ]) {
      t.sendState({ status: "error", code });
      assert.equal(t.ui.state, state, code);
      assert.match(t.ui.notice, notice, code);
      assert.equal(t.ui.notice.includes(code), false, "do not expose an untranslated internal code");
      assert.equal(t.ui.microphoneSettings, false);
      if (code === "LIVE_NATIVE_STORAGE_BACKPRESSURE" || code === "LIVE_NATIVE_OUTPUT_STORAGE_BACKPRESSURE") {
        assert.match(t.ui.notice, /сохранённая часть записи остаётся в приложении/);
        assert.doesNotMatch(t.ui.notice, /свободное место|Место для голосовых записей заполнено/);
      }
    }
    assert.equal(t.ui.browserStarts, 0);
  } finally { t.close(); }
});

test("UNAUTHENTICATED from native bootstrap stops the owner, discards identity and explains reauthentication", async () => {
  const t = await setup();
  try {
    t.sendState({ status: "error", code: "UNAUTHENTICATED" });
    assert.equal(t.env.activeIdentity.current, false);
    assert.equal(t.env.identityAbort.current.signal.aborted, true);
    assert.equal(t.env.conversationRef.current, null);
    assert.deepEqual(t.sent.at(-1).type, "stop");
    assert.equal(t.sent.at(-1).reason, "logout");
    assert.equal(t.ui.resets.length, 1);
    assert.match(t.ui.resets[0], /Войдите снова/);
    const statesBeforeLateEvent = t.ui.states.length;
    t.sendState({ status: "live", capturing: true, session_id: "session_old" });
    assert.equal(t.ui.states.length, statesBeforeLateEvent, "old authenticated owner cannot repaint the login");
    await new Promise(resolve => setImmediate(resolve));
  } finally { t.close(); }
});

test("a rejected native Start uses the same reauthentication boundary", async () => {
  const t = await setup();
  try {
    t.rejectStart({ code: "UNAUTHENTICATED", state: { status: "idle" } });
    await t.toggle();
    assert.equal(t.ui.resets.length, 1);
    assert.match(t.ui.resets[0], /Войдите снова/);
    assert.equal(t.env.activeIdentity.current, false);
    assert.equal(t.sent.at(-1).type, "stop");
    await new Promise(resolve => setImmediate(resolve));
  } finally { t.close(); }
});

test("a Start acknowledgement failure cannot hide active native recording or disable Stop", async () => {
  const t = await setup();
  try {
    t.rejectStart({ code: "NATIVE_VOICE_REQUEST_ID_CONFLICT",
      state: { status: "recording_only", capturing: true, source_id: "source_A" } });
    await t.toggle();
    assert.equal(t.ui.state, "recording_only");
    assert.equal(t.env.nativeStateRef.current.capturing, true);
    assert.match(t.ui.notice, /голосовую связь/);
    await t.toggle();
    assert.equal(t.sent.at(-1).type, "stop");
    assert.equal(t.sent.at(-1).reason, "user");
    assert.equal(t.ui.browserStarts, 0);
    await new Promise(resolve => setImmediate(resolve));
  } finally { t.close(); }
});

test("the reauthentication notice survives the actual App identity generation replacement", () => {
  const body = source.match(/  const resetIdentity = useCallback\(\(message: string \| null = null\) => \{([\s\S]*?)\n  \}, \[\]\);/)?.[1];
  assert.ok(body);
  const values = { generation: 4, notice: null };
  const reset = compile(`return (message = null) => {${body}};`, {
    setIdentityNotice: value => { values.notice = value; },
    setIdentityGeneration: update => { values.generation = update(values.generation); },
  });
  reset("Войдите снова.");
  assert.deepEqual(values, { generation: 5, notice: "Войдите снова." });
  assert.match(source, /initialNotice=\{identityNotice\}/);
  assert.match(source, /const \[notice, setNotice\] = useState<string \| null>\(initialNotice\)/);
  reset();
  assert.deepEqual(values, { generation: 6, notice: null }, "a later unrelated account reset does not retain an old notice");
});
