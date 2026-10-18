import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";
import { mergeTranscript, reduceVoiceToolProgress, selectProvisionalCaption,
  speechStartsNewUserBubble } from "../src/voiceUiContract.js";

// Execute the production App callbacks and render its actual JSX. The harness
// supplies React state setters and bridge events; it does not duplicate a reducer.
const source = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
const ast = ts.createSourceFile("App.tsx", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
function findNode(predicate) {
  let found;
  function visit(node) {
    if (!found && predicate(node)) found = node;
    if (!found) ts.forEachChild(node, visit);
  }
  visit(ast);
  assert.ok(found, "production App node exists");
  return found;
}
function evaluate(expression, env) {
  const { outputText } = ts.transpileModule(`const value = ${expression};`, {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ESNext, jsx: ts.JsxEmit.React },
  });
  return new Function(...Object.keys(env), outputText + "\nreturn value;")(...Object.values(env));
}
function callback(name, env) {
  const declaration = findNode(node => ts.isVariableDeclaration(node) && node.name.getText(ast) === name);
  assert.equal(declaration.initializer.expression.getText(ast), "useCallback");
  return evaluate(declaration.initializer.arguments[0].getText(ast), env);
}
function appFunction(name, env) {
  return evaluate(findNode(node => ts.isFunctionDeclaration(node) && node.name?.text === name).getText(ast), env);
}
function renderAppPart(className, env) {
  const element = findNode(node => ts.isJsxElement(node) && node.openingElement.attributes.properties.some(
    prop => ts.isJsxAttribute(prop) && prop.name.text === "className" && prop.initializer?.text === className));
  return renderToStaticMarkup(evaluate(element.getText(ast), { React, ...env }));
}
function setup(nativeSurface = true) {
  const ui = { messages: [], interim: "", speechActive: false, speechPending: false,
    inputSeen: false, progress: null, requests: [] };
  const noop = () => {};
  const set = key => value => { ui[key] = typeof value === "function" ? value(ui[key]) : value; };
  const env = {
    nativeSurface, activeIdentity: { current: true },
    conversationRef: { current: { id: "conversation", actor_id: "actor", workspace_id: "workspace" } },
    nativeVoiceRef: { current: { state: { session_id: "session", conversation_id: "conversation" } } },
    nativeTimelineLoader: { current: { request: (hint, context) => ui.requests.push({ hint, context }) } },
    timelinePersistedRef: { current: new Map() }, userTranscriptIndex: { current: -1 },
    assistantTranscriptIndex: { current: -1 }, currentTurnIdRef: { current: null },
    currentSourceIdRef: { current: "capture-source" }, userTurnAwaitingFinalRef: { current: false },
    userTurnBoundaryPendingRef: { current: false }, turnHasInput: { current: false },
    setChatMessages: set("messages"), setInterimInputTranscript: set("interim"),
    setSpeechActive: set("speechActive"), setSpeechPending: set("speechPending"),
    setInputTranscriptSeen: set("inputSeen"), setToolProgress: set("progress"),
    setPlaybackProblem: noop, setNotice: noop, VOICE_TURN_PLACEHOLDER: "",
    mergeTranscript, reduceVoiceToolProgress, selectProvisionalCaption, speechStartsNewUserBubble,
  };
  let nextId = 0;
  env.opaqueTimelineId = prefix => `${prefix}_local_${++nextId}`;
  for (const name of ["timelineMessageFromServer", "timelineFingerprint"]) env[name] = appFunction(name, env);
  for (const name of ["mergeChatMessage", "settleCurrentVoiceBubble", "reserveUserVoiceBubble",
    "applyCaptionToUserBubble", "applyNativeTimeline", "applyLiveEvent", "applyVoiceTiming"]) {
    env[name] = callback(name, env);
  }
  const render = () => {
    const values = { nativeSurface, chatMessages: ui.messages, interimInputTranscript: ui.interim,
      speechActive: ui.speechActive, speechPending: ui.speechPending, toolProgress: ui.progress,
      collaborationCard: null, boot: null, notice: null, wait: null, playbackProblem: null,
      voiceActive: true, networkOnline: false, voiceHeadline: "Слушаю", voiceHint: "Говорите",
      recoverableSourceId: null, microphoneSettingsAvailable: false };
    return { chat: renderAppPart("chat-stack", values), voice: renderAppPart("voice-copy", values) };
  };
  return { ui, env, render, timing: env.applyVoiceTiming, event: env.applyLiveEvent };
}
function canonical(id, text, revision = 1, role = "user") {
  return { type: role === "user" ? "input_transcript" : "output_transcript", text,
    timeline_message: { id, conversation_id: "conversation", turn_id: `turn_${id}`,
      source_id: `source_${id}`, role, text, revision, transcript_revision: revision, blocks: [] } };
}

