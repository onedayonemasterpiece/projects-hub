package com.kenigevents.projectshub;

import static org.junit.Assert.*;
import android.graphics.Color;
import android.graphics.drawable.ColorDrawable;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowInsetsController;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebResourceError;
import android.webkit.ServiceWorkerClient;
import android.webkit.ServiceWorkerController;
import androidx.test.platform.app.InstrumentationRegistry;
import org.junit.After;
import org.json.JSONObject;
import java.io.ByteArrayInputStream;
import java.nio.charset.StandardCharsets;
import androidx.test.core.app.ActivityScenario;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import org.junit.Test;
import org.junit.runner.RunWith;

@RunWith(AndroidJUnit4.class)
public class MainActivitySmokeTest {
    @After public void restoreServiceWorkerClient() {
        // The API 31 image's WebView wraps a null client, then dereferences it
        // on the next worker request. Remove the fixture interception with a
        // non-null client returning null: WebView keeps its normal network path.
        InstrumentationRegistry.getInstrumentation().runOnMainSync(() ->
                ServiceWorkerController.getInstance().setServiceWorkerClient(new ServiceWorkerClient() {
                    @Override public WebResourceResponse shouldInterceptRequest(WebResourceRequest request) {
                        return null;
                    }
                }));
    }

    private WebResourceResponse fixtureResponse(String url, WebResourceRequest request) {
        String html = url.equals(request.getUrl().toString())
                ? "<!doctype html><html><body>Theme bridge fixture</body></html>" : "";
        return new WebResourceResponse("text/html", "UTF-8",
                new ByteArrayInputStream(html.getBytes(StandardCharsets.UTF_8)));
    }

    @Test public void activityLaunches() {
        try (ActivityScenario<MainActivity> scenario = ActivityScenario.launch(MainActivity.class)) {
            scenario.onActivity(activity -> assertNotNull(activity));
        }
    }

    @Test public void nativeVoicePublicationKeepsEveryPublicChangeAndCurrentSnapshot() throws Exception {
        java.lang.reflect.Method publish = NativeVoiceService.class.getDeclaredMethod("publish");
        publish.setAccessible(true);
        InstrumentationRegistry.getInstrumentation().runOnMainSync(() -> {
            // Exercise the actual publisher and Android JSONObject on the main
            // looper, without onCreate, codecs, microphone or any network work.
            NativeVoiceService service = new NativeVoiceService();
            java.util.List<JSONObject> received = new java.util.ArrayList<>();
            NativeVoiceService.Listener subscriber = new NativeVoiceService.Listener() {
                @Override public void onState(JSONObject state) { received.add(state); }
                @Override public void onEvent(String event) { fail("Publication cannot invent an event"); }
            };
            try {
                service.addListener(subscriber);
                assertEquals(0, service.stateJson().getLong("revision"));
                publish.invoke(service);
                for (int i = 0; i < 100; i++) publish.invoke(service);
                assertEquals("Repeated ACK-only state has one publication", 1, received.size());
                assertEquals(1, service.stateJson().getLong("revision"));

                Object[][] changes = {
                    {"status", "status", "starting"},
                    {"conversationId", "conversation_id", "conv_A"},
                    {"sessionId", "session_id", "live_A"},
                    {"sourceId", "source_id", "source_A"},
                    {"localSourceId", "local_source_id", "local_A"},
                    {"capturing", "capturing", true},
                    {"playing", "playing", true},
                    {"queuedAudioMs", "queued_audio_ms", 20L},
                    {"status", "status", "live"},
                    {"code", "code", "NATIVE_VOICE_TEST_WARNING"},
                    {"code", "code", "NATIVE_VOICE_TEST_ERROR"},
                    {"status", "status", "error"},
                    {"sourceId", "source_id", "source_B"},
                    {"localSourceId", "local_source_id", "local_B"},
                    {"queuedAudioMs", "queued_audio_ms", 21L},
                    {"code", "code", null},
                    {"status", "status", "stopping"},
                    {"capturing", "capturing", false},
                    {"playing", "playing", false},
                    {"sessionId", "session_id", null},
                    {"status", "status", "idle"},
                    {"status", "status", "starting"},
                    {"sessionId", "session_id", "live_B"},
                    {"status", "status", "live"}
                };
                for (Object[] change : changes) {
                    java.lang.reflect.Field field = NativeVoiceService.class.getDeclaredField((String) change[0]);
                    field.setAccessible(true);
                    field.set(service, change[2]);
                    int expected = received.size() + 1;
                    publish.invoke(service);
                    assertEquals("Deliver changed " + change[1] + " immediately", expected, received.size());
                    JSONObject state = received.get(expected - 1);
                    assertEquals(expected, state.getLong("revision"));
                    assertEquals(change[2] == null ? JSONObject.NULL : change[2], state.get((String) change[1]));
                    assertEquals(service.stateJson().toString(), state.toString());
                    publish.invoke(service);
                    assertEquals("Suppress only an identical projection", expected, received.size());
                }

                service.removeListener(subscriber);
                java.util.List<JSONObject> nextReceived = new java.util.ArrayList<>();
                service.addListener(new NativeVoiceService.Listener() {
                    @Override public void onState(JSONObject state) { nextReceived.add(state); }
                    @Override public void onEvent(String event) { fail("Unexpected event"); }
                });
                publish.invoke(service);
                assertTrue(nextReceived.isEmpty());
                // NativeVoiceBridge's existing attach/state ACK reads this
                // snapshot directly, independent of newly published events.
                assertEquals(received.get(received.size() - 1).toString(), service.stateJson().toString());
                service.rejectStart("NATIVE_VOICE_TEST_ERROR");
                assertEquals(1, nextReceived.size());
                assertEquals("error", nextReceived.get(0).getString("status"));
                assertEquals("NATIVE_VOICE_TEST_ERROR", nextReceived.get(0).getString("code"));
            } catch (Exception failure) {
                throw new AssertionError(failure);
            } finally {
                service.removeListener(subscriber);
            }
        });
    }

