package com.kenigevents.projectshub;

import android.content.Context;
import android.content.SharedPreferences;
import android.os.Handler;
import android.os.Looper;
import android.webkit.CookieManager;

import org.json.JSONArray;
import org.json.JSONObject;
import org.onedayonemasterpiece.live.VoiceAudioV2;
import org.onedayonemasterpiece.live.platform.SessionApi;
import org.onedayonemasterpiece.live.platform.StartContext;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.util.Map;
import java.util.TimeZone;
import java.util.UUID;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CompletionException;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.Executor;
import java.util.concurrent.RejectedExecutionException;
import java.util.concurrent.ThreadPoolExecutor;
import java.util.concurrent.TimeUnit;

/** Product authentication/routes. The shared SDK receives only an opaque same-origin socket binding. */
final class NativeSessionApi implements SessionApi, AutoCloseable {
    private static final String SESSION_COOKIE = "projects_hub_session";
    // A late Start response must still be closed after this UI/session instance
    // has stopped. This bounded cleanup pool owns no audio and no new Start.
    private static final ExecutorService STOP_IO = new ThreadPoolExecutor(0, 2, 30, TimeUnit.SECONDS,
            new ArrayBlockingQueue<>(16), task -> {
                Thread thread = new Thread(task, "native-voice-session-cleanup");
                thread.setDaemon(true); return thread;
            });
    private final ApiClient api = new ApiClient(BuildConfig.HUB_URL);
    private final Handler main = new Handler(Looper.getMainLooper());
    private final SharedPreferences preferences;
    private final ExecutorService io = Executors.newFixedThreadPool(2);
    private final String conversationId, clientInstanceId;
    private volatile String cookie;
    private volatile String sessionId;
    private volatile NativeVoiceStore.Scope scope;
    private volatile boolean active = true;
    private final NativeStartAttempt startAttempt = new NativeStartAttempt();

    private final ApiClient.SessionCookies cookies = new ApiClient.SessionCookies() {
        @Override public String current() { return cookie; }
        @Override public void received(String value) {
            if (!active || value == null || value.length() > 8192 || value.contains("\r") || value.contains("\n")) return;
            String pair = value.split(";", 2)[0];
            if (!pair.startsWith(SESSION_COOKIE + "=")) return;
            cookie = pair;
            main.post(() -> {
                if (!active) return;
                CookieManager.getInstance().setCookie(BuildConfig.HUB_URL, value);
                CookieManager.getInstance().flush();
            });
        }
    };

    NativeSessionApi(Context context, String conversationId, String cookieHeader) {
        this.conversationId = VoiceAudioV2.id(conversationId);
        cookie = sessionCookie(cookieHeader);
        preferences = context.getSharedPreferences("native_voice_scope_v2", Context.MODE_PRIVATE);
        String existing = preferences.getString("client_instance_id", null);
        if (existing == null) {
            existing = "android_" + UUID.randomUUID().toString().replace("-", "");
            if (!preferences.edit().putString("client_instance_id", existing).commit())
                throw new IllegalStateException("NATIVE_VOICE_STORAGE_UNAVAILABLE");
        }
        clientInstanceId = existing;
    }

    /**
     * Cached scope permits private local recording during an outage only for
     * the same previously authenticated cookie and conversation. Every server
     * request still reauthorizes; a cached scope never grants remote access.
     */
    CompletableFuture<NativeVoiceStore.Scope> authorize() {
        return async(() -> {
            String key = "scope_" + VoiceAudioV2.sha256(conversationId.getBytes(StandardCharsets.UTF_8));
            try {
                JSONObject bootstrap = api.sessionRequest("GET", "api/bootstrap", null, cookies, 15000);
                JSONObject conversation = api.sessionRequest("GET", "api/conversations/" + conversationId,
                        null, cookies, 15000);
                String actor = bootstrap.getJSONObject("actor").getString("id");
                if (!conversationId.equals(conversation.getString("id"))
                        || !actor.equals(conversation.getString("actor_id")))
                    throw new IllegalStateException("NATIVE_VOICE_SCOPE_MISMATCH");
                synchronized (this) {
                    if (!active) throw new IllegalStateException("NATIVE_VOICE_START_CANCELLED");
                    scope = new NativeVoiceStore.Scope(actor, conversation.getString("workspace_id"), conversationId);
                    JSONObject cached = new JSONObject().put("actor", scope.actorId()).put("workspace", scope.workspaceId())
                            .put("conversation", conversationId).put("cookie_hash", cookieFingerprint());
                    preferences.edit().putString(key, cached.toString()).commit();
                    return scope;
                }
            } catch (Exception failure) {
                if (failure instanceof ApiClient.ApiException http && http.statusCode < 500) {
                    if (http.statusCode == 401 || http.statusCode == 403 || http.statusCode == 404)
                        preferences.edit().remove(key).commit();
                    throw failure;
                }
                // Only network/server availability failure qualifies for an already-bound local scope.
                if (!(failure instanceof IOException)) throw failure;
                synchronized (this) {
                    if (!active) throw new IllegalStateException("NATIVE_VOICE_START_CANCELLED");
                    String text = preferences.getString(key, null);
                    if (text != null) {
                        JSONObject cached = new JSONObject(text);
                        if (conversationId.equals(cached.optString("conversation"))
                                && cookieFingerprint().equals(cached.optString("cookie_hash"))) {
                            scope = new NativeVoiceStore.Scope(cached.getString("actor"), cached.getString("workspace"), conversationId);
                            return scope;
                        }
                    }
                }
                throw failure;
            }
        });
    }

