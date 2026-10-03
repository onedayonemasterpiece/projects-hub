package com.kenigevents.projectshub;

import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

public class WebOriginPolicyTest {
    private final WebOriginPolicy policy =
            new WebOriginPolicy("https://projects-hub.kenigevents.ru/");

    @Test
    public void microphoneRequiresExactHttpsOrigin() {
        assertTrue(policy.isTrustedPermissionOrigin(
                "https://projects-hub.kenigevents.ru"
        ));
        assertTrue(policy.isTrustedPermissionOrigin(
                "https://projects-hub.kenigevents.ru/"
        ));

        assertFalse(policy.isTrustedPermissionOrigin(
                "http://projects-hub.kenigevents.ru/"
        ));
        assertFalse(policy.isTrustedPermissionOrigin(
                "https://evil.projects-hub.kenigevents.ru/"
        ));
        assertFalse(policy.isTrustedPermissionOrigin(
                "https://projects-hub.kenigevents.ru.evil.example/"
        ));
        assertFalse(policy.isTrustedPermissionOrigin(
                "https://projects-hub.kenigevents.ru:444/"
        ));
        assertFalse(policy.isTrustedPermissionOrigin(
                "https://user@projects-hub.kenigevents.ru/"
        ));
        assertFalse(policy.isTrustedPermissionOrigin(
                "https://projects-hub.kenigevents.ru/oauth"
        ));
    }

    @Test
    public void productPagesMayUsePathsButForeignOauthPagesAreNotTrusted() {
        assertTrue(policy.isTrustedPageUrl(
                "https://projects-hub.kenigevents.ru/?code=callback"
        ));
        assertTrue(policy.isTrustedPageUrl(
                "https://projects-hub.kenigevents.ru/work"
        ));
        assertFalse(policy.isTrustedPageUrl(
                "https://epyznmylqmchteykjsqj.supabase.co/auth/v1/authorize"
        ));
        assertFalse(policy.isTrustedPageUrl(
                "https://oauth.yandex.ru/authorize"
        ));
    }
}
