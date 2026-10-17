package com.kenigevents.projectshub;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;

final class ApiClient {
    interface SessionCookies {
        String current();
        void received(String setCookie);
    }

    static final class ApiException extends IOException {
        final int statusCode;
        final String code;

        ApiException(int statusCode, String message) {
            this(statusCode, message, "HTTP_" + statusCode);
        }

        ApiException(int statusCode, String message, String code) {
            super(message);
            this.statusCode = statusCode;
            this.code = code;
        }
    }

    static final class ClaimedCommand {
        final String commandId;
        final String claimToken;
        final String capability;
        final String payloadSha256;
        final JSONObject payload;

        ClaimedCommand(
                String commandId,
                String claimToken,
                String capability,
                String payloadSha256,
                JSONObject payload
        ) {
            this.commandId = commandId;
            this.claimToken = claimToken;
            this.capability = capability;
            this.payloadSha256 = payloadSha256;
            this.payload = payload;
        }
    }

    private final String baseUrl;
    private final String origin;

    ApiClient(String baseUrl) {
        this.baseUrl = baseUrl.endsWith("/") ? baseUrl : baseUrl + "/";
        this.origin = this.baseUrl.substring(0, this.baseUrl.length() - 1);
    }

    String bootstrapWorkspace(String cookie) throws Exception {
        JSONObject payload = request("GET", "api/bootstrap", null, null, cookie, false, 15000);
        return payload.getJSONObject("workspace").getString("id");
    }

    JSONObject sessionRequest(String method, String path, JSONObject body, SessionCookies cookies,
                              int readTimeoutMs) throws Exception {
        if (!path.startsWith("api/") || path.contains("..") || path.contains("://"))
            throw new IllegalArgumentException("NATIVE_SESSION_ORIGIN");
        return request(method, path, body, null, cookies.current(), true, readTimeoutMs, cookies);
    }

    String registerAndroidDevice(String cookie, String workspaceId, String displayName) throws Exception {
        JSONObject body = new JSONObject()
                .put("workspace_id", workspaceId)
                .put("display_name", displayName)
                .put("platform", "android")
                .put(
                        "capabilities",
                        new JSONArray()
                                .put("calendar.create_event")
                                .put("calendar.read_events")
                );
        JSONObject payload = request(
                "POST",
                "api/devices/register",
                body,
                null,
                cookie,
                true,
                15000
        );
        return payload.getString("device_token");
    }

    JSONObject updateDeviceCapabilities(String deviceToken) throws Exception {
        JSONObject body = new JSONObject().put(
                "capabilities",
                new JSONArray()
                        .put("calendar.create_event")
                        .put("calendar.read_events")
        );
        return request(
                "POST",
                "api/device/capabilities",
                body,
                "Device " + deviceToken,
                null,
                true,
                15000
        );
    }

    JSONArray recentDevelopmentCompletions(String deviceToken) throws Exception {
        JSONObject response = request(
                "GET", "api/device/development/completed",
                null, "Device " + deviceToken, null, false, 15000
        );
        return response.optJSONArray("items") == null
                ? new JSONArray() : response.getJSONArray("items");
    }

    ClaimedCommand nextCommand(String deviceToken, int waitMs) throws Exception {
        JSONObject payload = request(
                "GET",
                "api/device/commands/next?wait_ms=" + Math.max(0, waitMs),
                null,
                "Device " + deviceToken,
                null,
                false,
                Math.max(15000, waitMs + 10000)
        );
        if (payload.isNull("command")) return null;
        JSONObject command = payload.getJSONObject("command");
        return new ClaimedCommand(
                command.getString("command_id"),
                payload.getString("claim_token"),
                command.getString("capability"),
                command.getString("payload_sha256"),
                command.getJSONObject("payload")
        );
    }

