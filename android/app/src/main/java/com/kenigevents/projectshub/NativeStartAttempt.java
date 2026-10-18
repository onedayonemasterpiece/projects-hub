package com.kenigevents.projectshub;

import java.io.IOException;
import java.util.UUID;
import java.util.function.Supplier;

/** A user Start keeps its HTTP idempotency key until the server proves it terminal. */
final class NativeStartAttempt {
    private final Supplier<String> ids;
    private String current;
    private boolean active = true;

    NativeStartAttempt() {
        this(() -> "native_" + UUID.randomUUID().toString().replace("-", ""));
    }

    NativeStartAttempt(Supplier<String> ids) {
        this.ids = ids;
        current = ids.get();
    }

    synchronized String current() throws IOException {
        requireActive();
        return current;
    }

    static boolean terminal(String code) {
        return "LIVE_SESSION_CLOSED".equals(code)
                || "LIVE_SESSION_NOT_FOUND".equals(code)
                || "LIVE_ATTEMPT_CLOSED".equals(code);
    }

    synchronized String afterTerminal(String observedAttempt, String code) throws IOException {
        requireActive();
        if (!terminal(code)) throw new IllegalArgumentException("Start outcome is not known to be terminal");
        // A duplicate late failure for the same request cannot rotate twice.
        if (current.equals(observedAttempt)) current = ids.get();
        return current;
    }

    synchronized void cancel() { active = false; }

    private void requireActive() throws IOException {
        if (!active) throw new IOException("NATIVE_VOICE_START_CANCELLED");
    }
}