test("actual native App keeps late A separate after A ends and B starts", () => {
  const t = setup();
  t.timing("speech_start");
  t.timing("speech_end");
  assert.equal(t.ui.speechPending, true);
  t.event({ type: "turn_complete" });
  t.timing("speech_start");
  assert.deepEqual(t.ui.messages, [], "VAD alone cannot create empty or failed message rows");
  t.event({ type: "caption_interim_transcript", text: "Промежуточная подпись" });
  t.event(canonical("message_A", "Первая фраза"));
  t.event(canonical("message_B", "Вторая фраза"));
  t.event(canonical("message_A", "Первая", 2));
  t.event(canonical("message_A", "Старая первая фраза", 1));
  t.event(canonical("message_B", "Вторая фраза"));
  assert.deepEqual(t.ui.messages.map(({ id, turnId, text }) => ({ id, turnId, text })), [
    { id: "message_A", turnId: "turn_message_A", text: "Первая" },
    { id: "message_B", turnId: "turn_message_B", text: "Вторая фраза" },
  ]);
  assert.ok(t.ui.messages.every(message => !message.awaitingTranscript && !message.deliveryNote));
  assert.equal(t.env.userTranscriptIndex.current, -1, "canonical completion does not move a local turn cursor");
  const { chat } = t.render();
  assert.equal((chat.match(/class="chat-row user"/g) ?? []).length, 2);
  assert.doesNotMatch(chat, /Текст не удалось отобразить|Промежуточная подпись/);
});

test("actual native App renders one temporary status caption and accepts shorter final", () => {
  const t = setup();
  t.timing("speech_start");
  const interim = "Мира, Projects Hub, Codex, DevCoveer, Калининград, KenigEvents, Wonderful Lections";
  t.event({ type: "caption_interim_transcript", text: interim });
  assert.equal(t.ui.interim, interim);
  assert.deepEqual(t.ui.messages, []);
  let rendered = t.render();
  assert.equal((rendered.voice.match(/class="native-voice-caption"/g) ?? []).length, 1);
  assert.doesNotMatch(rendered.chat, /Projects Hub|chat-row user/);
  t.event({ type: "caption_final_transcript", text: "Мира" });
  assert.equal(t.ui.interim, "Мира", "a final result can remove incorrect interim words without a word filter");
  rendered = t.render();
  assert.match(rendered.voice, /Распознаю: Мира/);
  assert.doesNotMatch(rendered.voice, /Projects Hub/);
  t.event(canonical("message_A", "Мира, привет"));
  assert.equal(t.ui.interim, "");
  assert.equal(t.ui.messages[0].text, "Мира, привет");
  assert.doesNotMatch(t.render().voice, /native-voice-caption/);
});

test("native noise and unavailable captions cannot manufacture transcript errors", () => {
  const t = setup();
  for (let count = 0; count < 4; count++) {
    t.timing("speech_start");
    t.event({ type: "caption_unavailable" });
    t.timing("speech_end");
    t.event({ type: "turn_complete" });
  }
  assert.deepEqual(t.ui.messages, []);
  assert.doesNotMatch(t.render().chat, /chat-row user|Текст не удалось отобразить/);
  assert.equal(t.ui.speechPending, true, "waiting for Mira remains visible without creating a user bubble");
});

