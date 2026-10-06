import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const shell = readFileSync(new URL("../src/BoardShell.tsx", import.meta.url), "utf8");
const boardApi = readFileSync(new URL("../src/boardApi.ts", import.meta.url), "utf8");
const app = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8");

test("normal board UI is voice-first and read-only", () => {
  for (const forbidden of [
    /\+ Стикер/,
    /createSticky/,
    /board-editor/,
    /onDoubleClick/,
    />\s*Удалить\s*</,
    /send\("create"|send\("update"|send\("move"|send\("delete"/,
    /canEdit/,
    /Поиск по доске/,
    /Запустить анализ/,
    />\s*Анализировать\s*</,
    />\s*Добавить на доску\s*</,
    />\s*Новый анализ\s*</,
    /startSelectedAnalysis/,
    /publishCurrentAnalysis/,
    /cancelCurrentAnalysis/,
  ]) {
    assert.doesNotMatch(shell, forbidden);
  }
  assert.doesNotMatch(boardApi, /sendCommand\(/);
  assert.doesNotMatch(app, /canEdit=/);

  assert.match(shell, /Показать всё/);
  assert.match(shell, /Поделиться/);
  assert.match(shell, /Открыть отчёт/);
  assert.match(shell, /board-history/);
  assert.match(shell, /onWheel=\{onWheel\}/);
  assert.match(shell, /GitHub-копия отчёта/);
});

test("browser board socket is receive-oriented and shares the Live tab identity", () => {
  assert.match(boardApi, /class BoardSocket/);
  assert.match(boardApi, /sendPresence/);
  assert.doesNotMatch(boardApi, /private pending/);
  assert.doesNotMatch(boardApi, /sendCommand/);

  assert.match(app, /clientInstanceIdRef/);
  assert.match(app, /client_instance_id: clientInstanceIdRef\.current/);
  assert.match(app, /clientInstanceId=\{clientInstanceIdRef\.current\}/);
  assert.match(shell, /new BoardSocket\([\s\S]{0,180}clientInstanceId/);
});

test("board publishes bounded structural viewport context without client semantic text", () => {
  assert.match(boardApi, /updateBoardViewContext/);
  assert.match(boardApi, /visible_object_ids/);
  assert.match(shell, /updateBoardViewContext\(conversationId/);
  assert.match(shell, /visible_object_ids: visibleObjectIds/);
  assert.match(shell, /selected_object_ids: selectedId \? \[selectedId\] : \[\]/);
  assert.match(shell, /focused_object_id: focusedId/);
  const contextStart = shell.indexOf("updateBoardViewContext(conversationId");
  const contextEnd = shell.indexOf("}).catch", contextStart);
  assert.ok(contextStart >= 0 && contextEnd > contextStart);
  const contextPayload = shell.slice(contextStart, contextEnd);
  assert.doesNotMatch(contextPayload, /text\s*:/);
});
