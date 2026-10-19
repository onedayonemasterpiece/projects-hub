package com.kenigevents.projectshub;

import static org.junit.Assert.*;
import static org.junit.Assume.assumeTrue;

import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.ServiceConnection;
import android.graphics.Bitmap;
import android.os.IBinder;
import android.os.SystemClock;
import android.util.Log;
import android.webkit.CookieManager;

import androidx.test.core.app.ActivityScenario;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;

import org.json.JSONArray;
import org.json.JSONObject;
import org.json.JSONTokener;
import org.junit.Test;
import org.junit.runner.RunWith;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;

/**
 * Actual installed Activity/React/native audio path, against an isolated HTTPS
 * backend using the real host, stores and codec. Its AI runner is controlled;
 * this is not a claim about a production model/provider or physical microphone.
 */
@RunWith(AndroidJUnit4.class)
public final class NativeVoiceFlowTest {
    private final Context context = InstrumentationRegistry.getInstrumentation().getTargetContext();
    private final AtomicReference<JSONObject> state = new AtomicReference<>(new JSONObject());
    private final JSONArray transitions = new JSONArray();
    private final JSONArray events = new JSONArray();
    private final JSONObject receipt = new JSONObject();
    private volatile boolean played;
    private NativeVoiceService service;
    private ActivityScenario<MainActivity> scenario;
    private File evidence;
    private final NativeVoiceService.Listener listener = new NativeVoiceService.Listener() {
        @Override public void onState(JSONObject next) {
            state.set(next);
            if (next.optBoolean("playing")) played = true;
            synchronized (transitions) { transitions.put(next); }
        }
        @Override public void onEvent(String eventJson) {
            synchronized (events) { events.put(eventJson); }
        }
    };
    private final CountDownLatch bound = new CountDownLatch(1);
    private final ServiceConnection connection = new ServiceConnection() {
        @Override public void onServiceConnected(ComponentName name, IBinder binder) {
            service = ((NativeVoiceService.LocalBinder) binder).service();
            service.addListener(listener);
            listener.onState(service.stateJson());
            bound.countDown();
        }
        @Override public void onServiceDisconnected(ComponentName name) { service = null; }
    };