    @Test public void debugUpdaterDiscardsStableOffersAtEveryEntryPoint() {
        assertTrue("This regression exercises the debug update channel", BuildConfig.DEBUG);
        android.content.Context context = InstrumentationRegistry.getInstrumentation().getTargetContext();
        android.content.SharedPreferences preferences = context.getSharedPreferences(
                "debug_updater_guard_test", android.content.Context.MODE_PRIVATE);
        java.util.concurrent.ExecutorService noWork = java.util.concurrent.Executors.newSingleThreadExecutor();
        noWork.shutdown(); // Any attempted scheduling fails before network work starts.
        try {
            InstrumentationRegistry.getInstrumentation().runOnMainSync(() -> {
                android.app.Activity host = new android.app.Activity() {
                    @Override public android.content.SharedPreferences getSharedPreferences(String name, int mode) {
                        assertEquals("projects_hub_update", name);
                        return preferences;
                    }
                    @Override public android.content.pm.PackageManager getPackageManager() {
                        throw new AssertionError("Debug update must not request installation permission");
                    }
                };
                Updater.Listener noOffer = new Updater.Listener() {
                    @Override public void onAvailable(Updater.AvailableUpdate update) { fail("Stable offer in debug"); }
                    @Override public void onMessage(String message) { fail("Debug installer message"); }
                    @Override public void onFailure(String message) { fail("Debug update attempted"); }
                };
                Updater.AvailableUpdate stale = new Updater.AvailableUpdate(63, "0.1.62",
                        "https://github.com/onedayonemasterpiece/projects-hub/releases/download/android-v63/projects-hub.apk",
                        "ec5ba31cd8a32fb566b74415b6cb59d14b29d2a3863f5889a8f85332c1a5c89f");
                String encoded = PendingUpdateState.encode(stale.versionCode, stale.versionName, stale.apkUrl, stale.sha256);
                assertTrue(preferences.edit().putString("pending_install_v1", encoded)
                        .putString("unrelated", "preserved").commit());
                Updater updater = new Updater(host, noWork, noOffer);
                assertFalse("Discard persisted offer at construction", preferences.contains("pending_install_v1"));
                assertEquals("preserved", preferences.getString("unrelated", null));
                Runnable[] entryPoints = {updater::checkForUpdate, updater::resumePendingInstall,
                        () -> updater.downloadAndInstall(stale)};
                for (Runnable entryPoint : entryPoints) {
                    assertTrue(preferences.edit().putString("pending_install_v1", encoded).commit());
                    entryPoint.run();
                    assertFalse("Discard stale offer without network or installer work",
                            preferences.contains("pending_install_v1"));
                    assertEquals("preserved", preferences.getString("unrelated", null));
                }
            });
        } finally {
            preferences.edit().remove("pending_install_v1").remove("unrelated").commit();
        }
    }

