package com.kenigevents.projectshub;

/** A Stop invalidates every pending permission/auth/bootstrap completion. */
final class NativeVoiceOwner {
    record Lease(long generation, String conversationId) {}
    private long generation;
    private Lease current;

    synchronized Lease claim(String conversationId) {
        if (current != null) {
            if (!current.conversationId().equals(conversationId))
                throw new IllegalStateException("NATIVE_VOICE_ALREADY_ACTIVE");
            return current;
        }
        current = new Lease(++generation, conversationId);
        return current;
    }
    synchronized boolean owns(Lease lease) { return lease != null && lease.equals(current); }
    synchronized Lease current() { return current; }
    synchronized void stop() { ++generation; current = null; }
}