test("optional caption outage preserves actual voice state and messages on both surfaces", () => {
  for (const nativeSurface of [true, false]) {
    const t = setup(nativeSurface);
    t.ui.messages = [{ id: "already-saved", role: "user", text: "Принятая фраза", revision: 1 }];
    t.ui.speechActive = true;
    t.ui.speechPending = true;
    t.ui.interim = "Временная подпись";
    t.ui.progress = { id: "tool-operation", elapsed_ms: 1000 };
    const before = structuredClone(t.ui);
    t.event({ type: "caption_unavailable", code: "CAPTION_PROVIDER_ERROR" });
    assert.deepEqual(t.ui, before, `caption outage is optional with nativeSurface=${nativeSurface}`);
  }
});

test("a native reference captures identity but cannot bind a late load to a guessed placeholder", () => {
  const t = setup();
  t.timing("speech_start");
  t.timing("speech_end");
  const hint = { id: "message_A", conversation_id: "conversation", revision: 3 };
  t.event({ type: "input_transcript", timeline_ref: hint });
  t.timing("speech_start");
  assert.deepEqual(t.ui.requests, [{ hint, context: { actorId: "actor", workspaceId: "workspace",
    conversationId: "conversation", sessionId: "session", owner: t.env.nativeVoiceRef.current } }]);
  t.env.applyNativeTimeline(canonical("message_A", "Первая длинная фраза", 3));
  t.event(canonical("message_B", "Вторая фраза"));
  assert.deepEqual(t.ui.messages.map(message => message.id), ["message_A", "message_B"]);
  t.event({ type: "input_transcript", timeline_ref: { ...hint, conversation_id: "foreign" } });
  assert.equal(t.ui.requests.length, 1, "foreign conversation hint cannot trigger a read");
});

test("unidentified native transcript cannot become a guessed durable message", () => {
  const t = setup();
  t.timing("speech_start");
  t.event({ type: "input_transcript", text: "Пока без серверного идентификатора" });
  t.event({ type: "output_transcript", text: "Ответ без серверного идентификатора" });
  assert.deepEqual(t.ui.messages, []);
  assert.equal(t.ui.interim, "Пока без серверного идентификатора");
  assert.doesNotMatch(t.render().chat, /chat-row/);
});

test("native captions and canonical rows preserve pending tool progress", () => {
  const t = setup();
  t.timing("speech_start");
  t.timing("speech_end");
  t.event({ type: "tool_progress", id: "tool_A", status: "pending", elapsed_ms: 1200 });
  t.event({ type: "caption_final_transcript", text: "Задача" });
  t.event(canonical("message_A", "Сделай задачу"));
  t.event({ type: "turn_complete" });
  t.timing("first_output_audio");
  assert.deepEqual(t.ui.progress, { id: "tool_A", elapsed_ms: 1200 });
  assert.match(t.render().chat, /Мира ждёт результат/);
  t.event({ type: "tool_cancelled", ids: ["tool_A"] });
  assert.equal(t.ui.progress, null);
});

test("legacy PWA still reserves and completes its own local message identities", () => {
  const t = setup(false);
  t.timing("speech_start");
  const firstId = t.ui.messages[0].id;
  t.event({ type: "caption_interim_transcript", text: "Длинная гипотеза первой фразы" });
  t.event({ type: "caption_final_transcript", text: "Первая" });
  assert.equal(t.ui.messages[0].text, "Первая");
  assert.equal(t.ui.messages[0].provisionalCaption, true);
  assert.doesNotMatch(t.render().voice, /native-voice-caption/);
  t.timing("speech_end");
  t.event({ type: "input_transcript", text: "Первая фраза" });
  t.timing("speech_start");
  t.event({ type: "input_transcript", text: "Вторая фраза" });
  assert.equal(t.ui.messages[0].id, firstId);
  assert.deepEqual(t.ui.messages.map(message => message.text), ["Первая фраза", "Вторая фраза"]);
  assert.ok(t.ui.messages.every(message => !message.awaitingTranscript && !message.deliveryNote));
});