    @Test public void actualUiVoiceCapturePlaybackOutageRecoveryAndStop() throws Exception {
        assumeTrue("Requires isolated -PvoiceE2e=true APK and fixture", BuildConfig.VOICE_E2E);
        assertEquals("https://10.0.2.2:8443/", BuildConfig.HUB_URL);
        // UTP pulls this directory before uninstalling the target APK. App-owned
        // Android/data disappears during connectedDebugAndroidTest cleanup.
        String additionalOutput = InstrumentationRegistry.getArguments().getString("additionalTestOutputDir");
        File outputRoot = additionalOutput == null || additionalOutput.isBlank()
                ? context.getExternalFilesDir(null) : new File(additionalOutput);
        evidence = new File(outputRoot, "voice-e2e");
        assertTrue(evidence.isDirectory() || evidence.mkdirs());
        receipt.put("evidence_directory", evidence.getAbsolutePath());
        Log.i("ProjectsHubVoiceE2E", "evidence_directory=" + evidence.getAbsolutePath());
        boolean attached = false;
        try {
            JSONObject login = fixture("GET", "login");
            CountDownLatch cookieSet = new CountDownLatch(1);
            AtomicReference<Boolean> accepted = new AtomicReference<>(false);
            InstrumentationRegistry.getInstrumentation().runOnMainSync(() -> {
                CookieManager cookies = CookieManager.getInstance();
                cookies.setAcceptCookie(true);
                cookies.setCookie(BuildConfig.HUB_URL, login.optString("cookie"), success -> {
                    accepted.set(success);
                    cookies.flush();
                    cookieSet.countDown();
                });
            });
            assertTrue("Fixture cookie callback", cookieSet.await(10, TimeUnit.SECONDS));
            assertTrue("Fixture session cookie accepted", accepted.get());
            attached = context.bindService(new Intent(context, NativeVoiceService.class), connection,
                    Context.BIND_AUTO_CREATE);
            assertTrue("Bind real native owner", attached);
            assertTrue("Native owner binding", bound.await(15, TimeUnit.SECONDS));
            scenario = ActivityScenario.launch(MainActivity.class);
            // Its normal onCreate loads HUB_URL with native_version. Keep the
            // production WebViewClient, origin checks and bridge unchanged.
            scenario.onActivity(activity -> assertNotNull(activity.themeWebView()));
            await("React microphone enabled", 45, () -> "true".equals(js(
                    "Boolean(document.querySelector('button.voice-orb:not(:disabled)'))")));
            receipt.put("real_page", js("location.href"));
            clickMic();
            await("native live capture", 45, () -> nativeStatus("live") && capturing());
            String conversation = state.get().getString("conversation_id");
            receipt.put("conversation_id", conversation);
            snapshot("connected");
            fixture("POST", "speak?name=hello");
            await("encoded microphone reached actual host and produced output", 45, () -> {
                JSONObject remote = fixture("GET", "state");
                return remote.optLong("main_audio_bytes") > 0 && remote.optDouble("main_rms") > 0
                        && nonemptySources(remote.opt("encoded_sources"))
                        && remote.optLong("turns") >= 1 && remote.optLong("output_turns") >= 1 && played;
            });
            await("reply visible in actual React conversation", 15,
                    () -> visiblePage().contains("Проверка голоса пройдена"));
            snapshot("live");
            await("first reply playback durably completed", 30, () -> !state.get().optBoolean("playing")
                    && completedPlaybackSources(fixture("GET", "state")) >= 1);
            JSONObject beforeOutage = fixture("GET", "state");
            long turnsBefore = beforeOutage.getLong("turns");
            long outputsBefore = beforeOutage.getLong("output_turns");
            int completedBefore = completedPlaybackSources(beforeOutage);
            long connectionsBefore = beforeOutage.getLong("connection_count");
            long queuedBefore = state.get().optLong("queued_audio_ms");
            fixture("POST", "outage");
            await("outage keeps actual capture active", 30, () -> capturing()
                    && (nativeStatus("recording_only") || nativeStatus("delayed") || nativeStatus("starting")));
            fixture("POST", "speak?name=offline");
            // Let the real native VAD close this entire offline utterance.
            // Reconnecting mid-utterance would legitimately send its live tail.
            SystemClock.sleep(1500);
            await("compressed recording durable during outage", 20,
                    () -> capturing() && state.get().optLong("queued_audio_ms") > queuedBefore + 300);
            long offlineQueue = state.get().getLong("queued_audio_ms");
            receipt.put("offline_queued_audio_ms", offlineQueue);
            snapshot("outage");
            fixture("POST", "restore");
            await("reconnect and deliver durable original source", 60, () -> {
                JSONObject remote = fixture("GET", "state");
                return capturing() && nativeStatus("live")
                        && remote.optLong("connection_count") > connectionsBefore
                        && state.get().optLong("queued_audio_ms") < offlineQueue;
            });
            snapshot("recovered");
            // Old recorded speech must not become a fresh main-model request.
            SystemClock.sleep(4000);
            assertEquals("Recovered source is not semantically replayed", turnsBefore,
                    fixture("GET", "state").getLong("turns"));
            played = false;
            fixture("POST", "speak?name=overlap");
            await("fresh speech after recovery produces a complete played answer", 45, () -> {
                JSONObject remote = fixture("GET", "state");
                return remote.optLong("turns") > turnsBefore
                        && remote.optLong("output_turns") > outputsBefore && played;
            });
            await("recovered reply playback completed", 30,
                    () -> !state.get().optBoolean("playing")
                            && completedPlaybackSources(fixture("GET", "state")) > completedBefore);
            await("new reply visible after reconnect", 15,
                    () -> {
                        String page = visiblePage();
                        return page.contains("второй вопрос после восстановления связи")
                                && page.contains("Проверка голоса пройдена");
                    });
            snapshot("recovered-answer");
            JSONObject beforeShort = fixture("GET", "state");
            long shortOutputsBefore = beforeShort.getLong("output_turns");
            int completedBeforeShort = completedPlaybackSources(beforeShort);
            played = false;
            fixture("POST", "speak?name=short");
            await("real microphone short request receives a sub-200ms answer", 45, () -> {
                JSONObject remote = fixture("GET", "state");
                return remote.optLong("output_turns") > shortOutputsBefore && played;
            });
            await("120ms answer advances physical playhead and durably completes", 12,
                    () -> !state.get().optBoolean("playing")
                            && completedPlaybackSources(fixture("GET", "state")) > completedBeforeShort);
            snapshot("short-answer");
            JSONObject beforeTail = fixture("GET", "state");
            long tailOutputsBefore = beforeTail.getLong("output_turns");
            int completedBeforeTail = completedPlaybackSources(beforeTail);
            played = false;
            fixture("POST", "speak?name=tail");
            await("same-source output resumes after a one-second starvation gap", 45, () -> {
                JSONObject remote = fixture("GET", "state");
                return remote.optLong("output_turns") > tailOutputsBefore && played;
            });
            await("final 120ms tail drains after earlier frames have played", 12,
                    () -> !state.get().optBoolean("playing")
                            && completedPlaybackSources(fixture("GET", "state")) > completedBeforeTail);
            snapshot("terminal-tail");
            clickMic();
            await("explicit UI Stop releases microphone and speaker", 20,
                    () -> nativeStatus("idle") && !capturing() && !state.get().optBoolean("playing"));
            snapshot("stopped");
            long stoppedConnections = fixture("GET", "state").getLong("connection_count");
            long stoppedRevision = state.get().optLong("revision");
            SystemClock.sleep(6000);
            assertTrue("Stop never restarts recording", nativeStatus("idle") && !capturing());
            assertFalse("Stop never restarts playback", state.get().optBoolean("playing"));
            assertEquals("No late Start after Stop", stoppedConnections,
                    fixture("GET", "state").getLong("connection_count"));
            receipt.put("stopped_revision", stoppedRevision);
            verifyDurablePlayback(login, conversation);
            receipt.put("passed", true);
        } catch (Exception | AssertionError failure) {
            receipt.put("passed", false).put("failure", Log.getStackTraceString(failure));
            throw failure;
        } finally {
            // Keep the actual failed state even if Activity teardown also fails.
            checkpoint();
            if (scenario != null) {
                try { snapshot("final"); } catch (Exception ignored) { }
                try { if (capturing()) clickMic(); } catch (Exception ignored) { }
                scenario.close();
            }
            if (attached) {
                InstrumentationRegistry.getInstrumentation().runOnMainSync(() -> {
                    if (service != null) service.removeListener(listener);
                    context.unbindService(connection);
                });
            }
            checkpoint();
        }
    }

