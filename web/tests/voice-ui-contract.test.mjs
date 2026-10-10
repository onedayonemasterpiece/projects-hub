import test from "node:test";
import assert from "node:assert/strict";

import {
  mergeTranscript,
  resolveTerminalVoiceState,
  selectProvisionalCaption,
  speechStartsNewUserBubble,
  voiceStartupFailureNotice,
} from "../src/voiceUiContract.js";

test("long transcript merge is lossless beyond 4000 chars", () => {
  const start = "НАЧАЛО " + "а".repeat(2600);
  const middle = " СЕРЕДИНА " + "б".repeat(2600);
  const end = " КОНЕЦ";
  const full = start + middle + end;

  assert.equal(mergeTranscript("", start), start);
  assert.equal(mergeTranscript(start, full), full);
  assert.equal(mergeTranscript(start + middle, middle + end), full);
  assert.ok(full.length > 4000);
  assert.ok(mergeTranscript(start, full).startsWith("НАЧАЛО"));
  assert.ok(mergeTranscript(start, full).includes("СЕРЕДИНА"));
  assert.ok(mergeTranscript(start, full).endsWith("КОНЕЦ"));
});

test("provider-corrected final replaces cumulative prefix instead of duplicating it", () => {
  const partial = "Встреча во вторник";
  const corrected = "Встреча во вторник, точнее в среду в 16:00";
  assert.equal(mergeTranscript(partial, corrected), corrected);
});

test("shorter sidecar final never rolls back a longer visible interim caption", () => {
  const interim = "а".repeat(184);
  const shorterFinal = "б".repeat(133);
  assert.equal(selectProvisionalCaption(interim, shorterFinal, true), interim);
  assert.equal(selectProvisionalCaption(interim, "в".repeat(188), true), "в".repeat(188));
});

test("interim caption remains revisable while canonical Live transcript stays separate", () => {
  assert.equal(selectProvisionalCaption("длинная гипотеза", "короче", false), "короче");
  assert.equal(selectProvisionalCaption("видимый текст", "   ", true), "видимый текст");
});

test("terminal failures remain distinguishable from ordinary off", () => {
  for (const reason of [
    "resource_denial",
    "provider_failure",
    "connection_failure",
    "capture_error",
  ]) {
    assert.equal(resolveTerminalVoiceState("off", { reason }), reason);
  }
  assert.equal(resolveTerminalVoiceState("off", { reason: "user_stop" }), "");
  assert.equal(resolveTerminalVoiceState("listening", { reason: "resource_denial" }), "");
});


test("speech start opens a new user bubble even without provider interim text", () => {
  assert.equal(speechStartsNewUserBubble("speech_start"), true);
  assert.equal(speechStartsNewUserBubble("speech_end"), false);
  assert.equal(speechStartsNewUserBubble("input_transcript"), false);
});
test("a rejected Live setup shows useful recoverable status instead of raw HTTP 503", () => {
  const error = Object.assign(new Error("HTTP 503"), { status: 503 });
  const message = voiceStartupFailureNotice(error);
  assert.match(message, /Голосовая модель Миры временно недоступна/);
  assert.match(message, /Переписка сохранена/);
  assert.match(message, /восстановить/);
  assert.equal(voiceStartupFailureNotice(new Error("HTTP 503")), message);
  assert.equal(voiceStartupFailureNotice(Object.assign(new Error("HTTP 401"), {status:401})), null);
  assert.equal(voiceStartupFailureNotice(new Error("MICROPHONE_UNAVAILABLE")), null);
});
