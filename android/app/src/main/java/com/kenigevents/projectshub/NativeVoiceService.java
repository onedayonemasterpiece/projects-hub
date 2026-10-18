package com.kenigevents.projectshub;

import android.Manifest;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.content.pm.ServiceInfo;
import android.os.Binder;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;

import org.json.JSONObject;
import org.onedayonemasterpiece.live.VoiceAudioV2;
import org.onedayonemasterpiece.live.platform.NativeLiveRuntime;
import org.onedayonemasterpiece.live.platform.StartContext;

import java.io.File;
import java.util.List;
import java.util.UUID;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CompletionException;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicBoolean;

/** One user-started native owner. Binding/rebinding a WebView never starts audio. */
public final class NativeVoiceService extends Service {
    static final String ACTION_ACTIVATE = "com.kenigevents.projectshub.voice.ACTIVATE";
    static final String ACTION_STOP = "com.kenigevents.projectshub.voice.STOP";
    private static final String CHANNEL = "projects_hub_voice";
    private static final int NOTIFICATION_ID = 1401;
    private static final NativeVoiceStore.Limits STORE_LIMITS = new NativeVoiceStore.Limits(
            128L * 1024 * 1024, 4096, 120L * 60 * 1_000_000);

    interface Listener {
        void onState(JSONObject state);
        void onEvent(String eventJson);
    }
    final class LocalBinder extends Binder { NativeVoiceService service() { return NativeVoiceService.this; } }

    private final Handler main = new Handler(Looper.getMainLooper());
    private final LocalBinder binder = new LocalBinder();
    private final NativeVoiceOwner owner = new NativeVoiceOwner();
    private final CopyOnWriteArrayList<Listener> listeners = new CopyOnWriteArrayList<>();
    private final ExecutorService storageIo = Executors.newSingleThreadExecutor();
    private final String ownerId = "voice_" + UUID.randomUUID().toString().replace("-", "");
    private final AtomicBoolean queueReadPending = new AtomicBoolean();
    private final CompletableFuture<Void> readiness = new CompletableFuture<>();
    private NativeLiveRuntime.Capabilities capabilities;
    private NativeLiveRuntime runtime;
    private NativeVoiceStore store;
    private NativeSessionApi sessionApi;
    private NativeLiveRuntime.Snapshot runtimeState;
    private CompletableFuture<Void> stopping = CompletableFuture.completedFuture(null);
    private String status = "idle", code, conversationId, sessionId, sourceId, localSourceId, notificationStatus;
    private String lastPublishedState;
    private boolean capturing, playing, foreground, initializingStore, destroyed;
    private long revision, queuedAudioMs;

    @Override public void onCreate() {
        super.onCreate();
        getSystemService(NotificationManager.class).createNotificationChannel(new NotificationChannel(
                CHANNEL, "Голосовой разговор", NotificationManager.IMPORTANCE_LOW));
        // MediaCodec configure/encode is a real probe; never run it on the Activity/service UI thread.
        storageIo.execute(() -> {
            NativeLiveRuntime.Capabilities checked;
            try { checked = NativeLiveRuntime.capabilities(this); }
            catch (RuntimeException failure) {
                checked = new NativeLiveRuntime.Capabilities(false, List.of(), List.of(), "NATIVE_VOICE_CODEC_UNAVAILABLE");
            }
            NativeLiveRuntime.Capabilities actual = checked;
            main.post(() -> {
                capabilities = actual;
                if (!actual.supported()) { status = "unsupported"; code = actual.errorCode(); }
                readiness.complete(null);
                if (!destroyed) publish();
            });
        });
    }

    @Override public IBinder onBind(Intent intent) { return binder; }

