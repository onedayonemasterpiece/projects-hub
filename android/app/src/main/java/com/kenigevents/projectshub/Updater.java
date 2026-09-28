package com.kenigevents.projectshub;

import android.app.Activity;
import android.content.Intent;
import android.net.Uri;
import android.provider.Settings;
import android.util.Log;

import androidx.core.content.FileProvider;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.Locale;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.atomic.AtomicBoolean;

final class Updater {
    private static final String TAG = "ProjectsHubUpdate";

    interface Listener {
        void onAvailable(AvailableUpdate update);
        void onMessage(String message);
        void onFailure(String message);
    }

    static final class AvailableUpdate {
        final int versionCode;
        final String versionName;
        final String apkUrl;
        final String sha256;

        AvailableUpdate(int versionCode, String versionName, String apkUrl, String sha256) {
            this.versionCode = versionCode;
            this.versionName = versionName;
            this.apkUrl = apkUrl;
            this.sha256 = sha256.toLowerCase(Locale.ROOT);
        }
    }

    private final Activity activity;
    private final ExecutorService executor;
    private final Listener listener;
    private final AtomicBoolean checking = new AtomicBoolean(false);
    private volatile AvailableUpdate pendingInstall;

    Updater(Activity activity, ExecutorService executor, Listener listener) {
        this.activity = activity;
        this.executor = executor;
        this.listener = listener;
    }

    void checkForUpdate() {
        if (!checking.compareAndSet(false, true)) return;
        executor.execute(() -> {
            try {
                for (int attempt = 1; attempt <= 3; attempt++) {
                    try {
                        AvailableUpdate update = fetchLatestUpdate();
                        if (update != null && UpdatePolicy.shouldOffer(
                                BuildConfig.VERSION_CODE,
                                update.versionCode
                        )) {
                            Log.i(TAG, "update_available versionCode=" + update.versionCode);
                            activity.runOnUiThread(() -> listener.onAvailable(update));
                        } else {
                            Log.i(TAG, "update_current versionCode=" + BuildConfig.VERSION_CODE);
                        }
                        return;
                    } catch (Exception failure) {
                        Log.w(
                                TAG,
                                "update_check_retry attempt=" + attempt
                                        + " error=" + failure.getClass().getSimpleName()
                        );
                        if (attempt >= 3) {
                            Log.w(TAG, "update_check_unavailable");
                            return;
                        }
                        try {
                            Thread.sleep(attempt == 1 ? 1500L : 4000L);
                        } catch (InterruptedException interrupted) {
                            Thread.currentThread().interrupt();
                            return;
                        }
                    }
                }
            } finally {
                checking.set(false);
            }
        });
    }

    void downloadAndInstall(AvailableUpdate update) {
        if (!activity.getPackageManager().canRequestPackageInstalls()) {
            pendingInstall = update;
            Log.i(TAG, "install_permission_required versionCode=" + update.versionCode);
            Intent settings = new Intent(
                    Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                    Uri.parse("package:" + activity.getPackageName())
            );
            activity.startActivity(settings);
            listener.onMessage("Разрешите установку обновлений для Projects Hub.");
            return;
        }

        pendingInstall = null;
        Log.i(TAG, "update_download_start versionCode=" + update.versionCode);
        listener.onMessage("Скачиваю обновление…");
        executor.execute(() -> {
            try {
                File apk = download(update);
                Log.i(TAG, "update_verified versionCode=" + update.versionCode);
                activity.runOnUiThread(() -> {
                    listener.onMessage("Обновление готово. Подтвердите установку Android.");
                    Log.i(TAG, "installer_launch versionCode=" + update.versionCode);
                    launchInstaller(apk);
                });
            } catch (Exception failure) {
                Log.w(TAG, "update_download_failed error=" + failure.getClass().getSimpleName());
                activity.runOnUiThread(() ->
                        listener.onFailure("Не удалось скачать или проверить обновление.")
                );
            }
        });
    }

    void resumePendingInstall() {
        AvailableUpdate update = pendingInstall;
        if (update != null && activity.getPackageManager().canRequestPackageInstalls()) {
            downloadAndInstall(update);
        }
    }

