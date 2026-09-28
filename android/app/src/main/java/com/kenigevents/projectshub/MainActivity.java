package com.kenigevents.projectshub;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.graphics.drawable.GradientDrawable;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.Gravity;
import android.view.View;
import android.webkit.CookieManager;
import android.webkit.PermissionRequest;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.FrameLayout;
import android.widget.Toast;

import org.json.JSONObject;

import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicBoolean;

public final class MainActivity extends Activity {
    private static final int REQUEST_MICROPHONE = 101;
    private static final int REQUEST_CALENDAR = 102;
    private static final int REQUEST_NOTIFICATIONS = 103;

    private final Handler main = new Handler(Looper.getMainLooper());
    private final ExecutorService io = Executors.newFixedThreadPool(3);
    private final AtomicBoolean pairing = new AtomicBoolean(false);

    private WebView webView;
    private Button updateButton;
    private SecureStore secureStore;
    private ApiClient api;
    private CalendarExecutor calendar;
    private Notifier notifier;
    private Updater updater;
    private DeviceCommandLoop deviceLoop;
    private Updater.AvailableUpdate availableUpdate;
    private PermissionRequest pendingWebPermission;
    private ApiClient.ClaimedCommand pendingCalendarCommand;
    private DeviceCommandLoop.Completion pendingCalendarCompletion;

    private final Runnable pairingProbe = new Runnable() {
        @Override
        public void run() {
            attemptPairing();
            if (secureStore != null && secureStore.getDeviceToken() == null) {
                main.postDelayed(this, 2500);
            }
        }
    };

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        getWindow().setStatusBarColor(Color.rgb(7, 7, 8));
        getWindow().setNavigationBarColor(Color.rgb(7, 7, 8));

        secureStore = new SecureStore(this);
        api = new ApiClient(BuildConfig.HUB_URL);
        calendar = new CalendarExecutor(this);
        notifier = new Notifier(this);
        updater = new Updater(this, io, new Updater.Listener() {
            @Override
            public void onAvailable(Updater.AvailableUpdate update) {
                availableUpdate = update;
                updateButton.setText("Доступно обновление · "
                        + UpdatePolicy.displayVersion(update.versionName, update.versionCode));
                updateButton.setVisibility(View.VISIBLE);
                updateButton.setEnabled(true);
                notifier.updateAvailable(
                        UpdatePolicy.displayVersion(update.versionName, update.versionCode)
                );
            }

            @Override
            public void onMessage(String message) {
                updateButton.setText(message);
                updateButton.setEnabled(false);
                toast(message);
            }

            @Override
            public void onFailure(String message) {
                updateButton.setText("Повторить обновление");
                updateButton.setEnabled(true);
                toast(message);
            }
        });

        FrameLayout root = new FrameLayout(this);
        root.setBackgroundColor(Color.rgb(7, 7, 8));

