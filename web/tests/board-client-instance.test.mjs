import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const source = readFileSync(new URL("../src/boardApi.ts", import.meta.url), "utf8");

test("board client instance is per browsing context, not shared origin storage", () => {
  assert.match(source, /const BOARD_CLIENT_INSTANCE_ID = crypto\.randomUUID\(\);/);
  assert.match(source, /clientInstanceId \|\| BOARD_CLIENT_INSTANCE_ID/);
  assert.doesNotMatch(source, /localStorage\.getItem\("projects-hub-board-client"\)/);
  assert.doesNotMatch(source, /localStorage\.setItem\("projects-hub-board-client"/);
});
