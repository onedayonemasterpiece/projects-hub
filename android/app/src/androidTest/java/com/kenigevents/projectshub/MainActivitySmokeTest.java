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
