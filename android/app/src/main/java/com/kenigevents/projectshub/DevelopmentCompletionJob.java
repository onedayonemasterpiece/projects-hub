package com.kenigevents.projectshub;

import android.app.job.JobInfo;
import android.app.job.JobParameters;
import android.app.job.JobScheduler;
import android.app.job.JobService;
import android.content.ComponentName;
import android.content.Context;
import android.content.SharedPreferences;
import android.util.Log;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.HashSet;
import java.util.Set;

/**
 * Battery-friendly completion check using the existing paired-device credential.
 *
 * No permanent socket, Firebase account or second worker service. Android owns
 * scheduling even after the Activity closes; delayed jobs are not realtime FCM.
 */
public final class DevelopmentCompletionJob extends JobService {
    private static final int JOB_ID = 1986;
    private static final long PERIOD_MS = 15 * 60 * 1000L;
    private static final String PREFS = "projects_hub_development_notices";

    static void schedule(Context context) {
        JobScheduler scheduler = context.getSystemService(JobScheduler.class);
        if (scheduler == null) return;
        JobInfo info = new JobInfo.Builder(
                JOB_ID,
                new ComponentName(context, DevelopmentCompletionJob.class))
                .setRequiredNetworkType(JobInfo.NETWORK_TYPE_ANY)
                .setPersisted(true)
                .setPeriodic(PERIOD_MS)
                .build();
        scheduler.schedule(info);
    }

    @Override
    public boolean onStartJob(JobParameters params) {
        new Thread(() -> {
            try {
                checkAndNotify(this);
            } catch (Exception failure) {
                Log.w("ProjectsHubDelivery", "Completion check will retry", failure);
            } finally {
                jobFinished(params, false);
            }
        }, "projects-hub-delivery-check").start();
        return true;
    }

    @Override
    public boolean onStopJob(JobParameters params) {
        // Read-only request; retry when Android next grants a background window.
        return true;
    }

    static void checkAndNotify(Context context) throws Exception {
        String token = new SecureStore(context).getDeviceToken();
        if (token == null || token.isBlank()) return;
        JSONArray recent = new ApiClient(BuildConfig.HUB_URL)
                .recentDevelopmentCompletions(token);
        SharedPreferences prefs = context.getSharedPreferences(PREFS, MODE_PRIVATE);
        String boundDevice = token.substring(0, token.indexOf('.'));
        String oldDevice = prefs.getString("device_id", "");
        Set<String> seen = new HashSet<>(
                prefs.getStringSet("seen_ids", new HashSet<>()));
        // Do not replay another actor's notification history after account/device reset.
        if (!boundDevice.equals(oldDevice)) seen.clear();

        boolean firstSync = !boundDevice.equals(oldDevice) || !prefs.getBoolean("initialized", false);
        Notifier notifier = new Notifier(context);
        int delivered = 0;
        for (int i = recent.length() - 1; i >= 0; i--) {
            JSONObject execution = recent.optJSONObject(i);
            if (execution == null) continue;
            String id = execution.optString("id", "");
            if (!id.startsWith("devrun_") || seen.contains(id)) continue;
            JSONArray titles = execution.optJSONArray("titles");
            String title = titles != null && titles.length() > 0
                    ? titles.optString(0, "Разработка")
                    : "Разработка";
            if (!firstSync && delivered < 3) {
                notifier.developmentCompleted(id, title);
                delivered++;
            }
            seen.add(id);
        }

        // A seven-day rolling history is at most 20 records; keep a bounded dedup set.
        Set<String> current = new HashSet<>();
        for (int i = 0; i < recent.length(); i++) {
            JSONObject execution = recent.optJSONObject(i);
            if (execution != null) current.add(execution.optString("id", ""));
        }
        seen.retainAll(current);
        prefs.edit().putString("device_id", boundDevice)
                .putBoolean("initialized", true).putStringSet("seen_ids", seen).apply();
    }
}
