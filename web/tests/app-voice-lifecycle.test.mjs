import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { createLiveAudioSender } from "@onedayonemasterpiece/live-interaction/browser";

test("Projects Hub preflights microphone and cleans failed Live startup", async () => {
  const source = await readFile(new URL("../src/App.tsx", import.meta.url), "utf8");
  assert.match(source, /captureDuringStart:\s*true/);
  assert.match(source, /kind === "microphone_error"[\s\S]*clientRef\.current\?\.stop\(\{ reason: "microphone_unavailable" \}\)/);
  assert.match(source, /catch \(error\) \{[\s\S]*client\.stop\(\{ reason: "start_error" \}\)/);
  assert.match(source, /projectshub:\/\/settings\/microphone/);
  assert.match(source, /Открыть настройки микрофона/);
});

test("Projects Hub enables the voice orb only after the Live client exists", async () => {
  const source = await readFile(new URL("../src/App.tsx", import.meta.url), "utf8");
  assert.match(source, /const \[voiceClientReady, setVoiceClientReady\] = useState\(false\)/);
  assert.match(
    source,
    /clientRef\.current = client;\s*setVoiceClientReady\(true\)/,
  );
  assert.match(
    source,
    /client\.stop\(\{ reason: "ui_unmount" \}\);[\s\S]*setVoiceClientReady\(false\)/,
  );
  assert.match(source, /disabled=\{busy \|\| !voiceClientReady\}/);
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
  assert.match(source, /import \{ mergeTranscript, resolveTerminalVoiceState, selectProvisionalCaption, speechStartsNewUserBubble, voiceStartupFailureNotice \} from "\.\/voiceUiContract\.js"/);
  assert.match(source, /mergeTranscript\(current\.text, clean\)/);
  assert.match(source, /getPersonalTimeline\(/);
  assert.match(source, /upsertPersonalTimelineMessage\(/);
  assert.match(source, /key=\{message\.id\}/);
  assert.match(source, /chatFollowRef/);
});

test("Projects Hub renders provider interim speech without committing it to chat history", async () => {
  const source = await readFile(new URL("../src/App.tsx", import.meta.url), "utf8");
  const backend = await readFile(
    new URL("../../src/projects_hub/live_adapter.py", import.meta.url),
    "utf8",
  );
  assert.match(source, /event\.type === "interim_input_transcript"[\s\S]*applyCaptionToUserBubble\(event\.text, false\)/);
  assert.match(source, /event\.type === "interim_input_transcript"[\s\S]*setInterimInputTranscript/);
  assert.match(source, /event\.type === "input_transcript"[\s\S]*setInterimInputTranscript\(""/);
  assert.doesNotMatch(source, /Слышу сейчас/);
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
  assert.match(source, /suppressCaptureDuringPlayback:\s*"adaptive"/);
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
  const pyproject = await readFile(
    new URL("../../pyproject.toml", import.meta.url),
    "utf8",
  );
  const match = version.match(/__version__\s*=\s*"([^"]+)"/);
  assert.ok(match);
  assert.match(match[1], /^\d+\.\d+\.\d+$/);
  assert.match(pyproject, new RegExp('version = "' + match[1].replace(/\./g, "\\.") + '"'));
  assert.match(source, /backend_version/);
  assert.match(source, /backend_release_sha упоминай только/);
});

test("Projects Hub disables provider-transcript voice stop control", async () => {
  const source = await readFile(new URL("../src/App.tsx", import.meta.url), "utf8");
  assert.match(source, /voiceControl:\s*null/);
  assert.match(source, /suppressCaptureDuringPlayback:\s*"adaptive"/);
});

test("Projects Hub uses explicit client VAD with a bounded low-latency endpoint", async () => {
  const source = await readFile(new URL("../src/App.tsx", import.meta.url), "utf8");
  assert.match(source, /manualActivityDetection:\s*true/);
  assert.match(source, /continuousCapture:\s*false/);
  assert.match(source, /speechEndSilenceMs:\s*1200/);
  assert.match(source, /speechStartMs:\s*180/);
  assert.match(source, /longSpeechEndSilenceMs:\s*2500/);
  assert.match(source, /longSpeechAfterMs:\s*2500/);
  assert.match(source, /suppressCaptureDuringPlayback:\s*"adaptive"/);
});


test("adaptive endpoint keeps short commands responsive and gives sustained speech a conservative pause", async () => {
  const sent = [];
  let at = 0;
  const sender = createLiveAudioSender({
    send: async message => sent.push(message),
    now: () => at,
    batchMs: 0,
    speechEndMs: 1200,
    speechStartMs: 180,
    longSpeechEndMs: 2500,
    longSpeechAfterMs: 2500,
    manualActivityDetection: true,
  });
  const tick = () => new Promise(resolve => setImmediate(resolve));
  const push = async (rms, ms = 100) => {
    at += ms;
    sender.push(new Int16Array(ms * 16).fill(rms > 0.01 ? 1000 : 0), rms);
    await tick();
    await tick();
  };

  for (let i = 0; i < 10; i += 1) await push(0.05);
  for (let i = 0; i < 11; i += 1) await push(0);
  assert.equal(sent.filter(message => message.activity_end).length, 0);
  await push(0);
  assert.equal(sent.filter(message => message.activity_end).length, 1);

  for (let i = 0; i < 30; i += 1) await push(0.05);
  for (let i = 0; i < 24; i += 1) await push(0);
  assert.equal(sent.filter(message => message.activity_end).length, 1);
  await push(0);
  assert.equal(sent.filter(message => message.activity_end).length, 2);
  sender.stop();
});

test("Projects Hub keeps long provider transcript intact and exposes terminal voice failures", async () => {
  const source = await readFile(new URL("../src/App.tsx", import.meta.url), "utf8");
  assert.doesNotMatch(source, /clean\.slice\(0, 4000\)/);
  assert.doesNotMatch(source, /mergeTranscript\([^\n]+\)\.slice\(0, 4000\)/);
  assert.doesNotMatch(source, /setInterimInputTranscript\([^\n]+slice\(-1200\)/);
  assert.match(source, /onState:\s*\(state, detail\)/);
  assert.match(source, /resource_denial/);
  assert.match(source, /provider_failure/);
  assert.doesNotMatch(source, /Микрофон работает; текст ещё не получен/);
  assert.match(source, /Жду ответ Миры…/);
  assert.match(source, /suppressCaptureDuringPlayback:\s*"adaptive"/);
});


test("a new microphone speech start cannot append to the prior final-only user bubble", async () => {
  const source = await readFile(new URL("../src/App.tsx", import.meta.url), "utf8");
  assert.match(source, /onTiming:\s*event\s*=>\s*\{[\s\S]*speechStartsNewUserBubble\(event\)/);
  assert.match(source, /speechStartsNewUserBubble\(event\)[\s\S]*userTranscriptIndex\.current\s*=\s*-1/);
  assert.match(source, /speechStartsNewUserBubble\(event\)[\s\S]*turnHasInput\.current\s*=\s*false/);
});

test("Projects Hub opts into shared adaptive duplex echo rejection", async () => {
  const source = await readFile(new URL("../src/App.tsx", import.meta.url), "utf8");
  assert.match(source, /suppressCaptureDuringPlayback:\s*"adaptive"/);
  assert.doesNotMatch(source, /suppressCaptureDuringPlayback:\s*false/);
  assert.match(source, /speechEndSilenceMs:\s*1200/);
  assert.match(source, /speechStartMs:\s*180/);
  assert.match(source, /longSpeechEndSilenceMs:\s*2500/);
  assert.match(source, /longSpeechAfterMs:\s*2500/);
});


test("accepted microphone turns stay visible without premature transcription errors", async () => {
  const source = await readFile(new URL("../src/App.tsx", import.meta.url), "utf8");
  assert.match(source, /VOICE_TURN_PLACEHOLDER = ""/);
  assert.doesNotMatch(source, /"Голосовая реплика"/);
  assert.match(source, /const reserveUserVoiceBubble = useCallback/);
  assert.match(
    source,
    /messages\.push\(\{[\s\S]*id: opaqueTimelineId\("msg"\)[\s\S]*turnId,[\s\S]*role: "user"[\s\S]*text: VOICE_TURN_PLACEHOLDER[\s\S]*sourceId: currentSourceIdRef\.current[\s\S]*awaitingTranscript: true/,
  );
  assert.match(source, /\{message\.text && \(/);
  assert.match(source, /speechStartsNewUserBubble\(event\)[\s\S]*settleCurrentVoiceBubble\(\)[\s\S]*reserveUserVoiceBubble\(\)/);
  assert.match(source, /Текст не удалось отобразить/);
  assert.doesNotMatch(source, /Текст не получен/);
  assert.doesNotMatch(source, /текст распознавания не получен/);
  assert.doesNotMatch(source, /Слышу сейчас|текст ещё не получен/);
});


test("Transcribe Live captions update silently and main Mira final wins", async () => {
  const source = await readFile(new URL("../src/App.tsx", import.meta.url), "utf8");
  assert.match(source, /event\.type === "caption_interim_transcript"[\s\S]*applyCaptionToUserBubble\(event\.text, false\)/);
  assert.match(source, /event\.type === "caption_final_transcript"[\s\S]*applyCaptionToUserBubble\(event\.text, true\)/);
  assert.match(source, /const applyCaptionToUserBubble = useCallback/);
  assert.match(source, /selectProvisionalCaption\(messages\[index\]\.text, clean, _final\)/);
  assert.match(source, /projectshub:\/\/audio\/focus\/acquire/);
  assert.match(source, /text,[\s\S]*revision: messages\[index\]\.revision \+ 1[\s\S]*awaitingTranscript: true,[\s\S]*provisionalCaption: true/);
  assert.match(source, /role === "user" && current\.awaitingTranscript[\s\S]*transcriptRevision: current\.transcriptRevision \+ 1[\s\S]*deliveryNote: undefined/);
  assert.doesNotMatch(source, /"Распознаю"/);
  assert.doesNotMatch(source, /"Транскрипция"/);
  assert.match(source, /event\.type === "caption_unavailable"[\s\S]*Captions are deliberately fail-open/);
});


test("transport loss preserves visible provisional text and exposes recovery", async () => {
  const source = await readFile(new URL("../src/App.tsx", import.meta.url), "utf8");
  assert.match(source, /kind === "transport_error"[\s\S]*Связь прервалась до подтверждения фразы/);
  assert.match(source, /kind === "transport_error"[\s\S]*setRecoverableSourceId\(currentSourceIdRef\.current\)/);
  assert.match(source, /settleCurrentVoiceBubble\("Связь прервалась до подтверждения фразы", true\)/);
  assert.match(source, /message\.deliveryNote[\s\S]*message-delivery-note/);
});


test("restart adopts only unresolved server utterance verdicts", async () => {
  const source = await readFile(new URL("../src/App.tsx", import.meta.url), "utf8");
  assert.match(source, /function adoptPendingVoiceRecovery/);
  assert.match(source, /verdict === "no_turn_closed" \|\| verdict === "turn_closed_no_transcript"/);
  assert.match(source, /adoptPendingVoiceRecovery\(started\)/);
  assert.doesNotMatch(source, /verdict === "turn_committed"[\s\S]*setRecoverableSourceId/);
  assert.match(source, /Восстановить фразу/);
});
test("a Live 503 never looks like a completed development task or hides voice recovery", async () => {
  const source = await readFile(new URL("../src/App.tsx", import.meta.url), "utf8");
  assert.match(source, /kind === "start_error" && voiceStartupFailureNotice\(error\)/);
  assert.match(source, /setVoiceState\("provider_failure"\)/);
  assert.match(source, /setNotice\(voiceStartupFailureNotice\(error\)\)/);
  assert.match(source, /setNotice\(previous => previous \|\| "Не удалось восстановить Live/);
  // This is inside .chat-status, not behind showWork (hidden with chat history).
  const chatStatus = source.slice(source.indexOf('className="chat-status" role={notice'));
  assert.match(chatStatus, /recoverableSourceId && networkOnline && !voiceActive/);
  assert.match(chatStatus, /Восстановить фразу/);
});
