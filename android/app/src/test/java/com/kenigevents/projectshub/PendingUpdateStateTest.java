package com.kenigevents.projectshub;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

public class PendingUpdateStateTest {
    private static final String SHA =
            "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef";

    @Test
    public void roundTripSurvivesActivityOrProcessRecreation() {
        String encoded = PendingUpdateState.encode(
                50,
                "0.1.50",
                "https://github.com/example/releases/download/v50/app.apk",
                SHA
        );

        PendingUpdateState.Value restored = PendingUpdateState.decode(encoded);
        assertNotNull(restored);
        assertEquals(50, restored.versionCode);
        assertEquals("0.1.50", restored.versionName);
        assertEquals(
                "https://github.com/example/releases/download/v50/app.apk",
                restored.apkUrl
        );
        assertEquals(SHA, restored.sha256);
        assertTrue(PendingUpdateState.shouldResume(restored, 49));
        assertFalse(PendingUpdateState.shouldResume(restored, 50));
    }

    @Test
    public void damagedOrUnsafeMetadataFailsClosed() {
        assertNull(PendingUpdateState.decode(null));
        assertNull(PendingUpdateState.decode("garbage"));
        assertNull(
                PendingUpdateState.decode(
                        "v1|50||aHR0cDovL2V4YW1wbGUuY29tL2FwcC5hcGs|" + SHA
                )
        );
        assertNull(
                PendingUpdateState.decode(
                        "v1|50||aHR0cHM6Ly9leGFtcGxlLmNvbS9hcHAuYXBr|bad"
                )
        );
    }
}
