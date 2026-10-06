import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const shell = readFileSync(new URL("../src/BoardShell.tsx", import.meta.url), "utf8");
const boardApi = readFileSync(new URL("../src/boardApi.ts", import.meta.url), "utf8");
const app = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");

test("normal board UI is voice-first and read-only", () => {
  assert.doesNotMatch(shell, /\+ Стикер/);
  assert.doesNotMatch(shell, /createSticky/);
  assert.doesNotMatch(shell, /board-editor/);
  assert.doesNotMatch(shell, /onDoubleClick/);
  assert.doesNotMatch(shell, />\s*Удалить\s*</);
  assert.doesNotMatch(shell, /send\("create"|send\("update"|send\("move"|send\("delete"/);
  assert.doesNotMatch(shell, /canEdit/);
  assert.doesNotMatch(boardApi, /sendCommand\(/);
  assert.doesNotMatch(app, /canEdit=/);

  assert.match(shell, /Показать всё/);
  assert.match(shell, /Поиск по доске/);
  assert.match(shell, /Поделиться/);
  assert.match(shell, /Анализировать/);
  assert.match(shell, /board-history/);
  assert.match(shell, /onWheel=\{onWheel\}/);
});

test("browser board socket is receive-oriented", () => {
  assert.match(boardApi, /class BoardSocket/);
  assert.match(boardApi, /sendPresence/);
  assert.doesNotMatch(boardApi, /private pending/);
  assert.doesNotMatch(boardApi, /sendCommand/);
});
