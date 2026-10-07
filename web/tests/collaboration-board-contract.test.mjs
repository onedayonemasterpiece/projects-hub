import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const app = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");
const board = readFileSync(new URL("../src/InlineBoard.tsx", import.meta.url), "utf8");
const api = readFileSync(new URL("../src/boardApi.ts", import.meta.url), "utf8");
const productApi = readFileSync(new URL("../src/api.ts", import.meta.url), "utf8");

test("personal timeline mounts at most one board renderer", () => {
  assert.equal((app.match(/<InlineBoard/g) ?? []).length, 1);
  assert.match(app, /boardProjectId/);
  assert.match(app, /uiCommand\.kind === "board"/);
  assert.match(app, /setBoardProjectId\(null\)/);
  assert.match(board, /new Application\(\)/);
  assert.doesNotMatch(board, /createPortal|second.*Application|cloneNode/);
});

test("normal board surface is read-only apart from camera controls", () => {
  for (const forbidden of [
    /sendCommand/,
    /createSticky/,
    /onDoubleClick/,
    />\s*Удалить\s*</,
    /board-editor/,
    /contentEditable/,
    /draggable=/,
  ]) {
    assert.doesNotMatch(board, forbidden);
  }
  assert.match(board, /onPointerMove=\{pointerMove\}/);
  assert.match(board, /onWheel=\{wheel\}/);
  assert.match(board, /Показать всё/);
  assert.match(board, /Развернуть/);
  assert.match(board, /Объекты изменяет Мира/);
  assert.doesNotMatch(api, /sendCommand/);
});

test("board shares the current Live browser-tab identity and structural context only", () => {
  assert.match(app, /sessionStorage\.getItem\(key\)/);
  assert.match(app, /client_instance_id: clientInstanceId/);
  assert.match(app, /clientInstanceId=\{clientInstanceId\}/);
  assert.match(board, /new BoardSocket\([\s\S]*clientInstanceId/);
  assert.match(board, /updateBoardViewContext\(conversationId/);
  const start = board.indexOf("updateBoardViewContext(conversationId");
  const end = board.indexOf("}).catch", start);
  assert.ok(start >= 0 && end > start);
  const payload = board.slice(start, end);
  assert.match(payload, /visible_object_ids/);
  assert.match(payload, /focused_object_id/);
  assert.doesNotMatch(payload, /text\s*:/);
});

test("board socket is receive-oriented and direct object mutation stays server-side", () => {
  assert.match(api, /class BoardSocket/);
  assert.doesNotMatch(api, /sendCommand|operation:\s*"create"|operation:\s*"update"/);
});


test("personal conversation is server-backed and widgets are durable message blocks", () => {
  assert.match(app, /getPersonalTimeline\(/);
  assert.match(app, /upsertPersonalTimelineMessage\(/);
  assert.match(app, /key=\{message\.id\}/);
  assert.match(app, /message\.blocks\.map\(/);
  assert.match(app, /transcriptRevision/);
  assert.match(app, /sourceId/);
  assert.doesNotMatch(app, /projects-hub-personal-timeline:/);
  assert.equal((app.match(/<CollaborationTimeline/g) ?? []).length, 1);
  assert.equal((app.match(/<CollaborationQuestions/g) ?? []).length, 1);
  assert.match(productApi, /getPersonalTimeline/);
  assert.match(productApi, /upsertPersonalTimelineMessage/);
});
