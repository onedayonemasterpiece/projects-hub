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
                new java.util.concurrent.atomic.AtomicReference<>("no navigation");
        scenario.onActivity(activity -> {
            WebView view = activity.themeWebView();
            view.stopLoading();
            view.setWebViewClient(new WebViewClient() {
                @Override public void onPageFinished(WebView page, String finished) {
                    location.set(finished + " (current=" + page.getUrl() + ")");
                    page.evaluateJavascript(
                            "document.body?.textContent?.includes('Theme bridge fixture')===true",
                            value -> { if ("true".equals(value)) loaded.countDown(); });
                }
            });
            // Deterministic local HTML with the exact HTTPS base AND history URL.
            // No external network fixture is required; production still enforces
            // its strict origin and current-page checks for the native bridge.
            view.loadDataWithBaseURL(url,
                    "<!doctype html><html><body>Theme bridge fixture</body></html>",
                    "text/html", "UTF-8", url);
        });
        assertTrue("exact theme fixture must load before posting to bridge: " + location.get(),
                loaded.await(15, TimeUnit.SECONDS));
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
