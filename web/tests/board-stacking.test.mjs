import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const boardCss = readFileSync(new URL("../src/board.css", import.meta.url), "utf8");
const appCss = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");

function zIndex(source, selector) {
  const start = source.indexOf(selector);
  assert.notEqual(start, -1, "missing selector " + selector);
  const open = source.indexOf("{", start);
  const close = source.indexOf("}", open);
  assert.ok(open > start && close > open, "invalid block for " + selector);
  const block = source.slice(open, close);
  const match = block.match(/z-index:\s*(\d+)\s*;/);
  assert.ok(match, "missing z-index for " + selector);
  return Number(match[1]);
}

test("fullscreen board receives pointer events above global project context", () => {
  const board = zIndex(boardCss, ".board-shell");
  const context = zIndex(appCss, ".context-wrap");
  assert.ok(
    board > context,
    "board-shell z-index must stay above context-wrap: " + board + " <= " + context,
  );
});
