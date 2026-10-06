import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

test("server recovery reads the authoritative utterance verdict before replay", async () => {
  const source = await readFile(new URL("../src/serverRecovery.ts", import.meta.url), "utf8");
  assert.match(source, /\/api\/sources\/\$\{encodeURIComponent\(sourceId\)\}/);
  assert.match(source, /verdict === "turn_committed"/);
  assert.match(source, /verdict === "no_turn_closed" \|\| verdict === "turn_closed_no_transcript"/);
  assert.match(source, /params\.set\("start_bytes"/);
  assert.match(source, /params\.set\("end_bytes"/);
  assert.doesNotMatch(source, /\/api\/conversations\/\$\{encodeURIComponent\(conversationId\)\}\/sources\/\$\{encodeURIComponent\(sourceId\)\}\/audio/);
});
