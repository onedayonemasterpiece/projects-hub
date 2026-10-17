import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const source = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
const block = source.match(/    const timelineRef = event\.timeline_ref;([\s\S]*?)\n    if \(event\.type === "caption_interim_transcript"/);
assert.ok(block, "exercise the actual reference dispatch before ordinary text guards");
const body = "const timelineRef = event.timeline_ref;" + block[1].replace(" as NativeTimelineHint", "");

test("a native canonical reference without raw text loads its exact captured placeholder", () => {
  const requests = [];
  const conversationRef = { current: { id: "conversation", actor_id: "actor", workspace_id: "workspace" } };
  const nativeVoiceRef = { current: { state: { session_id: "session" } } };
  const nativeUserPlaceholderId = { current: "first-placeholder" };
  const userTurnAwaitingFinalRef = { current: true };
  const turnHasInput = { current: false };
  const names = ["event", "nativeSurface", "nativeVoiceRef", "conversationRef", "nativeUserPlaceholderId",
    "nativeTimelineLoader", "setInputTranscriptSeen", "setInterimInputTranscript", "userTurnAwaitingFinalRef", "turnHasInput"];
  const dispatch = new Function(...names, body);
  const event = { type: "input_transcript", timeline_ref: { id: "message", conversation_id: "conversation", revision: 3 } };
  const invoke = item => dispatch(item, true, nativeVoiceRef, conversationRef, nativeUserPlaceholderId,
    { current: { request: (hint, context) => requests.push({ hint, context }) } }, () => {}, () => {}, userTurnAwaitingFinalRef, turnHasInput);
  invoke(event);
  nativeUserPlaceholderId.current = "second-placeholder";
  assert.equal(requests.length, 1);
  assert.equal(requests[0].context.placeholderId, "first-placeholder");
  assert.equal(requests[0].context.owner, nativeVoiceRef.current);
  assert.equal(requests[0].context.sessionId, "session");
  assert.equal(requests[0].context.actorId, "actor");
  assert.equal(userTurnAwaitingFinalRef.current, false);
  assert.equal(turnHasInput.current, true);
  invoke({ ...event, timeline_ref: { ...event.timeline_ref, conversation_id: "foreign" } });
  assert.equal(requests.length, 1, "foreign conversation hint cannot trigger a read");
});