    @Test public void completionSchedulingCannotBreakStartupOrReopen() {
        // The prior launch smoke never exercised the new post-pairing Android
        // JobScheduler registration. Scheduling may fail on vendor ROMs but
        // must not tear down the WebView or the Activity.
        try (ActivityScenario<MainActivity> scenario = ActivityScenario.launch(MainActivity.class)) {
            scenario.onActivity(activity -> {
                assertNotNull(activity.themeWebView());
                DevelopmentCompletionJob.schedule(activity.getApplicationContext());
                assertNotNull(activity.themeWebView());
            });
            scenario.recreate();
            scenario.onActivity(activity -> {
                assertNotNull(activity.themeWebView());
                DevelopmentCompletionJob.schedule(activity.getApplicationContext());
                assertNotNull(activity.themeWebView());
            });
        }
    }

    private void page(ActivityScenario<MainActivity> scenario, String url) throws Exception {
        CountDownLatch loaded = new CountDownLatch(1);
        java.util.concurrent.atomic.AtomicReference<String> location =
                new java.util.concurrent.atomic.AtomicReference<>("no callback");
        java.util.concurrent.atomic.AtomicReference<String> dom =
                new java.util.concurrent.atomic.AtomicReference<>("not checked");
        java.util.concurrent.atomic.AtomicInteger viewRequests = new java.util.concurrent.atomic.AtomicInteger();
        java.util.concurrent.atomic.AtomicInteger workerRequests = new java.util.concurrent.atomic.AtomicInteger();
        scenario.onActivity(activity -> {
            WebView view = activity.themeWebView();
            view.stopLoading();
            // Earlier launch tests can install the PWA service worker for this
            // origin. Its network-first fetches bypass WebViewClient, so intercept
            // BOTH request paths; otherwise a live/cached app shell replaces HTML.
            ServiceWorkerController.getInstance().setServiceWorkerClient(new ServiceWorkerClient() {
                @Override public WebResourceResponse shouldInterceptRequest(WebResourceRequest request) {
                    workerRequests.incrementAndGet();
                    return fixtureResponse(url, request);
                }
            });
            view.setWebViewClient(new WebViewClient() {
                @Override public WebResourceResponse shouldInterceptRequest(
                        WebView page, WebResourceRequest request) {
                    // Exercise a real HTTPS document/origin, not the synthetic
                    // history URL of loadDataWithBaseURL. Never use the network.
                    viewRequests.incrementAndGet();
                    return fixtureResponse(url, request);
                }
                @Override public void onPageFinished(WebView page, String finished) {
                    location.set("finished=" + finished + " current=" + page.getUrl());
                }
                @Override public void onReceivedError(WebView page, WebResourceRequest request,
                        WebResourceError error) {
                    if (request.isForMainFrame()) location.set("error=" + error.getErrorCode()
                            + " url=" + request.getUrl());
                }
            });
            view.loadUrl(url);
        });
        // Page-finished is a navigation callback, not our fixture-readiness
        // condition. Old startup callbacks can arrive after stopLoading; poll
        // the exact current document and content before exercising the bridge.
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(15);
        while (System.nanoTime() < deadline && loaded.getCount() != 0) {
            scenario.onActivity(activity -> {
                WebView view = activity.themeWebView();
                view.evaluateJavascript(
                        "location.href===" + JSONObject.quote(url)
                        + " && document.readyState==='complete'"
                        + " && document.body?.textContent==='Theme bridge fixture'",
                        value -> {
                            dom.set("ready=" + value + " current=" + view.getUrl());
                            if ("true".equals(value) && url.equals(view.getUrl())) loaded.countDown();
                        });
            });
            loaded.await(50, TimeUnit.MILLISECONDS);
        }
        // Capture individual failed predicates, not only an opaque false. This is
        // synthetic test content; never collect real app/account document text.
        if (loaded.getCount() != 0) {
            CountDownLatch inspected = new CountDownLatch(1);
            scenario.onActivity(activity -> activity.themeWebView().evaluateJavascript(
                    "JSON.stringify({href:location.href,state:document.readyState,"
                    + "fixture:document.body?.textContent==='Theme bridge fixture',"
                    + "bodyLength:document.body?.textContent?.length,"
                    + "worker:navigator.serviceWorker?.controller?.scriptURL||null})",
                    value -> { dom.set(value); inspected.countDown(); }));
            inspected.await(2, TimeUnit.SECONDS);
        }
        android.util.Log.i("ThemeFixture", "viewRequests=" + viewRequests.get()
                + " workerRequests=" + workerRequests.get() + " " + dom.get());
        assertTrue("exact theme fixture must load before posting to bridge: "
                + location.get() + "; " + dom.get(), loaded.getCount() == 0);
    }