    @Override public CompletableFuture<Connection> connect(StartContext context, String previousSessionId,
            long eventCursor, int connectionGeneration, boolean recoveryOnly) {
        return async(() -> {
            try {
            if (!active) throw new IOException("NATIVE_VOICE_START_CANCELLED");
            if (scope == null || !conversationId.equals(context.conversationId()))
                throw new IOException("NATIVE_VOICE_SCOPE_REQUIRED");
            JSONObject reply;
            String requestedSession = previousSessionId;
            if (requestedSession != null) {
                VoiceAudioV2.id(requestedSession);
                String observedAttempt = startAttempt.current();
                try {
                    reply = api.sessionRequest("POST", sessionPath(requestedSession) + "/socket-ticket",
                            new JSONObject(), cookies, 15000);
                } catch (ApiClient.ApiException failure) {
                    if (!"LIVE_SESSION_CLOSED".equals(failure.code) && !"LIVE_SESSION_NOT_FOUND".equals(failure.code))
                        throw failure;
                    startAttempt.afterTerminal(observedAttempt, failure.code);
                    // Closed server identity is terminal to this user-owned
                    // Start. Never silently start a new paid provider session
                    // after archive/transport failure. Preserve local outbox.
                    throw classifiedConnectFailure(failure);
                }
            } else reply = startSession(recoveryOnly);
            if (requestedSession == null) requestedSession = reply.getString("session_id");
            VoiceAudioV2.id(requestedSession);
            sessionId = requestedSession;
            if (!supportsV2(reply)) {
                stop(requestedSession, "protocol_unsupported");
                throw new IOException("NATIVE_VOICE_PROTOCOL_UNAVAILABLE");
            }
            String socketPath = reply.getString("socket_url");
            String expectedPath = "/" + sessionPath(requestedSession) + "/socket";
            if (!expectedPath.equals(socketPath)) {
                stop(requestedSession, "socket_origin_rejected");
                throw new IOException("NATIVE_VOICE_SOCKET_ORIGIN");
            }
            return new Connection(BuildConfig.HUB_URL, socketPath, requestedSession,
                    reply.getString("attempt_id"), reply.getString("socket_ticket"),
                    connectionGeneration, recoveryOnly, Map.of("Cookie", cookie));
            } catch (ApiClient.ApiException failure) {
                throw classifiedConnectFailure(failure);
            }
        });
    }

    static SessionApi.ConnectFailure classifiedConnectFailure(ApiClient.ApiException failure) {
        String code = failure.code;
        // Never turn a definite quota/auth/closed-session refusal into an
        // unlimited new provider Start. Ordinary 5xx transport faults remain
        // reconnectable with the shared SDK's existing bounded backoff.
        boolean terminal = (code.startsWith("RESOURCE_")
                && !"RESOURCE_CONTROL_UNAVAILABLE".equals(code))
                || "LIVE_SESSION_CLOSED".equals(code)
                || "LIVE_SESSION_NOT_FOUND".equals(code)
                || "LIVE_ATTEMPT_CLOSED".equals(code)
                || failure.statusCode == 401 || failure.statusCode == 403
                || failure.statusCode == 404 || failure.statusCode == 429;
        return new SessionApi.ConnectFailure(code, !terminal);
    }