    private void checkpoint() throws Exception {
        receipt.put("final_native", state.get());
        synchronized (transitions) { receipt.put("native_transitions", new JSONArray(transitions.toString())); }
        synchronized (events) { receipt.put("native_events", new JSONArray(events.toString())); }
        try { receipt.put("fixture", fixture("GET", "state")); } catch (Exception ignored) { }
        write("voice-flow.json", receipt.toString(2));
    }

    private void verifyDurablePlayback(JSONObject login, String conversation) throws Exception {
        NativeVoiceStore.Scope scope = new NativeVoiceStore.Scope(login.getString("actor_id"),
                login.getString("workspace_id"), conversation);
        try (NativeVoiceStore store = new NativeVoiceStore(new File(context.getNoBackupFilesDir(), "voice-v2"),
                scope, new NativeVoiceStore.Limits(128L * 1024 * 1024, 4096, 120L * 60 * 1_000_000))) {
            JSONArray outputs = new JSONArray();
            long playedUs = 0;
            for (var position : store.playbackPositions()) {
                playedUs += position.playedUs();
                outputs.put(new JSONObject().put("received_chunks", position.receivedThrough())
                        .put("played_us", position.playedUs()).put("duration_us", position.durationUs()));
            }
            receipt.put("durable_output_positions", outputs);
            receipt.put("durable_input_sources", store.sources().size());
            receipt.put("pending_after_stop_us", store.pendingDurationUs());
            assertTrue("Actual decoded output was marked played in durable store", playedUs > 100_000);
            assertTrue("Actual compressed input sources persisted", store.sources().size() > 0);
        }
    }

