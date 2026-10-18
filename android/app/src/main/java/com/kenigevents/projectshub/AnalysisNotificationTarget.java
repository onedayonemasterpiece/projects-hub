package com.kenigevents.projectshub;

/** Only an opaque local result ID may cross the notification navigation boundary. */
final class AnalysisNotificationTarget {
    static final String EXTRA = "projects_hub_analysis_result";

    static boolean isValid(String id) {
        return id != null && id.matches("ca_[A-Za-z0-9_-]{1,128}");
    }
}