    private JSONObject startSession(boolean recoveryOnly) throws Exception {
        if (!active) throw new IOException("NATIVE_VOICE_START_CANCELLED");
        String attemptId = startAttempt.current();
        try {
            return postStart(recoveryOnly, attemptId);
        } catch (ApiClient.ApiException failure) {
            if (!"LIVE_ATTEMPT_CLOSED".equals(failure.code)) throw failure;
            // A lost initial POST may have created a session which the server
            // later closed. Only that explicit tombstone permits a fresh key.
            // Retry once here; an uncertain response retains the new key.
            return postStart(recoveryOnly, startAttempt.afterTerminal(attemptId, failure.code));
        }
    }

    private JSONObject postStart(boolean recoveryOnly, String attemptId) throws Exception {
        if (!active) throw new IOException("NATIVE_VOICE_START_CANCELLED");
        return api.sessionRequest("POST", "api/live/" + conversationId + "/sessions",
                startRequest(recoveryOnly, BuildConfig.VERSION_NAME, clientInstanceId, attemptId,
                        TimeZone.getDefault().getID()), cookies, 45000);
    }

    /** The actual HTTP payload producer; tested against the server LiveStart schema. */
    static JSONObject startRequest(boolean recoveryOnly, String clientVersion, String instanceId,
                                   String attemptId, String zone) throws org.json.JSONException {
        JSONObject body = new JSONObject().put("transport", "wss")
                .put("audio_mode", recoveryOnly ? "buffered" : "realtime")
                .put("recovery_only", recoveryOnly).put("client_version", clientVersion)
                .put("client_instance_id", instanceId).put("attempt_id", attemptId);
        if (zone != null && zone.matches("[A-Za-z0-9._+/-]{1,100}")) body.put("client_timezone", zone);
        return body;
    }

    @Override public CompletableFuture<Void> stop(String session, String reason) {
        return async(() -> {
            VoiceAudioV2.id(session);
            api.sessionRequest("POST", sessionPath(session) + "/stop",
                    new JSONObject().put("reason", reason), cookies, 5000);
            if (session.equals(sessionId)) sessionId = null;
            return null;
        }, STOP_IO);
    }

    String sessionId() { return sessionId; }

    synchronized void clearCachedScope() {
        active = false;
        startAttempt.cancel();
        preferences.edit().remove("scope_" + VoiceAudioV2.sha256(conversationId.getBytes(StandardCharsets.UTF_8))).commit();
    }

    private String sessionPath(String session) { return "api/live/" + conversationId + "/sessions/" + session; }
    private String cookieFingerprint() { return VoiceAudioV2.sha256(cookie.getBytes(StandardCharsets.UTF_8)); }
    private static boolean supportsV2(JSONObject reply) {
        if (VoiceAudioV2.PROTOCOL.equals(reply.optString("transport_protocol"))) return true;
        JSONArray protocols = reply.optJSONArray("transport_protocols");
        if (protocols != null) for (int i = 0; i < protocols.length(); i++)
            if (VoiceAudioV2.PROTOCOL.equals(protocols.optString(i))) return true;
        return false;
    }
    private static String sessionCookie(String header) {
        if (header != null && header.length() <= 16384 && !header.contains("\r") && !header.contains("\n")) {
            for (String item : header.split(";")) {
                String pair = item.trim();
                if (pair.startsWith(SESSION_COOKIE + "=") && pair.length() > SESSION_COOKIE.length() + 1)
                    return pair;
            }
        }
        throw new IllegalArgumentException("NATIVE_VOICE_AUTH_REQUIRED");
    }
    @FunctionalInterface private interface Request<T> { T run() throws Exception; }
    private <T> CompletableFuture<T> async(Request<T> request) {
        return async(request, io);
    }
    private <T> CompletableFuture<T> async(Request<T> request, Executor executor) {
        try { return CompletableFuture.supplyAsync(() -> {
            try { return request.run(); }
            catch (ApiClient.ApiException failure) { throw new CompletionException(new IOException(failure.code)); }
            catch (Exception failure) { throw new CompletionException(failure); }
        }, executor); }
        catch (RejectedExecutionException unavailable) {
            CompletableFuture<T> rejected = new CompletableFuture<>();
            rejected.completeExceptionally(new IOException("NATIVE_VOICE_SESSION_BACKPRESSURE"));
            return rejected;
        }
    }
    synchronized void cancelPendingAuthentication() { active = false; startAttempt.cancel(); }
    @Override public synchronized void close() { active = false; startAttempt.cancel(); io.shutdown(); }
}
