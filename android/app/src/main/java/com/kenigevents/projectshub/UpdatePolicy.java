package com.kenigevents.projectshub;

final class UpdatePolicy {
    private UpdatePolicy() {}

    static boolean shouldOffer(int currentVersionCode, int candidateVersionCode) {
        return candidateVersionCode > currentVersionCode;
    }

    static String displayVersion(String versionName, int versionCode) {
        String clean = versionName == null ? "" : versionName.trim();
        return clean.isEmpty() ? "v" + versionCode : clean;
    }
}
