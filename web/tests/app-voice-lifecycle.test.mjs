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