    private boolean nativeStatus(String expected) { return expected.equals(state.get().optString("status")); }
    private boolean capturing() { return state.get().optBoolean("capturing"); }
    private int completedPlaybackSources(JSONObject remote) {
        JSONArray sources = remote.optJSONArray("encoded_sources");
        if (sources == null) return 0;
        int complete = 0;
        for (int i = 0; i < sources.length(); i++) {
            JSONObject source = sources.optJSONObject(i);
            if (source == null) continue;
            long duration = source.optLong("capture_end_us");
            // Wire playback receipts have millisecond precision. Input-only
            // sources never have a positive AudioTrack playback position.
            if (source.optInt("complete") == 1 && duration > 0
                    && source.optLong("played_us") > 0
                    && source.optLong("played_us") + 1000 >= duration) complete++;
        }
        return complete;
    }
    private boolean nonemptySources(Object sources) {
        if (sources instanceof Number number) return number.longValue() > 0;
        if (sources instanceof JSONArray array) return array.length() > 0;
        if (sources instanceof JSONObject object) return object.length() > 0;
        return false;
    }
    @FunctionalInterface private interface Check { boolean ok() throws Exception; }
    private void await(String description, int seconds, Check check) throws Exception {
        long until = SystemClock.elapsedRealtime() + seconds * 1000L;
        do {
            if (check.ok()) return;
            SystemClock.sleep(250);
        } while (SystemClock.elapsedRealtime() < until);
        fail(description + "; native=" + state.get() + "; fixture=" + fixture("GET", "state")
                + "; page=" + js("document.body.innerText.slice(-1600)"));
    }
    private String js(String expression) throws Exception {
        AtomicReference<String> value = new AtomicReference<>();
        CountDownLatch evaluated = new CountDownLatch(1);
        scenario.onActivity(activity -> activity.themeWebView().evaluateJavascript(expression, result -> {
            value.set(result); evaluated.countDown();
        }));
        assertTrue("Real WebView JavaScript callback", evaluated.await(10, TimeUnit.SECONDS));
        return value.get();
    }
    private void clickMic() throws Exception {
        assertEquals("Real React microphone button clicked", "true", js("(() => {const b="
                + "document.querySelector('button.voice-orb');if(!b||b.disabled)return false;b.click();return true})()"));
    }
    private String visiblePage() throws Exception {
        Object text = new JSONTokener(js("document.body.innerText")).nextValue();
        return text instanceof String ? (String) text : "";
    }
    private JSONObject fixture(String method, String path) throws Exception {
        HttpURLConnection request = (HttpURLConnection) new URL(BuildConfig.HUB_URL + "_e2e/" + path).openConnection();
        request.setRequestMethod(method);
        request.setConnectTimeout(5000);
        request.setReadTimeout(15000);
        try {
            int status = request.getResponseCode();
            ByteArrayOutputStream bytes = new ByteArrayOutputStream();
            try (InputStream stream = status < 400 ? request.getInputStream() : request.getErrorStream()) {
                byte[] block = new byte[8192];
                int n;
                while (stream != null && (n = stream.read(block)) >= 0) {
                    bytes.write(block, 0, n);
                    if (bytes.size() > 2_000_000) throw new IllegalStateException("Fixture response too large");
                }
            }
            String body = bytes.toString(StandardCharsets.UTF_8.name());
            assertTrue("Fixture " + method + " " + path + " status=" + status + " " + body, status >= 200 && status < 300);
            return new JSONObject(body);
        } finally { request.disconnect(); }
    }
    private void snapshot(String name) throws Exception {
        Bitmap screenshot = InstrumentationRegistry.getInstrumentation().getUiAutomation().takeScreenshot();
        assertNotNull("Installed UI screenshot " + name, screenshot);
        try (FileOutputStream out = new FileOutputStream(new File(evidence, name + ".png"))) {
            assertTrue(screenshot.compress(Bitmap.CompressFormat.PNG, 100, out));
        } finally { screenshot.recycle(); }
        write(name + ".json", new JSONObject().put("native", state.get())
                .put("fixture", fixture("GET", "state")).put("ui", js("document.body.innerText")).toString(2));
    }
    private void write(String name, String value) throws Exception {
        if (evidence == null) return;
        try (FileOutputStream out = new FileOutputStream(new File(evidence, name))) {
            out.write(value.getBytes(StandardCharsets.UTF_8));
        }
    }
}
