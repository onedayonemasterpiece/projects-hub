package com.kenigevents.projectshub;

/** Bounded enum/revision presentation only. Never authentication or preference authority. */
final class ThemePresentation {
    private long revision = -1;
    private String theme = "dark";

    boolean admit(String next, long nextRevision, boolean reset) {
        if (!("light".equals(next) || "dark".equals(next)) || nextRevision < 0
                || nextRevision > 9007199254740991L) return false;
        if (reset) {
            if (!"dark".equals(next) || nextRevision != 0) return false;
            reset();
            return true;
        }
        if (nextRevision < revision || (nextRevision == revision && !theme.equals(next))) return false;
        revision = nextRevision;
        theme = next;
        return true;
    }

    void reset() { revision = -1; theme = "dark"; }
}
