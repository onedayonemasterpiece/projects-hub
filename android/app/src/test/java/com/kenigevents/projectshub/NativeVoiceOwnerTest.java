package com.kenigevents.projectshub;

import org.junit.Test;
import static org.junit.Assert.*;

public final class NativeVoiceOwnerTest {
    @Test public void repeatedStartForSameConversationHasExactlyOneOwner() {
        NativeVoiceOwner owner = new NativeVoiceOwner();
        var first = owner.claim("conversation_A");
        assertSame(first, owner.claim("conversation_A"));
        assertTrue(owner.owns(first));
        try { owner.claim("conversation_B"); fail("A second capture owner must be rejected"); }
        catch (IllegalStateException expected) { assertEquals("NATIVE_VOICE_ALREADY_ACTIVE", expected.getMessage()); }
        assertTrue(owner.owns(first));
    }

    @Test public void stopDuringPermissionOrAuthorizationCannotResurrectHardwareOnLateSuccess() {
        NativeVoiceOwner owner = new NativeVoiceOwner();
        var pendingPermission = owner.claim("conversation_A");
        owner.stop();
        assertFalse(owner.owns(pendingPermission));
        var next = owner.claim("conversation_A");
        assertFalse(owner.owns(pendingPermission));
        assertTrue(owner.owns(next));
        assertTrue(next.generation() > pendingPermission.generation());
    }
}
