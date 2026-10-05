package com.kenigevents.projectshub;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.provider.Settings;
import android.graphics.Color;
import android.graphics.drawable.GradientDrawable;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;
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
    private WebOriginPolicy webOriginPolicy;
    private SecureStore secureStore;
    private ApiClient api;
    private CalendarExecutor calendar;
    private Notifier notifier;
    private Updater updater;
    private DeviceCommandLoop deviceLoop;
    private Updater.AvailableUpdate availableUpdate;
    private int promptedUpdateVersionCode = -1;
    private PermissionRequest pendingWebPermission;
    private boolean microphonePermissionInFlight;
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
        webOriginPolicy = new WebOriginPolicy(BuildConfig.HUB_URL);
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
                updateButton.post(() -> Log.i(
                        "ProjectsHubUpdate",
                        "update_button_ready versionCode=" + update.versionCode
                                + " bounds=" + updateButton.getLeft()
                                + "," + updateButton.getTop()
                                + "," + updateButton.getRight()
                                + "," + updateButton.getBottom()
                ));
                notifier.updateAvailable(
                        UpdatePolicy.displayVersion(update.versionName, update.versionCode)
                );
                showUpdateDialog(update);
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
        webView.clearCache(true);
        if (!handleGitHubReturn(getIntent())) {
            String separator = BuildConfig.HUB_URL.contains("?") ? "&" : "?";
            webView.loadUrl(
                    BuildConfig.HUB_URL
                            + separator
                            + "native_version="
                            + Uri.encode(BuildConfig.VERSION_NAME)
            );
        }
        updater.checkForUpdate();
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        handleGitHubReturn(intent);
    }

    @Override
    protected void onResume() {
        super.onResume();
        main.removeCallbacks(pairingProbe);
        main.post(pairingProbe);
        if (updater != null) {
            updater.resumePendingInstall();
            updater.checkForUpdate();
        }
    }

    @Override
    protected void onPause() {
        main.removeCallbacks(pairingProbe);
        super.onPause();
    }

    @Override
    protected void onDestroy() {
        main.removeCallbacksAndMessages(null);
        PermissionRequest pendingPermission = pendingWebPermission;
        pendingWebPermission = null;
        if (pendingPermission != null) pendingPermission.deny();
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
                String targetUrl = request.getUrl().toString();
                String currentUrl = webView.getUrl() == null ? "" : webView.getUrl();
                if (webOriginPolicy.isTrustedUpdateCheckAction(currentUrl, targetUrl)) {
                    updater.checkForUpdate();
                    toast("Проверяю обновление…");
                    return true;
                }
                if (webOriginPolicy.isTrustedMicrophoneSettingsAction(currentUrl, targetUrl)) {
                    openMicrophoneSettings();
                    return true;
                }
                if (webOriginPolicy.isTrustedGitHubBrowserAction(currentUrl, targetUrl)) {
                    openGitHubManifestInBrowser(request.getUrl());
                    return true;
                }
                if (webOriginPolicy.isTrustedExternalGitHubNavigation(currentUrl, targetUrl)) {
                    openExternalBrowser(request.getUrl());
                    return true;
                }
                String scheme = request.getUrl().getScheme();
                return !("http".equalsIgnoreCase(scheme) || "https".equalsIgnoreCase(scheme));
            }

            @Override
            public void onPageStarted(WebView webView, String url, android.graphics.Bitmap favicon) {
                super.onPageStarted(webView, url, favicon);
                if (!webOriginPolicy.isTrustedPageUrl(url)) {
                    PermissionRequest pending = pendingWebPermission;
                    pendingWebPermission = null;
                    if (pending != null) pending.deny();
                }
            }

            @Override
            public void onPageFinished(WebView webView, String url) {
                super.onPageFinished(webView, url);
                if (webOriginPolicy.isTrustedPageUrl(url)) {
                    attemptPairing();
                }
            }
        });

        view.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onPermissionRequest(PermissionRequest request) {
                String origin = request.getOrigin() == null
                        ? ""
                        : request.getOrigin().toString();
                String currentUrl = webView == null ? "" : webView.getUrl();
                if (!webOriginPolicy.isTrustedPermissionOrigin(origin)
                        || !webOriginPolicy.isTrustedPageUrl(currentUrl)) {
                    Log.w("ProjectsHubWebView", "denied privileged web permission for foreign origin");
                    request.deny();
                    return;
                }
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
                        PermissionRequest previous = pendingWebPermission;
                        if (previous != null && previous != request) previous.deny();
                        pendingWebPermission = request;
                        requestNativeMicrophonePermission();
                    }
                });
            }

            @Override
            public void onPermissionRequestCanceled(PermissionRequest request) {
                runOnUiThread(() -> {
                    if (pendingWebPermission == request) {
                        pendingWebPermission = null;
                    }
                });
            }
        });
    }

    private void requestMicrophoneAfterPairingOnce() {
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO)
                == PackageManager.PERMISSION_GRANTED) {
            requestNotificationsOnce();
            return;
        }
        android.content.SharedPreferences prefs = getSharedPreferences(
                "projects_hub_ui",
                MODE_PRIVATE
        );
        if (prefs.getBoolean("microphone_asked", false)) {
            requestNotificationsOnce();
            return;
        }
        prefs.edit().putBoolean("microphone_asked", true).apply();
        requestNativeMicrophonePermission();
    }

    private void requestNativeMicrophonePermission() {
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO)
                == PackageManager.PERMISSION_GRANTED
                || microphonePermissionInFlight) {
            return;
        }
        microphonePermissionInFlight = true;
        requestPermissions(
                new String[]{Manifest.permission.RECORD_AUDIO},
                REQUEST_MICROPHONE
        );
    }

    private boolean handleGitHubReturn(Intent intent) {
        Uri data = intent == null ? null : intent.getData();
        if (data == null
                || !"projectshub".equalsIgnoreCase(data.getScheme())
                || !"github".equalsIgnoreCase(data.getHost())
                || !"/connected".equals(data.getPath())
                || data.getQuery() != null
                || data.getFragment() != null) {
            return false;
        }
        if (webView != null) {
            webView.loadUrl(BuildConfig.HUB_URL + "?github=connected");
        }
        return true;
    }

    private void openGitHubManifestInBrowser(Uri actionUri) {
        String state = actionUri.getQueryParameter("state");
        if (state == null || state.isBlank()) return;
        Uri launch = Uri.parse(BuildConfig.HUB_URL)
                .buildUpon()
                .appendEncodedPath("api/github/app-manifest/launch")
                .appendQueryParameter("state", state)
                .build();
        openExternalBrowser(launch);
    }

    private void openExternalBrowser(Uri uri) {
        Intent chrome = new Intent(Intent.ACTION_VIEW, uri);
        chrome.setPackage("com.android.chrome");
        try {
            startActivity(chrome);
            return;
        } catch (ActivityNotFoundException chromeUnavailable) {
            Log.i("ProjectsHubGitHub", "Chrome package unavailable; trying default browser");
        }

        Intent browser = new Intent(Intent.ACTION_VIEW, uri);
        try {
            startActivity(browser);
        } catch (ActivityNotFoundException browserUnavailable) {
            Log.w("ProjectsHubGitHub", "No HTTPS browser activity available", browserUnavailable);
            toast("Не найден браузер для открытия GitHub.");
        }
    }

    private void openMicrophoneSettings() {
        Intent intent = new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS);
        intent.setData(Uri.parse("package:" + getPackageName()));
        startActivity(intent);
    }

    private void showMicrophoneSettingsDialog() {
        if (isFinishing() || isDestroyed()) return;
        new AlertDialog.Builder(this)
                .setTitle("Нужен доступ к микрофону")
                .setMessage("Projects Hub использует микрофон только когда вы запускаете голосовой разговор.")
                .setPositiveButton("Открыть настройки", (dialog, which) -> openMicrophoneSettings())
                .setNegativeButton("Позже", null)
                .show();
    }

    private void showUpdateDialog(Updater.AvailableUpdate update) {
        if (update == null
                || update.versionCode <= BuildConfig.VERSION_CODE
                || promptedUpdateVersionCode == update.versionCode
                || isFinishing()
                || isDestroyed()) {
            return;
        }
        promptedUpdateVersionCode = update.versionCode;
        String version = UpdatePolicy.displayVersion(update.versionName, update.versionCode);
        AlertDialog dialog = new AlertDialog.Builder(this)
                .setTitle("Доступно обновление Projects Hub")
                .setMessage("Новая версия " + version + " готова к установке.")
                .setPositiveButton("Обновить", (ignored, which) -> {
                    Log.i(
                            "ProjectsHubUpdate",
                            "update_dialog_confirmed versionCode=" + update.versionCode
                    );
                    updater.downloadAndInstall(update);
                })
                .setNegativeButton("Позже", null)
                .create();
        dialog.setOnShowListener(ignored -> Log.i(
                "ProjectsHubUpdate",
                "update_dialog_shown versionCode=" + update.versionCode
        ));
        dialog.show();
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
            if (update != null) {
                Log.i("ProjectsHubUpdate", "update_button_clicked versionCode=" + update.versionCode);
                updater.downloadAndInstall(update);
            }
        });
        return button;
    }

    private void attemptPairing() {
        String existing = secureStore.getDeviceToken();
        if (existing != null && !existing.isBlank()) {
            startDeviceLoop(existing);
            requestMicrophoneAfterPairingOnce();
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
                    requestMicrophoneAfterPairingOnce();
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
        io.execute(() -> {
            try {
                api.updateDeviceCapabilities(token);
            } catch (Exception failure) {
                Log.w("ProjectsHubDevice", "Could not refresh device capabilities", failure);
            }
            deviceLoop.start();
        });
    }

    private void handleDeviceCommand(
            ApiClient.ClaimedCommand command,
            DeviceCommandLoop.Completion completion
    ) {
        if ("calendar.create_event".equals(command.capability)) {
            if (!calendar.hasWritePermissions()) {
                requestCalendarPermissionFor(command, completion);
                return;
            }
            executeCalendar(command, completion);
            return;
        }

        if ("calendar.read_events".equals(command.capability)) {
            if (!calendar.hasReadPermission()) {
                requestCalendarPermissionFor(command, completion);
                return;
            }
            executeCalendarRead(command, completion);
            return;
        }

        if ("share.open_chooser".equals(command.capability)) {
            executeShareChooser(command, completion);
            return;
        }

        completion.complete(
                "failed",
                errorResult("UNSUPPORTED_CAPABILITY")
        );
    }

    private void requestCalendarPermissionFor(
            ApiClient.ClaimedCommand command,
            DeviceCommandLoop.Completion completion
    ) {
        pendingCalendarCommand = command;
        pendingCalendarCompletion = completion;
        if ("calendar.read_events".equals(command.capability)) {
            requestPermissions(
                    new String[]{Manifest.permission.READ_CALENDAR},
                    REQUEST_CALENDAR
            );
        } else {
            requestPermissions(
                    new String[]{
                            Manifest.permission.READ_CALENDAR,
                            Manifest.permission.WRITE_CALENDAR
                    },
                    REQUEST_CALENDAR
            );
        }
    }

    private void resumeCalendarCommand(
            ApiClient.ClaimedCommand command,
            DeviceCommandLoop.Completion completion
    ) {
        if ("calendar.read_events".equals(command.capability)) {
            if (calendar.hasReadPermission()) {
                executeCalendarRead(command, completion);
            } else {
                completion.complete("rejected", errorResult("CALENDAR_PERMISSION_DENIED"));
            }
            return;
        }
        if ("calendar.create_event".equals(command.capability) && calendar.hasWritePermissions()) {
            executeCalendar(command, completion);
        } else {
            completion.complete("rejected", errorResult("CALENDAR_PERMISSION_DENIED"));
        }
    }

    private void executeCalendarRead(
            ApiClient.ClaimedCommand command,
            DeviceCommandLoop.Completion completion
    ) {
        try {
            completion.complete("applied", calendar.readEvents(command.payload));
        } catch (Exception failure) {
            completion.complete(
                    "failed",
                    errorResult(failure.getClass().getSimpleName())
            );
        }
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

    private void executeShareChooser(
            ApiClient.ClaimedCommand command,
            DeviceCommandLoop.Completion completion
    ) {
        try {
            String url = command.payload.optString("url", "").trim();
            String title = command.payload.optString("title", "Поделиться доской").trim();
            Uri parsed = Uri.parse(url);
            if (!"https".equalsIgnoreCase(parsed.getScheme()) || parsed.getHost() == null) {
                completion.complete("failed", errorResult("INVALID_SHARE_URL"));
                return;
            }

            Intent send = new Intent(Intent.ACTION_SEND);
            send.setType("text/plain");
            send.putExtra(Intent.EXTRA_TEXT, url);
            send.putExtra(Intent.EXTRA_SUBJECT, title);
            Intent chooser = Intent.createChooser(send, title);
            startActivity(chooser);

            completion.complete(
                    "applied",
                    new JSONObject()
                            .put("readback_verified", true)
                            .put("chooser_opened", true)
                            .put("delivery_confirmed", false)
            );
        } catch (ActivityNotFoundException missing) {
            completion.complete("failed", errorResult("SHARE_CHOOSER_UNAVAILABLE"));
        } catch (Exception failure) {
            completion.complete(
                    "failed",
                    errorResult(failure.getClass().getSimpleName())
            );
        }
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
            microphonePermissionInFlight = false;
            boolean granted = grantResults.length > 0
                    && grantResults[0] == PackageManager.PERMISSION_GRANTED;
            PermissionRequest request = pendingWebPermission;
            pendingWebPermission = null;
            if (request != null) {
                String origin = request.getOrigin() == null
                        ? ""
                        : request.getOrigin().toString();
                String currentUrl = webView == null ? "" : webView.getUrl();
                if (granted
                        && webOriginPolicy.isTrustedPermissionOrigin(origin)
                        && webOriginPolicy.isTrustedPageUrl(currentUrl)) {
                    request.grant(new String[]{PermissionRequest.RESOURCE_AUDIO_CAPTURE});
                } else {
                    request.deny();
                }
            }
            if (!granted) {
                showMicrophoneSettingsDialog();
            }
            requestNotificationsOnce();
            return;
        }

        if (requestCode == REQUEST_CALENDAR) {
            ApiClient.ClaimedCommand command = pendingCalendarCommand;
            DeviceCommandLoop.Completion completion = pendingCalendarCompletion;
            pendingCalendarCommand = null;
            pendingCalendarCompletion = null;

            if (command != null && completion != null) {
                resumeCalendarCommand(command, completion);
            }
            requestNotificationsOnce();
            return;
        }
    }

    private static JSONObject errorResult(String error) {
        JSONObject result = new JSONObject();
        try {
            result.put("readback_verified", false);
            result.put("error_code", error);
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