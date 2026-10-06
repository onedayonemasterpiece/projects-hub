package com.kenigevents.projectshub;

import static org.junit.Assert.*;
import android.graphics.Color;
import android.view.View;
import android.webkit.WebView;
import android.webkit.WebViewClient;
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

    private void page(ActivityScenario<MainActivity> scenario, String url) throws Exception {
        CountDownLatch loaded = new CountDownLatch(1);
        scenario.onActivity(activity -> {
            WebView view = activity.themeWebView();
            view.setWebViewClient(new WebViewClient() {
                @Override public void onPageFinished(WebView page, String finished) { loaded.countDown(); }
            });
            // Prepared bridge fixture, not provider/microphone acceptance.
            view.loadDataWithBaseURL(url, "<html><body>Theme bridge fixture</body></html>", "text/html", "UTF-8", null);
        });
        assertTrue(loaded.await(10, TimeUnit.SECONDS));
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
            page(scenario, BuildConfig.HUB_URL);
            final WebView[] original = new WebView[1];
            scenario.onActivity(activity -> original[0] = activity.themeWebView());
            message(scenario, "light", 1);
            scenario.onActivity(activity -> {
                assertSame(original[0], activity.themeWebView());
                assertEquals(Color.rgb(244,245,247), activity.getWindow().getStatusBarColor());
                assertTrue((activity.getWindow().getDecorView().getSystemUiVisibility()
                        & View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR) != 0);
            });
            message(scenario, "dark", 2);
            scenario.onActivity(activity -> {
                assertSame(original[0], activity.themeWebView());
                assertEquals(Color.rgb(7,7,8), activity.getWindow().getNavigationBarColor());
                assertEquals(0, activity.getWindow().getDecorView().getSystemUiVisibility()
                        & View.SYSTEM_UI_FLAG_LIGHT_NAVIGATION_BAR);
            });
            page(scenario, "https://foreign.invalid/");
            CountDownLatch checked = new CountDownLatch(1);
            scenario.onActivity(activity -> activity.themeWebView().evaluateJavascript(
                    "typeof projectsHubTheme", value -> { assertEquals("\"undefined\"", value); checked.countDown(); }));
            assertTrue(checked.await(5, TimeUnit.SECONDS));
        }
    }
}
