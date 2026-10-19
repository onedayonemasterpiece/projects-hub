package com.kenigevents.projectshub;

import android.app.Activity;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.ServiceConnection;
import android.net.Uri;
import android.os.IBinder;
import android.util.Log;
import android.webkit.WebView;

import androidx.webkit.JavaScriptReplyProxy;
import androidx.webkit.WebViewCompat;

import org.json.JSONObject;
import org.onedayonemasterpiece.live.VoiceAudioV2;

import java.util.ArrayDeque;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.Map;

/** Exact-origin, main-frame commands and presentation state; never audio or credentials. */
final class NativeVoiceBridge implements AutoCloseable {
    interface Host {
        boolean isForeground();
        String sessionCookie();
        void requestMicrophone(Runnable granted, Runnable denied);
        void cancelMicrophoneRequest();
        void activateForegroundService();
    }
    private record Cached(String fingerprint, String reply) {}
    private final Activity activity;
    private final WebView view;
    private final WebOriginPolicy policy;
    private final Host host;
    private final Map<String, Cached> requests = new LinkedHashMap<>();
    private final ArrayDeque<Runnable> waitingForService = new ArrayDeque<>();
    private NativeVoiceService service;
    private JavaScriptReplyProxy subscriber;
    private String pendingConversation;
    private long documentGeneration, intentGeneration;
    private boolean bound, closed;

    private final NativeVoiceService.Listener listener = new NativeVoiceService.Listener() {
        @Override public void onState(JSONObject state) { emit("state", "state", state); }
        @Override public void onEvent(String eventJson) {
            try { emit("event", "event", new JSONObject(eventJson)); }
            catch (org.json.JSONException ignored) { /* Service validates the shared JSON event. */ }
        }
    };
    private final ServiceConnection connection = new ServiceConnection() {
        @Override public void onServiceConnected(ComponentName name, IBinder binder) {
            if (closed) return;
            service = ((NativeVoiceService.LocalBinder) binder).service();
            service.addListener(listener);
            NativeVoiceService connected = service;
            connected.whenReady(() -> {
                if (closed || service != connected) return;
                while (!waitingForService.isEmpty()) waitingForService.removeFirst().run();
            });
        }
        @Override public void onServiceDisconnected(ComponentName name) {
            service = null;
            waitingForService.clear();
        }
    };

    NativeVoiceBridge(Activity activity, WebView view, WebOriginPolicy policy, Host host) {
        this.activity = activity; this.view = view; this.policy = policy; this.host = host;
        String origin = Uri.parse(BuildConfig.HUB_URL).buildUpon().path("").query(null).fragment(null).build().toString();
        WebViewCompat.addWebMessageListener(view, "projectsHubVoice", Collections.singleton(origin),
                (source, message, sourceOrigin, isMainFrame, reply) -> {
                    if (closed || source != view || !isMainFrame
                            || !policy.isTrustedPermissionOrigin(sourceOrigin.toString())
                            || !policy.isTrustedPageUrl(source.getUrl())) return;
                    String raw = message.getData();
                    if (raw == null || raw.length() > 1024) return;
                    final JSONObject input;
                    try {
                        input = new JSONObject(raw);
                        if (!(input.get("version") instanceof Integer) || input.getInt("version") != 1
                                || !(input.get("type") instanceof String) || !(input.get("request_id") instanceof String)
                                || !input.getString("request_id").matches("[A-Za-z0-9-]{1,64}")) return;
                        String type = input.getString("type");
                        boolean extra = "start".equals(type) || "stop".equals(type);
                        if (input.length() != (extra ? 4 : 3)) return;
                        if (!java.util.Set.of("attach", "state", "start", "stop", "finish_turn", "detach").contains(type)) return;
                        if ("start".equals(type)) {
                            if (!(input.get("conversation_id") instanceof String)) return;
                            VoiceAudioV2.id(input.getString("conversation_id"));
                        }
                        if ("stop".equals(type) && (!(input.get("reason") instanceof String)
                                || !java.util.Set.of("user", "logout", "account_changed").contains(input.getString("reason")))) return;
                    } catch (Exception malformed) { return; }
                    long document = documentGeneration;
                    Runnable command = () -> { if (!closed && document == documentGeneration) handle(input, reply); };
                    if (service != null && (service.ready() || "stop".equals(input.optString("type")))) command.run();
                    else if (bound && waitingForService.size() < 16) waitingForService.addLast(command);
                    else acknowledge(input, reply, false, "NATIVE_VOICE_SERVICE_UNAVAILABLE");
                });
        bound = activity.bindService(new Intent(activity, NativeVoiceService.class), connection, Context.BIND_AUTO_CREATE);
    }

