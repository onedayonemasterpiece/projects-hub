package com.kenigevents.projectshub;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

public class UpdatePolicyTest {
    @Test
    public void onlyNewerVersionIsOffered() {
        assertFalse(UpdatePolicy.shouldOffer(12, 12));
        assertFalse(UpdatePolicy.shouldOffer(12, 11));
        assertTrue(UpdatePolicy.shouldOffer(12, 13));
    }

    @Test
    public void displayVersionFallsBackToVersionCode() {
        assertEquals("0.1.12", UpdatePolicy.displayVersion("0.1.12", 12));
        assertEquals("v12", UpdatePolicy.displayVersion(" ", 12));
    }
}
