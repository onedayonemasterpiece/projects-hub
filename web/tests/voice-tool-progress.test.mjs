import test from "node:test";
import assert from "node:assert/strict";
import { reduceVoiceToolProgress } from "../src/voiceUiContract.js";

const pending = (id) => ({ type: "tool_progress", id, status: "pending", elapsed_ms: 2500 });

test("a short spoken acknowledgement ending does not finish the external operation", () => {
  const state = reduceVoiceToolProgress(null, pending("first"));
  assert.equal(reduceVoiceToolProgress(state, { type: "turn_complete" }), state);
  assert.equal(reduceVoiceToolProgress(state, { type: "output_transcript", text: "Проверяю." }), state);
  assert.equal(reduceVoiceToolProgress(state, { type: "tool_result", id: "first", status: "ok" }), null);
});

test("an old receipt or cancellation cannot hide a different active operation", () => {
  const state = reduceVoiceToolProgress(null, pending("new"));
  assert.equal(reduceVoiceToolProgress(state, { type: "tool_result", id: "old", status: "error" }), state);
  assert.equal(reduceVoiceToolProgress(state, { type: "tool_cancelled", ids: ["old"] }), state);
  assert.equal(reduceVoiceToolProgress(state, { type: "tool_cancelled", ids: ["new"] }), null);
});

test("fresh speech, barge-in and local Stop clear stale presentation without a fabricated result", () => {
  for (const type of ["speech_start", "interrupted", "voice_stopped"]) {
    assert.equal(reduceVoiceToolProgress(reduceVoiceToolProgress(null, pending("call")), { type }), null);
  }
});

test("malformed progress cannot create an indefinite visible wait", () => {
  for (const item of [{ ...pending("" ) }, { ...pending("call"), elapsed_ms: NaN },
    { ...pending("call"), elapsed_ms: -1 }, { ...pending("call"), status: "ok" }]) {
    assert.equal(reduceVoiceToolProgress(null, item), null);
  }
});
