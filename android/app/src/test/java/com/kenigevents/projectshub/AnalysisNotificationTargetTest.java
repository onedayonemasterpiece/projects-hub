package com.kenigevents.projectshub;

import org.junit.Test;
import static org.junit.Assert.*;

public class AnalysisNotificationTargetTest {
    @Test public void acceptsOnlyOpaqueAnalysisIds() {
        assertTrue(AnalysisNotificationTarget.isValid("ca_0123abcd"));
        for (String value : new String[]{null, "", "devrun_123", "https://evil.test", "ca_../bad", "ca_\";alert(1)"}) {
            assertFalse(AnalysisNotificationTarget.isValid(value));
        }
        assertFalse(AnalysisNotificationTarget.isValid("ca_" + "a".repeat(129)));
    }
}
