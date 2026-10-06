package com.kenigevents.projectshub;

import java.nio.charset.StandardCharsets;
import java.util.Base64;
import java.util.Locale;

final class PendingUpdateState {
    private static final String FORMAT = "v1";

    static final class Value {
        final int versionCode;
        final String versionName;
        final String apkUrl;
        final String sha256;

        Value(int versionCode, String versionName, String apkUrl, String sha256) {
            this.versionCode = versionCode;
            this.versionName = versionName;
            this.apkUrl = apkUrl;
            this.sha256 = sha256;
        }
    }

    private PendingUpdateState() {}

    static String encode(int versionCode, String versionName, String apkUrl, String sha256) {
        Value value = new Value(
                versionCode,
                versionName == null ? "" : versionName,
                apkUrl == null ? "" : apkUrl,
                sha256 == null ? "" : sha256.toLowerCase(Locale.ROOT)
        );
        if (!isStructurallyValid(value)) {
            throw new IllegalArgumentException("Invalid pending update metadata");
        }
        return String.join(
                "|",
                FORMAT,
                Integer.toString(value.versionCode),
                encodeText(value.versionName),
                encodeText(value.apkUrl),
                value.sha256
        );
    }

    static Value decode(String encoded) {
        if (encoded == null || encoded.length() > 16 * 1024) return null;
        try {
            String[] parts = encoded.split("\\|", -1);
            if (parts.length != 5 || !FORMAT.equals(parts[0])) return null;
            Value value = new Value(
                    Integer.parseInt(parts[1]),
                    decodeText(parts[2]),
                    decodeText(parts[3]),
                    parts[4].toLowerCase(Locale.ROOT)
            );
            return isStructurallyValid(value) ? value : null;
        } catch (IllegalArgumentException failure) {
            return null;
        }
    }

    static boolean shouldResume(Value value, int currentVersionCode) {
        return value != null
                && isStructurallyValid(value)
                && value.versionCode > currentVersionCode;
    }

    private static boolean isStructurallyValid(Value value) {
        if (value.versionCode <= 0) return false;
        if (value.versionName == null || value.versionName.length() > 128) return false;
        if (value.apkUrl == null
                || value.apkUrl.length() > 4096
                || !value.apkUrl.startsWith("https://")) {
            return false;
        }
        return value.sha256 != null && value.sha256.matches("(?i)[0-9a-f]{64}");
    }

    private static String encodeText(String value) {
        return Base64.getUrlEncoder()
                .withoutPadding()
                .encodeToString(value.getBytes(StandardCharsets.UTF_8));
    }

    private static String decodeText(String value) {
        byte[] decoded = Base64.getUrlDecoder().decode(value);
        if (decoded.length > 4096) {
            throw new IllegalArgumentException("Pending update field is too large");
        }
        return new String(decoded, StandardCharsets.UTF_8);
    }
}
