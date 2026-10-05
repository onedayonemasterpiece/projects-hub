import test from "node:test";
import assert from "node:assert/strict";

import {
  mergeTranscript,
  resolveTerminalVoiceState,
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
