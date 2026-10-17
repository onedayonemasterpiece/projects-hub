package com.kenigevents.projectshub;

import org.json.JSONObject;
import org.junit.Test;

import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;

import static org.junit.Assert.*;

/** The fixture is also validated by the Python API's actual LiveStart model. */
public final class NativeSessionRequestTest {
    @Test public void actualNativeStartPayloadMatchesLiveStartContract() throws Exception {
        JSONObject expected;
        try (InputStream stream = getClass().getResourceAsStream("/native-live-start.json")) {
            assertNotNull(stream);
            expected = new JSONObject(new String(stream.readAllBytes(), StandardCharsets.UTF_8));
        }
        JSONObject generated = new JSONObject();
        for (String name : new String[]{"normal", "recovery"}) {
            JSONObject actual = NativeSessionApi.startRequest("recovery".equals(name),
                    "contract-fixture", "android_contract_instance", "native_contract_attempt", "Europe/Kaliningrad");
            JSONObject contract = expected.getJSONObject(name);
            assertEquals(contract.length(), actual.length());
            for (var keys = contract.keys(); keys.hasNext();) {
                String key = keys.next();
                assertEquals(name + "." + key, contract.get(key), actual.get(key));
            }
            generated.put(name, actual);
        }
        // A pairing check can validate these exact produced payloads with the
        // server model, rather than infer protocol success from APK compilation.
        Path output = Path.of("build", "native-contract", "native-live-start.generated.json");
        Files.createDirectories(output.getParent());
        Files.write(output, generated.toString().getBytes(StandardCharsets.UTF_8));
    }

    @Test public void retryPreservesInstanceAndAttemptAndDoesNotInventTimezone() throws Exception {
        JSONObject first = NativeSessionApi.startRequest(false, "test", "instance_a", "attempt_a", "bad zone");
        JSONObject retry = NativeSessionApi.startRequest(false, "test", "instance_a", "attempt_a", null);
        assertEquals("realtime", retry.getString("audio_mode"));
        assertEquals(first.getString("attempt_id"), retry.getString("attempt_id"));
        assertEquals(first.getString("client_instance_id"), retry.getString("client_instance_id"));
        assertFalse(first.has("client_timezone")); assertFalse(retry.has("client_timezone"));
    }
}