    private int themedRootColor(MainActivity activity) {
        ViewGroup content = activity.findViewById(android.R.id.content);
        return ((ColorDrawable) content.getChildAt(0).getBackground()).getColor();
    }

    private void message(ActivityScenario<MainActivity> scenario, String theme, int revision) throws Exception {
        CountDownLatch acknowledged = new CountDownLatch(1);
        scenario.onActivity(activity -> activity.themeWebView().evaluateJavascript(
                "window.themeAck=false; projectsHubTheme.onmessage=()=>{window.themeAck=true};"
                + "projectsHubTheme.postMessage(JSON.stringify({version:1,request_id:'fixture',theme:'"
                + theme + "',revision:" + revision + ",reset:false}));", ignored -> {}));
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(5);
        while (System.nanoTime() < deadline && acknowledged.getCount() != 0) {
            scenario.onActivity(activity -> activity.themeWebView().evaluateJavascript(
                    "window.themeAck===true", value -> { if ("true".equals(value)) acknowledged.countDown(); }));
            acknowledged.await(50, TimeUnit.MILLISECONDS);
        }
        assertEquals(0, acknowledged.getCount());
    }

    @Test public void trustedPageAppliesBothPalettesWithoutReplacingWebView() throws Exception {
        try (ActivityScenario<MainActivity> scenario = ActivityScenario.launch(MainActivity.class)) {
            page(scenario, BuildConfig.HUB_URL + "__theme_fixture__");
            final WebView[] original = new WebView[1];
            scenario.onActivity(activity -> original[0] = activity.themeWebView());
            message(scenario, "light", 1);
            scenario.onActivity(activity -> {
                assertSame(original[0], activity.themeWebView());
                assertEquals(Color.rgb(244,245,247), themedRootColor(activity));
                if (android.os.Build.VERSION.SDK_INT < 35) {
                    assertEquals(Color.rgb(244,245,247), activity.getWindow().getStatusBarColor());
                } else {
                    assertTrue((activity.getWindow().getInsetsController().getSystemBarsAppearance()
                            & WindowInsetsController.APPEARANCE_LIGHT_STATUS_BARS) != 0);
                }
                assertTrue((activity.getWindow().getDecorView().getSystemUiVisibility()
                        & View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR) != 0);
            });
            message(scenario, "dark", 2);
            scenario.onActivity(activity -> {
                assertSame(original[0], activity.themeWebView());
                assertEquals(Color.rgb(7,7,8), themedRootColor(activity));
                if (android.os.Build.VERSION.SDK_INT < 35) {
                    assertEquals(Color.rgb(7,7,8), activity.getWindow().getNavigationBarColor());
                } else {
                    assertEquals(0, activity.getWindow().getInsetsController().getSystemBarsAppearance()
                            & WindowInsetsController.APPEARANCE_LIGHT_NAVIGATION_BARS);
                }
                assertEquals(0, activity.getWindow().getDecorView().getSystemUiVisibility()
                        & View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR);
            });
            page(scenario, "https://foreign.invalid/__theme_fixture__");
            CountDownLatch checked = new CountDownLatch(1);
            scenario.onActivity(activity -> activity.themeWebView().evaluateJavascript(
                    "typeof projectsHubTheme", value -> { assertEquals("\"undefined\"", value); checked.countDown(); }));
            assertTrue(checked.await(5, TimeUnit.SECONDS));
        }
    }
}
