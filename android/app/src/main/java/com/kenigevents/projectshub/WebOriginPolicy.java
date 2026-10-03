package com.kenigevents.projectshub;

import java.net.URI;
import java.util.Locale;

/** Exact-origin boundary for privileged WebView capabilities such as microphone access. */
final class WebOriginPolicy {
    private final String scheme;
    private final String host;
    private final int port;

    WebOriginPolicy(String trustedBaseUrl) {
        URI trusted = parse(trustedBaseUrl);
        if (!"https".equalsIgnoreCase(trusted.getScheme())
                || trusted.getHost() == null
                || trusted.getUserInfo() != null) {
            throw new IllegalArgumentException("Trusted WebView origin must be HTTPS");
        }
        this.scheme = trusted.getScheme().toLowerCase(Locale.ROOT);
        this.host = trusted.getHost().toLowerCase(Locale.ROOT);
        this.port = effectivePort(trusted);
    }

    boolean isTrustedPermissionOrigin(String value) {
        URI candidate = parseOrNull(value);
        if (candidate == null
                || candidate.getUserInfo() != null
                || candidate.getQuery() != null
                || candidate.getFragment() != null) {
            return false;
        }
        String path = candidate.getPath();
        if (path != null && !path.isEmpty() && !"/".equals(path)) {
            return false;
        }
        return sameOrigin(candidate);
    }

    boolean isTrustedPageUrl(String value) {
        URI candidate = parseOrNull(value);
        return candidate != null && candidate.getUserInfo() == null && sameOrigin(candidate);
    }

    private boolean sameOrigin(URI candidate) {
        return candidate.getScheme() != null
                && candidate.getHost() != null
                && scheme.equals(candidate.getScheme().toLowerCase(Locale.ROOT))
                && host.equals(candidate.getHost().toLowerCase(Locale.ROOT))
                && port == effectivePort(candidate);
    }

    private static int effectivePort(URI value) {
        if (value.getPort() >= 0) return value.getPort();
        if ("https".equalsIgnoreCase(value.getScheme())) return 443;
        if ("http".equalsIgnoreCase(value.getScheme())) return 80;
        return -1;
    }

    private static URI parse(String value) {
        URI result = parseOrNull(value);
        if (result == null) throw new IllegalArgumentException("Invalid URL");
        return result;
    }

    private static URI parseOrNull(String value) {
        try {
            return value == null ? null : URI.create(value.trim());
        } catch (IllegalArgumentException ignored) {
            return null;
        }
    }
}
