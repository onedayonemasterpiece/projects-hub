package com.kenigevents.projectshub;

import android.net.Uri;
import org.json.JSONException;
import org.json.JSONObject;

/** Build-generated, display-only metadata shared by native Start and the UI. */
final class NativeBuildInfo {
    private NativeBuildInfo() {}

    static JSONObject json() throws JSONException {
        return new JSONObject()
                .put("package_id", BuildConfig.APPLICATION_ID)
                .put("version_code", BuildConfig.VERSION_CODE)
                .put("source_sha", BuildConfig.SOURCE_SHA)
                .put("sdk_version", BuildConfig.LIVE_SDK_VERSION)
                .put("sdk_sha", BuildConfig.LIVE_SDK_SHA);
    }

    static String querySuffix() {
        return "&native_code=" + BuildConfig.VERSION_CODE
                + "&native_package=" + Uri.encode(BuildConfig.APPLICATION_ID)
                + "&native_sha=" + Uri.encode(BuildConfig.SOURCE_SHA)
                + "&native_sdk=" + Uri.encode(BuildConfig.LIVE_SDK_VERSION)
                + "&native_sdk_sha=" + Uri.encode(BuildConfig.LIVE_SDK_SHA);
    }
}