        webView = new WebView(this);
        configureWebView(webView);
        root.addView(webView, new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.MATCH_PARENT
        ));

        updateButton = buildUpdateButton();
        FrameLayout.LayoutParams updateParams = new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.WRAP_CONTENT,
                dp(44)
        );
        updateParams.gravity = Gravity.BOTTOM | Gravity.END;
        updateParams.setMargins(dp(16), dp(16), dp(16), dp(24));
        root.addView(updateButton, updateParams);

        setContentView(root);
        webView.loadUrl(BuildConfig.HUB_URL);
        updater.checkForUpdate();
    }

    @Override
    protected void onResume() {
        super.onResume();
        main.removeCallbacks(pairingProbe);
        main.post(pairingProbe);
        if (updater != null) updater.resumePendingInstall();
    }

    @Override
    protected void onPause() {
        main.removeCallbacks(pairingProbe);
        super.onPause();
    }

    @Override
    protected void onDestroy() {
        main.removeCallbacksAndMessages(null);
        if (deviceLoop != null) deviceLoop.stop();
        if (webView != null) {
            webView.stopLoading();
            webView.destroy();
        }
        io.shutdownNow();
        super.onDestroy();
    }

    @Override
    public void onBackPressed() {
        if (webView != null && webView.canGoBack()) {
            webView.goBack();
        } else {
            super.onBackPressed();
        }
    }

    private void configureWebView(WebView view) {
        WebView.setWebContentsDebuggingEnabled(BuildConfig.DEBUG);
        view.setBackgroundColor(Color.rgb(7, 7, 8));
        view.getSettings().setJavaScriptEnabled(true);
        view.getSettings().setDomStorageEnabled(true);
        view.getSettings().setMediaPlaybackRequiresUserGesture(false);
        view.getSettings().setUserAgentString(
                view.getSettings().getUserAgentString()
                        + " ProjectsHubAndroid/" + BuildConfig.VERSION_NAME
        );

        CookieManager cookies = CookieManager.getInstance();
        cookies.setAcceptCookie(true);
        cookies.setAcceptThirdPartyCookies(view, true);

        view.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView webView, WebResourceRequest request) {
                String scheme = request.getUrl().getScheme();
                return !("http".equalsIgnoreCase(scheme) || "https".equalsIgnoreCase(scheme));
            }

            @Override
            public void onPageFinished(WebView webView, String url) {
                super.onPageFinished(webView, url);
                attemptPairing();
            }
        });

        view.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onPermissionRequest(PermissionRequest request) {
                boolean wantsAudio = false;
                for (String resource : request.getResources()) {
                    if (PermissionRequest.RESOURCE_AUDIO_CAPTURE.equals(resource)) {
                        wantsAudio = true;
                        break;
                    }
                }
                if (!wantsAudio) {
                    request.deny();
                    return;
                }
                runOnUiThread(() -> {
                    if (checkSelfPermission(Manifest.permission.RECORD_AUDIO)
                            == PackageManager.PERMISSION_GRANTED) {
                        request.grant(new String[]{PermissionRequest.RESOURCE_AUDIO_CAPTURE});
                    } else {
                        pendingWebPermission = request;
                        requestPermissions(
                                new String[]{Manifest.permission.RECORD_AUDIO},
                                REQUEST_MICROPHONE
                        );
                    }
                });
            }
        });
    }

    private Button buildUpdateButton() {
        Button button = new Button(this);
        button.setAllCaps(false);
        button.setTextColor(Color.rgb(245, 245, 247));
        button.setTextSize(13);
        button.setPadding(dp(16), 0, dp(16), 0);
        button.setVisibility(View.GONE);
        button.setElevation(dp(12));

        GradientDrawable background = new GradientDrawable();
        background.setColor(Color.rgb(31, 31, 35));
        background.setCornerRadius(dp(18));
        background.setStroke(dp(1), Color.argb(45, 255, 255, 255));
        button.setBackground(background);

        button.setOnClickListener(ignored -> {
            Updater.AvailableUpdate update = availableUpdate;
            if (update != null) updater.downloadAndInstall(update);
        });
        return button;
    }

    private void attemptPairing() {
        String existing = secureStore.getDeviceToken();
        if (existing != null && !existing.isBlank()) {
            startDeviceLoop(existing);
            return;
        }
        if (!pairing.compareAndSet(false, true)) return;

        String cookie = CookieManager.getInstance().getCookie(BuildConfig.HUB_URL);
        if (cookie == null || cookie.isBlank()) {
            pairing.set(false);
            return;
        }

        io.execute(() -> {
            try {
                String workspaceId = api.bootstrapWorkspace(cookie);
                String token = api.registerAndroidDevice(
                        cookie,
                        workspaceId,
                        Build.MANUFACTURER + " " + Build.MODEL
                );
                secureStore.putDeviceToken(token);
                runOnUiThread(() -> {
                    startDeviceLoop(token);
                    requestNotificationsOnce();
                });
            } catch (ApiClient.ApiException failure) {
                if (failure.statusCode != 401) {
                    runOnUiThread(() -> toast("Не удалось привязать этот Android к Projects Hub."));
                }
            } catch (Exception failure) {
                runOnUiThread(() -> toast("Не удалось привязать этот Android к Projects Hub."));
            } finally {
                pairing.set(false);
            }
        });
    }

    private synchronized void startDeviceLoop(String token) {
        if (deviceLoop != null) return;
        deviceLoop = new DeviceCommandLoop(
                api,
                token,
                io,
                (command, completion) -> runOnUiThread(() -> handleDeviceCommand(command, completion)),
                () -> runOnUiThread(() -> {
                    secureStore.clearDeviceToken();
                    synchronized (MainActivity.this) {
                        if (deviceLoop != null) deviceLoop.stop();
                        deviceLoop = null;
                    }
                    main.removeCallbacks(pairingProbe);
                    main.post(pairingProbe);
                })
        );
        deviceLoop.start();
    }

    private void handleDeviceCommand(
            ApiClient.ClaimedCommand command,
            DeviceCommandLoop.Completion completion
    ) {
        if (!"calendar.create_event".equals(command.capability)) {
            completion.complete(
                    "failed",
                    errorResult("UNSUPPORTED_CAPABILITY")
            );
            return;
        }

        if (!calendar.hasPermissions()) {
            pendingCalendarCommand = command;
            pendingCalendarCompletion = completion;
            requestPermissions(
                    new String[]{
                            Manifest.permission.READ_CALENDAR,
                            Manifest.permission.WRITE_CALENDAR
                    },
                    REQUEST_CALENDAR
            );
            return;
        }
        confirmCalendar(command, completion);
    }

    private void executeCalendar(
            ApiClient.ClaimedCommand command,
            DeviceCommandLoop.Completion completion
    ) {
        try {
            JSONObject result = calendar.createEvent(command.payload);
            completion.complete("applied", result);
            notifier.eventCreated(command.payload.optString("title", "Событие"));
        } catch (Exception failure) {
            completion.complete(
                    "failed",
                    errorResult(failure.getClass().getSimpleName())
            );
            toast("Не удалось создать событие в календаре.");
        }
    }

    private void confirmCalendar(
            ApiClient.ClaimedCommand command,
            DeviceCommandLoop.Completion completion
    ) {
        final AtomicBoolean done = new AtomicBoolean(false);
        String title = command.payload.optString("title", "");
        String startsAt = command.payload.optString("starts_at", "");
        String message = title + (startsAt.isEmpty() ? "" : "\n" + startsAt);

        new AlertDialog.Builder(this)
                .setTitle("Добавить в календарь?")
                .setMessage(message)
                .setPositiveButton("Добавить", (dialog, which) -> {
                    if (done.compareAndSet(false, true)) {
                        executeCalendar(command, completion);
                    }
                })
                .setNegativeButton("Отмена", (dialog, which) -> {
                    if (done.compareAndSet(false, true)) {
                        completion.complete(
                                "rejected",
                                errorResult("USER_REJECTED")
                        );
                    }
                })
                .setOnCancelListener(dialog -> {
                    if (done.compareAndSet(false, true)) {
                        completion.complete(
                                "rejected",
                                errorResult("USER_REJECTED")
                        );
                    }
                })
                .show();
    }

    private void requestNotificationsOnce() {
        if (Build.VERSION.SDK_INT < 33
                || checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)
                == PackageManager.PERMISSION_GRANTED) {
            return;
        }
        android.content.SharedPreferences prefs = getSharedPreferences(
                "projects_hub_ui",
                MODE_PRIVATE
        );
        if (prefs.getBoolean("notifications_asked", false)) return;
        prefs.edit().putBoolean("notifications_asked", true).apply();
        requestPermissions(
                new String[]{Manifest.permission.POST_NOTIFICATIONS},
                REQUEST_NOTIFICATIONS
        );
    }

    @Override
    public void onRequestPermissionsResult(
            int requestCode,
            String[] permissions,
            int[] grantResults
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults);

        if (requestCode == REQUEST_MICROPHONE) {
            PermissionRequest request = pendingWebPermission;
            pendingWebPermission = null;
            if (request == null) return;
            if (grantResults.length > 0
                    && grantResults[0] == PackageManager.PERMISSION_GRANTED) {
                request.grant(new String[]{PermissionRequest.RESOURCE_AUDIO_CAPTURE});
            } else {
                request.deny();
            }
            return;
        }

        if (requestCode == REQUEST_CALENDAR) {
            ApiClient.ClaimedCommand command = pendingCalendarCommand;
            DeviceCommandLoop.Completion completion = pendingCalendarCompletion;
            pendingCalendarCommand = null;
            pendingCalendarCompletion = null;
            if (command == null || completion == null) return;

            boolean granted = grantResults.length >= 2;
            for (int result : grantResults) {
                granted = granted && result == PackageManager.PERMISSION_GRANTED;
            }
            if (granted) {
                confirmCalendar(command, completion);
            } else {
                completion.complete(
                        "rejected",
                        errorResult("CALENDAR_PERMISSION_DENIED")
                );
            }
        }
    }

    private static JSONObject errorResult(String error) {
        JSONObject result = new JSONObject();
        try {
            result.put("readback_verified", false);
            result.put("error", error);
            return result;
        } catch (org.json.JSONException impossible) {
            throw new IllegalStateException(impossible);
        }
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }

    private void toast(String message) {
        Toast.makeText(this, message, Toast.LENGTH_SHORT).show();
    }
}