    @Override public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent != null && ACTION_STOP.equals(intent.getAction())) stopVoice("user");
        else if (intent != null && ACTION_ACTIVATE.equals(intent.getAction())) {
            // The Activity starts this only from its visible, permission-granted Start path.
            if (owner.current() != null) ensureForeground();
            else stopSelf(startId);
        }
        return START_NOT_STICKY; // reboot/process restart must never turn the microphone on
    }

    void addListener(Listener listener) { listeners.addIfAbsent(listener); }
    void removeListener(Listener listener) { listeners.remove(listener); }
    boolean ready() { return readiness.isDone(); }
    void whenReady(Runnable action) { readiness.thenRun(() -> main.post(action)); }

    JSONObject capabilitiesJson() {
        if (!ready()) throw new IllegalStateException("NATIVE_VOICE_CAPABILITIES_PENDING");
        return json(out -> out.put("protocol", VoiceAudioV2.PROTOCOL)
                .put("native_capture", capabilities.supported())
                .put("compressed_input", capabilities.supported())
                .put("compressed_output", capabilities.supported()));
    }

    JSONObject stateJson() {
        return json(out -> out.put("owner_id", ownerId).put("revision", revision).put("status", status)
                .put("capturing", capturing).put("playing", playing)
                .put("conversation_id", value(conversationId)).put("session_id", value(sessionId))
                .put("source_id", value(sourceId)).put("local_source_id", value(localSourceId))
                .put("queued_audio_ms", queuedAudioMs).put("code", value(code)));
    }

    void startVoice(String conversation, String cookie) {
        VoiceAudioV2.id(conversation);
        if (destroyed) throw new IllegalStateException("NATIVE_VOICE_SERVICE_CLOSED");
        if (!ready() || !capabilities.supported()) throw new IllegalStateException("NATIVE_VOICE_UNSUPPORTED");
        if (owner.current() != null) {
            if (owner.current().conversationId().equals(conversation)) return;
            throw new IllegalStateException("NATIVE_VOICE_ALREADY_ACTIVE");
        }
        if (!stopping.isDone() || initializingStore) throw new IllegalStateException("NATIVE_VOICE_STOPPING");
        if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED)
            throw new IllegalStateException("NATIVE_VOICE_MICROPHONE_REQUIRED");
        NativeVoiceOwner.Lease previous = owner.current();
        NativeVoiceOwner.Lease lease = owner.claim(conversation);
        if (lease.equals(previous)) return;
        conversationId = conversation;
        code = null; status = "starting"; queuedAudioMs = 0;
        try {
            ensureForeground();
            sessionApi = new NativeSessionApi(this, conversation, cookie);
        } catch (RuntimeException failure) {
            owner.stop();
            failStart(errorCode(failure));
            throw failure;
        }
        NativeSessionApi requestedApi = sessionApi;
        publish();
        requestedApi.authorize().whenComplete((scope, failure) -> main.post(() -> {
            if (!owner.owns(lease)) { requestedApi.close(); return; }
            if (failure != null) { failStart(errorCode(failure)); return; }
            initializingStore = true;
            CompletableFuture.supplyAsync(() -> {
                try { return new NativeVoiceStore(new File(getNoBackupFilesDir(), "voice-v2"), scope, STORE_LIMITS); }
                catch (Exception error) { throw new CompletionException(error); }
            }, storageIo).whenComplete((opened, storeFailure) -> main.post(() -> {
                initializingStore = false;
                if (!owner.owns(lease)) {
                    closeStore(opened);
                    finishStoppedIfIdle();
                    return;
                }
                if (storeFailure != null) { failStart(errorCode(storeFailure)); return; }
                store = opened;
                try {
                    runtime = new NativeLiveRuntime(this, opened, requestedApi, new NativeLiveRuntime.Listener() {
                    @Override public void onState(NativeLiveRuntime.Snapshot snapshot) {
                        main.post(() -> { if (owner.owns(lease)) projectState(snapshot); });
                    }
                    @Override public void onEvent(String eventJson) {
                        main.post(() -> {
                            if (!owner.owns(lease) || eventJson == null || eventJson.length() > 262144) return;
                            try {
                                JSONObject event = new JSONObject(eventJson);
                                if (event.has("audio") || event.has("audio_data")
                                        || "audio".equals(event.optString("type"))) return;
                                String activeSession = runtime == null ? sessionId : runtime.snapshot().sessionId();
                                event.put("conversation_id", lease.conversationId()).put("session_id", value(activeSession));
                                for (Listener listener : listeners) listener.onEvent(event.toString());
                            } catch (org.json.JSONException malformed) { stopVoiceWithError("NATIVE_VOICE_INVALID_EVENT"); }
                        });
                    }
                    @Override public void onError(String errorCode, boolean recoverable) {
                        main.post(() -> {
                            if (!owner.owns(lease)) return;
                            code = safeCode(errorCode);
                            if (!recoverable) stopVoiceWithError(code);
                            else publish();
                        });
                    }
                    @Override public void onDeviceDurable(VoiceAudioV2.DurablePrefix prefix) { refreshQueued(lease); }
                    @Override public void onServerDurable(VoiceAudioV2.DurablePrefix prefix) { refreshQueued(lease); }
                });
                    runtime.start(new StartContext(conversation)).whenComplete((snapshot, startFailure) -> main.post(() -> {
                        if (!owner.owns(lease)) return;
                        if (startFailure != null) stopVoiceWithError(errorCode(startFailure));
                        else projectState(snapshot);
                    }));
                } catch (RuntimeException failureToStart) { stopVoiceWithError(errorCode(failureToStart)); }
            }));
        }));
    }

    void rejectStart(String failure) {
        if (owner.current() != null) return;
        code = safeCode(failure); status = "error";
        publish();
    }

    void notePermissionRequest(String conversation) {
        if (owner.current() != null) return;
        conversationId = conversation; status = "starting"; code = null;
        publish();
    }

    void cancelPermissionRequest() {
        if (owner.current() == null && "starting".equals(status) && !initializingStore) {
            status = "idle"; code = null;
            publish();
        }
    }

    CompletableFuture<Void> stopVoice(String reason) {
        owner.stop(); // fence permission/auth/network completions before releasing hardware
        if ("user".equals(reason) || "logout".equals(reason) || "account_changed".equals(reason)) code = null;
        if ("logout".equals(reason) || "account_changed".equals(reason)) {
            conversationId = null; sourceId = null; localSourceId = null; queuedAudioMs = 0;
        }
        NativeLiveRuntime stoppedRuntime = runtime;
        NativeVoiceStore stoppedStore = store;
        NativeSessionApi stoppedApi = sessionApi;
        if (stoppedApi != null) {
            stoppedApi.cancelPendingAuthentication();
            if ("logout".equals(reason) || "account_changed".equals(reason)) stoppedApi.clearCachedScope();
        }
        if (stoppedRuntime == null && !stopping.isDone()) return stopping;
        status = "stopping";
        // SDK stop has a synchronous hardware-first portion; no network wait here.
        CompletableFuture<Void> localStop = stoppedRuntime == null ? CompletableFuture.completedFuture(null) : stoppedRuntime.stop(reason);
        CompletableFuture<Void> cleanup = new CompletableFuture<>();
        stopping = cleanup;
        runtime = null; store = null; sessionApi = null;
        capturing = false; playing = false; runtimeState = null;
        publish();
        localStop.whenComplete((ignored, failure) -> main.post(() -> {
            if (stoppedRuntime != null) stoppedRuntime.close();
            closeStore(stoppedStore);
            if (stoppedApi != null) stoppedApi.close();
            if (failure != null) code = errorCode(failure);
            cleanup.complete(null);
            finishStoppedIfIdle();
        }));
        return stopping;
    }

    void finishTurn() {
        if (runtime == null || !capturing) throw new IllegalStateException("NATIVE_VOICE_NOT_CAPTURING");
        runtime.finishTurn();
    }

    private void stopVoiceWithError(String failure) {
        code = failure;
        stopVoice("error");
    }

    private void failStart(String failure) {
        owner.stop();
        code = failure; status = "error"; capturing = false; playing = false;
        if (sessionApi != null) { sessionApi.close(); sessionApi = null; }
        closeStore(store); store = null;
        publish();
        removeForeground();
        stopSelf();
    }

    private void finishStoppedIfIdle() {
        if (owner.current() != null || initializingStore || !stopping.isDone()) return;
        status = code == null ? "idle" : "error";
        sessionId = null;
        publish();
        removeForeground();
        stopSelf();
    }

    private void projectState(NativeLiveRuntime.Snapshot snapshot) {
        if (snapshot == null) return;
        runtimeState = snapshot;
        status = switch (snapshot.state()) {
            case "live", "delayed", "recording_only", "starting", "stopping", "idle", "error", "unsupported" -> snapshot.state();
            default -> "error";
        };
        capturing = snapshot.capturing(); playing = snapshot.playing();
        conversationId = snapshot.conversationId(); sessionId = snapshot.sessionId();
        sourceId = snapshot.serverSourceId(); localSourceId = snapshot.sourceId();
        code = snapshot.errorCode() == null ? null : safeCode(snapshot.errorCode());
        publish();
    }

    private void refreshQueued(NativeVoiceOwner.Lease lease) {
        if (!queueReadPending.compareAndSet(false, true)) return;
        main.postDelayed(() -> {
            NativeVoiceStore currentStore = store;
            if (destroyed || currentStore == null || !owner.owns(lease)) { queueReadPending.set(false); return; }
            storageIo.execute(() -> {
                try {
                    long milliseconds = currentStore.pendingDurationUs() / 1000;
                    main.post(() -> { if (owner.owns(lease)) { queuedAudioMs = milliseconds; publish(); } });
                } catch (Exception failure) {
                    main.post(() -> { if (owner.owns(lease)) stopVoiceWithError(errorCode(failure)); });
                } finally { queueReadPending.set(false); }
            });
        }, 250);
    }

    private void ensureForeground() {
        if (foreground) return;
        Notification notification = notification();
        if (Build.VERSION.SDK_INT >= 30) startForeground(NOTIFICATION_ID, notification,
                ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE | ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK);
        else if (Build.VERSION.SDK_INT >= 29) startForeground(NOTIFICATION_ID, notification,
                ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK);
        else startForeground(NOTIFICATION_ID, notification);
        foreground = true;
        notificationStatus = status;
    }

    private void removeForeground() {
        if (foreground) { stopForeground(STOP_FOREGROUND_REMOVE); foreground = false; }
    }

    private Notification notification() {
        Intent open = new Intent(this, MainActivity.class).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        PendingIntent openIntent = PendingIntent.getActivity(this, NOTIFICATION_ID, open,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        PendingIntent stopIntent = PendingIntent.getService(this, NOTIFICATION_ID,
                new Intent(this, NativeVoiceService.class).setAction(ACTION_STOP),
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        String text = switch (status) {
            case "live" -> "Разговор идёт";
            case "delayed" -> "Соединение медленное — речь сохраняется";
            case "recording_only" -> "Запись на устройстве — соединение недоступно";
            case "stopping" -> "Завершаю сохранение записи";
            default -> "Подключаю голосовой разговор";
        };
        return new Notification.Builder(this, CHANNEL).setSmallIcon(android.R.drawable.ic_btn_speak_now)
                .setContentTitle("Разговор с Мирой").setContentText(text).setContentIntent(openIntent)
                .setCategory(Notification.CATEGORY_SERVICE).setOngoing(true).setOnlyAlertOnce(true)
                .addAction(new Notification.Action.Builder(null, "Стоп", stopIntent).build()).build();
    }

    private void publish() {
        // revision changes only here, so it still equals the last emitted
        // revision while comparing every public field. State reads/attach ACKs
        // remain available without a new publication.
        if (stateJson().toString().equals(lastPublishedState)) return;
        revision++;
        JSONObject state = stateJson();
        lastPublishedState = state.toString();
        for (Listener listener : listeners) listener.onState(state);
        if (foreground && !status.equals(notificationStatus)) {
            notificationStatus = status;
            getSystemService(NotificationManager.class).notify(NOTIFICATION_ID, notification());
        }
    }

    private void closeStore(NativeVoiceStore sourceStore) {
        if (sourceStore == null) return;
        try { sourceStore.close(); } catch (Exception failure) { code = "NATIVE_VOICE_STORAGE_CLOSE_FAILED"; }
    }

    static String errorCode(Throwable failure) {
        while (failure.getCause() != null && (failure instanceof CompletionException
                || failure instanceof java.util.concurrent.ExecutionException)) failure = failure.getCause();
        if (failure instanceof NativeVoiceStore.StoreException storeError) return storeError.code();
        return safeCode(failure.getMessage());
    }
    private static String safeCode(String value) {
        return value != null && value.matches("[A-Z0-9_]{1,96}") ? value : "NATIVE_VOICE_UNAVAILABLE";
    }
    private static Object value(String value) { return value == null ? JSONObject.NULL : value; }
    @FunctionalInterface private interface JsonWriter { void write(JSONObject out) throws Exception; }
    private static JSONObject json(JsonWriter writer) {
        JSONObject out = new JSONObject();
        try { writer.write(out); } catch (Exception impossible) { throw new IllegalStateException(impossible); }
        return out;
    }

    @Override public void onDestroy() {
        destroyed = true;
        stopVoice("service_destroyed");
        listeners.clear();
        storageIo.shutdown();
        super.onDestroy();
    }
}