    private AvailableUpdate fetchLatestUpdate() throws Exception {
        String releaseUrl = "https://api.github.com/repos/"
                + BuildConfig.UPDATE_REPOSITORY
                + "/releases/latest";
        JSONObject release = new JSONObject(fetchText(releaseUrl, 2 * 1024 * 1024));
        JSONArray assets = release.getJSONArray("assets");
        String manifestUrl = null;
        for (int index = 0; index < assets.length(); index++) {
            JSONObject asset = assets.getJSONObject(index);
            if ("update.json".equals(asset.optString("name"))) {
                manifestUrl = asset.getString("browser_download_url");
                break;
            }
        }
        if (manifestUrl == null) return null;

        JSONObject manifest = new JSONObject(fetchText(manifestUrl, 256 * 1024));
        return new AvailableUpdate(
                manifest.getInt("versionCode"),
                manifest.optString("versionName", ""),
                manifest.getString("apkUrl"),
                manifest.getString("sha256")
        );
    }

    private File download(AvailableUpdate update) throws Exception {
        File directory = new File(activity.getCacheDir(), "updates");
        if (!directory.exists() && !directory.mkdirs()) {
            throw new IllegalStateException("Cannot create update directory");
        }
        File target = new File(directory, "projects-hub-" + update.versionCode + ".apk");
        File partial = new File(directory, target.getName() + ".part");

        HttpURLConnection connection = open(update.apkUrl);
        int status = connection.getResponseCode();
        if (status < 200 || status >= 300) {
            connection.disconnect();
            throw new IllegalStateException("APK download failed");
        }

        MessageDigest digest = MessageDigest.getInstance("SHA-256");
        long total = 0L;
        try (InputStream input = connection.getInputStream();
             FileOutputStream output = new FileOutputStream(partial)) {
            byte[] buffer = new byte[64 * 1024];
            int count;
            while ((count = input.read(buffer)) >= 0) {
                total += count;
                if (total > 256L * 1024L * 1024L) {
                    throw new IllegalStateException("APK is unexpectedly large");
                }
                output.write(buffer, 0, count);
                digest.update(buffer, 0, count);
            }
            output.getFD().sync();
        } finally {
            connection.disconnect();
        }

        String actual = toHex(digest.digest());
        if (!actual.equalsIgnoreCase(update.sha256)) {
            partial.delete();
            throw new SecurityException("APK digest mismatch");
        }

        if (target.exists() && !target.delete()) {
            throw new IllegalStateException("Cannot replace previous update");
        }
        if (!partial.renameTo(target)) {
            throw new IllegalStateException("Cannot finalize update download");
        }
        return target;
    }

    private void launchInstaller(File apk) {
        Uri content = FileProvider.getUriForFile(
                activity,
                activity.getPackageName() + ".updates",
                apk
        );
        Intent install = new Intent(Intent.ACTION_VIEW)
                .setDataAndType(content, "application/vnd.android.package-archive")
                .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
        activity.startActivity(install);
    }

    private static String fetchText(String url, int maxBytes) throws Exception {
        HttpURLConnection connection = open(url);
        int status = connection.getResponseCode();
        if (status < 200 || status >= 300) {
            connection.disconnect();
            throw new IllegalStateException("HTTP " + status);
        }

        StringBuilder result = new StringBuilder();
        try (BufferedReader reader = new BufferedReader(
                new InputStreamReader(connection.getInputStream(), StandardCharsets.UTF_8)
        )) {
            char[] buffer = new char[4096];
            int count;
            while ((count = reader.read(buffer)) >= 0) {
                result.append(buffer, 0, count);
                if (result.length() > maxBytes) {
                    throw new IllegalStateException("Response is too large");
                }
            }
        } finally {
            connection.disconnect();
        }
        return result.toString();
    }

    private static HttpURLConnection open(String value) throws Exception {
        HttpURLConnection connection = (HttpURLConnection) new URL(value).openConnection();
        connection.setConnectTimeout(10000);
        connection.setReadTimeout(30000);
        connection.setRequestProperty("Accept", "application/vnd.github+json, application/json");
        connection.setRequestProperty("User-Agent", "ProjectsHubAndroid/" + BuildConfig.VERSION_NAME);
        return connection;
    }

    private static String toHex(byte[] bytes) {
        StringBuilder output = new StringBuilder(bytes.length * 2);
        for (byte value : bytes) {
            output.append(String.format(Locale.ROOT, "%02x", value));
        }
        return output.toString();
    }
}
