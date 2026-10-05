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

test("Projects Hub renders provider interim speech without committing it to chat history", async () => {
  const source = await readFile(new URL("../src/App.tsx", import.meta.url), "utf8");
  const backend = await readFile(
    new URL("../../src/projects_hub/live_adapter.py", import.meta.url),
    "utf8",
  );
  assert.match(source, /event\.type === "interim_input_transcript"[\s\S]*setInterimInputTranscript/);
  assert.match(source, /event\.type === "input_transcript"[\s\S]*setInterimInputTranscript\(""/);
  assert.match(source, /Слышу сейчас/);
  assert.match(source, /chat-bubble user interim/);
  assert.doesNotMatch(
    backend,
    /kind not in \{[^}]*interim_input_transcript/,
  );
});

test("Projects Hub keeps the Android-hosted PWA network-fresh", async () => {
  const worker = await readFile(new URL("../public/sw.js", import.meta.url), "utf8");
  const main = await readFile(new URL("../src/main.tsx", import.meta.url), "utf8");
  const android = await readFile(
    new URL("../../android/app/src/main/java/com/kenigevents/projectshub/MainActivity.java", import.meta.url),
    "utf8",
  );
  assert.match(worker, /projects-hub-shell-v2/);
  assert.match(worker, /cache:\s*"no-store"/);
  assert.match(main, /updateViaCache:\s*"none"/);
  assert.match(main, /controllerchange/);
  assert.match(android, /webView\.clearCache\(true\)/);
  assert.match(android, /native_version=/);
});

test("Android opens GitHub without resolveActivity package-visibility gating", async () => {
  const android = await readFile(
    new URL("../../android/app/src/main/java/com/kenigevents/projectshub/MainActivity.java", import.meta.url),
    "utf8",
  );
  const manifest = await readFile(
    new URL("../../android/app/src/main/AndroidManifest.xml", import.meta.url),
    "utf8",
  );
  const browserMethod = android.match(/private void openExternalBrowser\(Uri uri\) \{[\s\S]*?\n    \}/)?.[0] ?? "";
  assert.match(browserMethod, /startActivity\(chrome\)/);
  assert.match(browserMethod, /ActivityNotFoundException/);
  assert.match(browserMethod, /startActivity\(browser\)/);
  assert.doesNotMatch(browserMethod, /resolveActivity/);
  assert.match(manifest, /com\.android\.chrome/);
  assert.match(manifest, /android\.intent\.action\.VIEW/);
  assert.match(manifest, /android:scheme="https"/);
});


test("Projects Hub keeps the conversation on the canvas and mobile context scrollable", async () => {
  const source = await readFile(new URL("../src/App.tsx", import.meta.url), "utf8");
  const styles = await readFile(new URL("../src/styles.css", import.meta.url), "utf8");
  assert.match(source, /className="chat-canvas"/);
  assert.match(source, /className="chat-stack"/);
  assert.match(source, /const showWork = Boolean\([\s\S]*chatMessages\.length === 0/);
  assert.match(styles, /\.chat-canvas\s*\{[\s\S]*position:\s*fixed/);
  assert.match(styles, /\.chat-stack::before\s*\{[\s\S]*margin-top:\s*auto/);
  assert.match(styles, /\.context-sheet\s*\{[\s\S]*max-height:[\s\S]*overflow-y:\s*auto/);
});

test("Android advertises calendar capability but asks permission only on first calendar action", async () => {
  const android = await readFile(
    new URL("../../android/app/src/main/java/com/kenigevents/projectshub/MainActivity.java", import.meta.url),
    "utf8",
  );
  const api = await readFile(
    new URL("../../android/app/src/main/java/com/kenigevents/projectshub/ApiClient.java", import.meta.url),
    "utf8",
  );
  const calendar = await readFile(
    new URL("../../android/app/src/main/java/com/kenigevents/projectshub/CalendarExecutor.java", import.meta.url),
    "utf8",
  );
  assert.doesNotMatch(android, /requestCalendarAfterPairingOnce/);
  assert.match(android, /requestCalendarPermissionFor/);
  assert.match(android, /calendar\.read_events/);
  assert.doesNotMatch(android, /setTitle\("Добавить в календарь\?"\)/);
  assert.match(api, /calendar\.read_events/);
  assert.match(api, /api\/device\/capabilities/);
  assert.match(calendar, /JSONObject readEvents/);
  assert.match(calendar, /CalendarContract\.Instances/);
});

test("Projects Hub suppresses capture while Mira playback is active", async () => {
  const source = await readFile(new URL("../src/App.tsx", import.meta.url), "utf8");
  assert.match(source, /suppressCaptureDuringPlayback:\s*true/);
});

test("backlog stays primary while owner development is observable and triggers updater after success", async () => {
  const source = await readFile(new URL("../src/App.tsx", import.meta.url), "utf8");
  const api = await readFile(new URL("../src/api.ts", import.meta.url), "utf8");
  assert.match(source, /Бэклог/);
  assert.match(source, /backlogTasks\.map/);
  assert.match(source, /getDevelopmentBacklog/);
  assert.match(source, /getDevelopmentCodexStatus/);
  assert.match(source, /getLatestDevelopmentExecution/);
  assert.match(source, /window\.setInterval\(syncDevelopment, 15_000\)/);
  assert.match(source, /projectshub:\/\/update\/check/);
  assert.match(source, /Остаток/);
  assert.match(source, /phase_detail/);
  assert.match(source, /execution-stages/);
  assert.match(api, /remaining_percent/);
  assert.match(api, /\/api\/development\/backlog/);
  assert.match(api, /reasoning_efforts/);
});


test("Android shows an explicit update dialog and sends native version to Live", async () => {
  const source = await readFile(new URL("../src/App.tsx", import.meta.url), "utf8");
  const android = await readFile(
    new URL("../../android/app/src/main/java/com/kenigevents/projectshub/MainActivity.java", import.meta.url),
    "utf8",
  );
  assert.match(source, /ProjectsHubAndroid\\\/\(\[\^\\s\]\+\)/);
  assert.match(source, /client_version:\s*nativeVersion/);
  assert.match(android, /Доступно обновление Projects Hub/);
  assert.match(android, /setPositiveButton\("Обновить"/);
  assert.match(android, /setNegativeButton\("Позже"/);
});


test("Projects Hub sends timezone and auto-recovers one broken Live transport", async () => {
  const source = await readFile(new URL("../src/App.tsx", import.meta.url), "utf8");
  assert.match(source, /Intl\.DateTimeFormat\(\)\.resolvedOptions\(\)\.timeZone/);
  assert.match(source, /client_timezone:\s*clientTimezone/);
  assert.match(source, /kind === "transport_error"[\s\S]*recoverLiveConversation/);
  assert.match(source, /async function recoverLiveConversation\(\)/);
  assert.match(source, /userStoppedVoiceRef/);
});

test("runtime UX distinguishes semantic backend version from build provenance", async () => {
  const source = await readFile(
    new URL("../../src/projects_hub/live_adapter.py", import.meta.url),
    "utf8",
  );
  const version = await readFile(
    new URL("../../src/projects_hub/version.py", import.meta.url),
    "utf8",
  );
  assert.match(source, /backend_version/);
  assert.match(source, /backend_release_sha упоминай только/);
  assert.match(version, /0\.1\.24/);
});

test("Projects Hub disables provider-transcript voice stop control", async () => {
  const source = await readFile(new URL("../src/App.tsx", import.meta.url), "utf8");
  assert.match(source, /voiceControl:\s*null/);
  assert.match(source, /suppressCaptureDuringPlayback:\s*true/);
});

test("Projects Hub lets Live provider own realtime speech boundaries", async () => {
  const source = await readFile(new URL("../src/App.tsx", import.meta.url), "utf8");
  assert.match(source, /continuousCapture:\s*true/);
  assert.doesNotMatch(source, /speechEndSilenceMs:/);
  assert.match(source, /suppressCaptureDuringPlayback:\s*true/);
});
