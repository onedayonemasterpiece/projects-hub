package com.kenigevents.projectshub;

import org.junit.Test;

import java.io.IOException;
import java.util.concurrent.atomic.AtomicInteger;

import static org.junit.Assert.*;

public final class NativeStartAttemptTest {
    @Test public void lostPostKeepsKeyUntilConfirmedTerminalAndDuplicateFailureDoesNotRotateTwice() throws Exception {
        AtomicInteger generated = new AtomicInteger();
        NativeStartAttempt attempt = new NativeStartAttempt(() -> "attempt_" + generated.incrementAndGet());
        String lostPost = attempt.current();
        assertEquals(lostPost, attempt.current()); // uncertain HTTP response: exact retry
        assertFalse(NativeStartAttempt.terminal("HTTP_502"));
        assertThrows(IllegalArgumentException.class, () -> attempt.afterTerminal(lostPost, "HTTP_502"));
        assertEquals(lostPost, attempt.current());
        String fresh = attempt.afterTerminal(lostPost, "LIVE_ATTEMPT_CLOSED");
        assertNotEquals(lostPost, fresh);
        assertEquals(fresh, attempt.afterTerminal(lostPost, "LIVE_ATTEMPT_CLOSED"));
        assertEquals(fresh, attempt.current()); // fresh POST response may also be lost
        assertEquals(2, generated.get());
    }

    @Test public void knownClosedTicketCreatesFreshAttemptAndStopFencesLateTerminalReplies() throws Exception {
        for (String code : new String[]{"LIVE_SESSION_CLOSED", "LIVE_SESSION_NOT_FOUND", "LIVE_ATTEMPT_CLOSED"}) {
            AtomicInteger generated = new AtomicInteger();
            NativeStartAttempt attempt = new NativeStartAttempt(() -> "attempt_" + generated.incrementAndGet());
            String original = attempt.current();
            String resumed = attempt.afterTerminal(original, code);
            assertNotEquals(original, resumed);
            attempt.cancel();
            IOException cancelled = assertThrows(IOException.class, () -> attempt.afterTerminal(resumed, code));
            assertEquals("NATIVE_VOICE_START_CANCELLED", cancelled.getMessage());
            assertThrows(IOException.class, attempt::current);
            assertEquals(2, generated.get());
        }
    }
}
