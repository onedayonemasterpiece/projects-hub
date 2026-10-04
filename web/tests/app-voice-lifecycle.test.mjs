import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

test("Projects Hub preflights microphone and cleans failed Live startup", async () => {
  const source = await readFile(new URL("../src/App.tsx", import.meta.url), "utf8");
  assert.match(source, /captureDuringStart:\s*true/);
  assert.match(source, /kind === "microphone_error"[\s\S]*clientRef\.current\?\.stop\(\{ reason: "microphone_unavailable" \}\)/);
  assert.match(source, /catch \(error\) \{[\s\S]*client\.stop\(\{ reason: "start_error" \}\)/);
  assert.match(source, /projectshub:\/\/settings\/microphone/);
  assert.match(source, /Открыть настройки микрофона/);
});

test("Android package grants Chromium full microphone audio capability", async () => {
  const manifest = await readFile(
    new URL("../../android/app/src/main/AndroidManifest.xml", import.meta.url),
    "utf8",
  );
  assert.match(manifest, /android\.permission\.RECORD_AUDIO/);
  assert.match(manifest, /android\.permission\.MODIFY_AUDIO_SETTINGS/);
});

test("Projects Hub renders both sides of the Live conversation as a bounded messenger thread", async () => {
  const source = await readFile(new URL("../src/App.tsx", import.meta.url), "utf8");
  assert.match(source, /event\.type === "input_transcript"[\s\S]*mergeChatMessage\("user"/);
  assert.match(source, /event\.type === "output_transcript"[\s\S]*mergeChatMessage\("assistant"/);
  assert.match(source, /function mergeTranscript\(/);
  assert.match(source, /messages\.length > 48/);
  assert.match(source, /className=\{"chat-row " \+ message\.role\}/);
  assert.match(source, /chatFollowRef/);
});