    private void handle(JSONObject input, JavaScriptReplyProxy reply) {
        String type = input.optString("type"), requestId = input.optString("request_id");
        String fingerprint = type + "|" + input.optString("conversation_id") + "|" + input.optString("reason");
        Cached cached = requests.get(requestId);
        if (cached != null) {
            if (cached.fingerprint().equals(fingerprint)) reply.postMessage(cached.reply());
            else acknowledge(input, reply, false, "NATIVE_VOICE_REQUEST_ID_CONFLICT");
            return;
        }
        try {
            switch (type) {
                case "attach" -> subscriber = reply;
                case "state" -> { }
                case "detach" -> subscriber = null;
                case "start" -> {
                    if (!host.isForeground()) throw new IllegalStateException("NATIVE_VOICE_FOREGROUND_REQUIRED");
                    String conversation = input.getString("conversation_id");
                    if (pendingConversation != null) {
                        if (!pendingConversation.equals(conversation)) throw new IllegalStateException("NATIVE_VOICE_START_PENDING");
                        break;
                    }
                    pendingConversation = conversation;
                    long intent = ++intentGeneration, document = documentGeneration;
                    Log.i("ProjectsHubNativeVoice", "stage=bridge_start_request origin=trusted_page"
                            + " document_generation=" + document + " request_generation=" + intent
                            + " foreground=" + host.isForeground());
                    service.notePermissionRequest(conversation);
                    host.requestMicrophone(() -> {
                        if (closed || intent != intentGeneration || document != documentGeneration) return;
                        pendingConversation = null;
                        if (!host.isForeground() || !policy.isTrustedPageUrl(view.getUrl())) {
                            service.rejectStart("NATIVE_VOICE_FOREGROUND_REQUIRED");
                            return;
                        }
                        try {
                            Log.i("ProjectsHubNativeVoice", "stage=bridge_permission_granted origin=user_start"
                                    + " document_generation=" + document + " request_generation=" + intent);
                            host.activateForegroundService();
                            service.startVoice(conversation, host.sessionCookie());
                        } catch (RuntimeException failure) { service.rejectStart(NativeVoiceService.errorCode(failure)); }
                    }, () -> {
                        if (closed || intent != intentGeneration || document != documentGeneration) return;
                        pendingConversation = null;
                        service.rejectStart("NATIVE_VOICE_MICROPHONE_DENIED");
                    });
                }
                case "stop" -> {
                    Log.i("ProjectsHubNativeVoice", "stage=bridge_stop_request origin=trusted_page"
                            + " document_generation=" + documentGeneration
                            + " had_pending_start=" + (pendingConversation != null)
                            + " state=" + (service == null ? "detached" : service.stateJson().optString("status")));
                    cancelPendingStart();
                    service.stopVoice(input.getString("reason"));
                }
                case "finish_turn" -> service.finishTurn();
                default -> throw new IllegalStateException("NATIVE_VOICE_COMMAND_UNSUPPORTED");
            }
            acknowledge(input, reply, true, null);
        } catch (Exception failure) { acknowledge(input, reply, false, NativeVoiceService.errorCode(failure)); }
    }

    private void acknowledge(JSONObject input, JavaScriptReplyProxy reply, boolean ok, String code) {
        try {
            JSONObject response = new JSONObject().put("version", 1).put("type", "ack")
                    .put("request_id", input.getString("request_id")).put("ok", ok);
            if (code != null) response.put("code", code);
            if (service != null) {
                response.put("state", service.stateJson());
                if (service.ready()) response.put("capabilities", service.capabilitiesJson());
            }
            String encoded = response.toString();
            String fingerprint = input.optString("type") + "|" + input.optString("conversation_id") + "|" + input.optString("reason");
            requests.put(input.getString("request_id"), new Cached(fingerprint, encoded));
            while (requests.size() > 64) requests.remove(requests.keySet().iterator().next());
            reply.postMessage(encoded);
        } catch (Exception ignored) { /* A detached document cannot stop its native owner. */ }
    }

    private void emit(String type, String key, JSONObject value) {
        if (closed || subscriber == null || !policy.isTrustedPageUrl(view.getUrl())) return;
        try { subscriber.postMessage(new JSONObject().put("version", 1).put("type", type).put(key, value).toString()); }
        catch (Exception unavailableDocument) { subscriber = null; }
    }

    private void cancelPendingStart() {
        ++intentGeneration;
        if (pendingConversation != null && service != null) service.cancelPermissionRequest();
        pendingConversation = null;
        host.cancelMicrophoneRequest();
    }

    void onDocumentChanged() {
        ++documentGeneration;
        subscriber = null;
        requests.clear();
        waitingForService.clear();
        cancelPendingStart();
    }

    @Override public void close() {
        if (closed) return;
        closed = true;
        onDocumentChanged();
        if (service != null) service.removeListener(listener);
        if (bound) activity.unbindService(connection);
        bound = false;
        service = null;
        WebViewCompat.removeWebMessageListener(view, "projectsHubVoice");
    }
}
