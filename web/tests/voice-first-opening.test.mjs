import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const app = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
const timeline = readFileSync(new URL("../src/CollaborationTimeline.tsx", import.meta.url), "utf8");
const questions = readFileSync(new URL("../src/CollaborationQuestions.tsx", import.meta.url), "utf8");
const api = readFileSync(new URL("../src/api.ts", import.meta.url), "utf8");

test("app launch does not manufacture or resurrect historical collaboration widgets", () => {
  const startup = app.slice(app.indexOf("function defaultWidgetMessage("),
                            app.indexOf("function timelineFingerprint("));
  assert.match(startup, /blocks:\s*\[\]/);
  assert.doesNotMatch(app, /restored\.push\(defaultWidgetMessage\(/);
  assert.doesNotMatch(app, /missing\.push\(\{\s*kind:\s*"collaboration_/);
  assert.match(app, /setCollaborationCard\(null\)/);
  assert.match(app, /setBoardProjectId\(null\)/);
});

test("only an explicit Live tool result mounts collaboration, never an automatic banner or tool success", () => {
  assert.doesNotMatch(app, /development-arrival/);
  assert.match(app, /uiCommand\?\.kind === "collaboration"/);
  assert.match(app, /collaborationCard && boot/);
  assert.match(app, /requested-card-row/);
  assert.match(app, /if \(block\.kind === "collaboration_timeline"/);
  const handled = app.slice(app.indexOf('event.type === "tool_result"'),
                            app.indexOf('event.type === "capability_transition_requested"'));
  assert.doesNotMatch(handled, /set(?:EventOpen|MemoryOpen|BacklogOpen)\(true\)/);
});

test("requested activity is relevant to humans and completed analysis, not old owner notes", () => {
  assert.match(timeline, /event\.kind === "note_chatgpt_analyzed"/);
  assert.match(timeline, /event\.actor_id !== actorId/);
  assert.match(api, /latest: "true"/);
  assert.match(timeline, /mode === "activity"/);
  assert.match(timeline, /mode === "note"/);
});

test("four analysis-question outcomes remain available; text input is optional", () => {
  for (const value of ["answer", "unknown", "skip", "later"]) {
    assert.match(questions, new RegExp('respond\\(question, "' + value + '"\\)'));
  }
  assert.match(questions, /manualAnswerId === question\.id && \(/);
  assert.match(questions, /Ответьте Мире голосом/);
  assert.match(questions, /Ответить текстом/);
  assert.doesNotMatch(questions, /<textarea[^>]*autoFocus/);
});