    JSONObject submitReceipt(
            String deviceToken,
            ClaimedCommand command,
            String status,
            JSONObject result
    ) throws Exception {
        JSONObject body = new JSONObject()
                .put("claim_token", command.claimToken)
                .put("payload_sha256", command.payloadSha256)
                .put("status", status)
                .put("result", result);
        return request(
                "POST",
                "api/device/commands/" + command.commandId + "/receipt",
                body,
                "Device " + deviceToken,
                null,
                false,
                15000
        );
    }

    private JSONObject request(
            String method,
            String path,
            JSONObject body,
            String authorization,
            String cookie,
            boolean sameOriginMutation,
            int readTimeoutMs
    ) throws Exception {
        return request(method, path, body, authorization, cookie, sameOriginMutation, readTimeoutMs, null);
    }

    private JSONObject request(String method, String path, JSONObject body, String authorization,
                               String cookie, boolean sameOriginMutation, int readTimeoutMs,
                               SessionCookies sessionCookies) throws Exception {
        HttpURLConnection connection = (HttpURLConnection) new URL(baseUrl + path).openConnection();
        try {
            connection.setInstanceFollowRedirects(false);
            connection.setRequestMethod(method);
            connection.setConnectTimeout(10000);
            connection.setReadTimeout(readTimeoutMs);
            connection.setRequestProperty("Accept", "application/json");
            connection.setRequestProperty("User-Agent", "ProjectsHubAndroid/" + BuildConfig.VERSION_NAME);
            if (authorization != null) connection.setRequestProperty("Authorization", authorization);
            if (cookie != null && !cookie.isBlank()) connection.setRequestProperty("Cookie", cookie);
            if (sameOriginMutation) connection.setRequestProperty("Origin", origin);

            if (body != null) {
                byte[] bytes = body.toString().getBytes(StandardCharsets.UTF_8);
                connection.setDoOutput(true);
                connection.setRequestProperty("Content-Type", "application/json");
                connection.setFixedLengthStreamingMode(bytes.length);
                try (OutputStream output = connection.getOutputStream()) {
                    output.write(bytes);
                }
            }

            int status = connection.getResponseCode();
            if (sessionCookies != null) {
                for (var header : connection.getHeaderFields().entrySet()) {
                    if (header.getKey() != null && header.getKey().equalsIgnoreCase("Set-Cookie")) {
                        for (String value : header.getValue()) sessionCookies.received(value);
                    }
                }
            }
            InputStream stream = status >= 200 && status < 300
                    ? connection.getInputStream()
                    : connection.getErrorStream();
            String text = readText(stream);

            if (status < 200 || status >= 300) {
                String message = "HTTP " + status;
                String code = "HTTP_" + status;
                try {
                    JSONObject error = new JSONObject(text);
                    Object detail = error.opt("error");
                    if (detail instanceof JSONObject) {
                        message = ((JSONObject) detail).optString("message", message);
                        code = ((JSONObject) detail).optString("code", code);
                    } else {
                        Object fallback = error.opt("detail");
                        if (fallback instanceof JSONObject) {
                            message = ((JSONObject) fallback).optString("message", message);
                            code = ((JSONObject) fallback).optString("code", code);
                        }
                    }
                } catch (Exception ignored) {
                    // Keep the bounded HTTP status; never surface cookies/tokens.
                }
                if (!code.matches("[A-Z0-9_]{1,96}")) code = "HTTP_" + status;
                throw new ApiException(status, message, code);
            }
            return text.isBlank() ? new JSONObject() : new JSONObject(text);
        } finally { connection.disconnect(); }
    }

    private static String readText(InputStream stream) throws IOException {
        if (stream == null) return "";
        StringBuilder result = new StringBuilder();
        try (BufferedReader reader = new BufferedReader(
                new InputStreamReader(stream, StandardCharsets.UTF_8)
        )) {
            char[] buffer = new char[4096];
            int count;
            while ((count = reader.read(buffer)) >= 0) {
                result.append(buffer, 0, count);
                if (result.length() > 1024 * 1024) {
                    throw new IOException("Response is too large");
                }
            }
        }
        return result.toString();
    }
}
