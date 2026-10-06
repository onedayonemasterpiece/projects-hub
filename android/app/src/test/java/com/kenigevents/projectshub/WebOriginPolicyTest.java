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
    }    @Test
    public void nativeMicrophoneSettingsRequireTrustedCurrentPageAndExactAction() {
        assertTrue(policy.isTrustedMicrophoneSettingsAction(
                "https://projects-hub.kenigevents.ru/work",
                "projectshub://settings/microphone"
        ));
        assertFalse(policy.isTrustedMicrophoneSettingsAction(
                "https://evil.example/",
                "projectshub://settings/microphone"
        ));
        assertFalse(policy.isTrustedMicrophoneSettingsAction(
                "https://projects-hub.kenigevents.ru/work",
                "projectshub://settings/calendar"
        ));
        assertFalse(policy.isTrustedMicrophoneSettingsAction(
                "https://projects-hub.kenigevents.ru/work",
                "projectshub://settings/microphone?next=https://evil.example"
        ));
    }


    @Test
    public void voiceAudioFocusRequiresTrustedPageAndExactBoundedAction() {
        assertTrue(policy.isTrustedVoiceAudioFocusAcquireAction(
                "https://projects-hub.kenigevents.ru/work",
                "projectshub://audio/focus/acquire"
        ));
        assertTrue(policy.isTrustedVoiceAudioFocusReleaseAction(
                "https://projects-hub.kenigevents.ru/work",
                "projectshub://audio/focus/release"
        ));
        assertFalse(policy.isTrustedVoiceAudioFocusAcquireAction(
                "https://evil.example/",
                "projectshub://audio/focus/acquire"
        ));
        assertFalse(policy.isTrustedVoiceAudioFocusAcquireAction(
                "https://projects-hub.kenigevents.ru/work",
                "projectshub://audio/focus/release"
        ));
        assertFalse(policy.isTrustedVoiceAudioFocusReleaseAction(
                "https://projects-hub.kenigevents.ru/work",
                "projectshub://audio/focus/acquire"
        ));
        assertFalse(policy.isTrustedVoiceAudioFocusAcquireAction(
                "https://projects-hub.kenigevents.ru/work",
                "projectshub://audio/focus/acquire?next=https://evil.example"
        ));
        assertFalse(policy.isTrustedVoiceAudioFocusReleaseAction(
                "https://projects-hub.kenigevents.ru/work",
                "projectshub://audio/focus/release#fragment"
        ));
    }

    @Test
    public void githubNavigationLeavesWebViewOnlyFromTrustedProjectsHubPage() {
        assertTrue(policy.isTrustedExternalGitHubNavigation(
                "https://projects-hub.kenigevents.ru/",
                "https://projects-hub.kenigevents.ru/api/github/app-manifest/launch?state=abc_DEF-123"
        ));
        assertTrue(policy.isTrustedExternalGitHubNavigation(
                "https://projects-hub.kenigevents.ru/work",
                "https://github.com/apps/projects-hub/installations/new?state=opaque"
        ));
        assertTrue(policy.isTrustedExternalGitHubNavigation(
                "https://projects-hub.kenigevents.ru/work",
                "https://github.com/settings/installations/123"
        ));
        assertFalse(policy.isTrustedExternalGitHubNavigation(
                "https://evil.example/",
                "https://github.com/settings/installations/123"
        ));
        assertFalse(policy.isTrustedExternalGitHubNavigation(
                "https://projects-hub.kenigevents.ru/",
                "https://github.com/evil/path"
        ));
        assertFalse(policy.isTrustedExternalGitHubNavigation(
                "https://projects-hub.kenigevents.ru/",
                "https://projects-hub.kenigevents.ru/api/github/app-manifest/launch?state=a&next=evil"
        ));
    }

    @Test
    public void legacyGithubBrowserActionIsExactAndStateBound() {
        assertTrue(policy.isTrustedGitHubBrowserAction(
                "https://projects-hub.kenigevents.ru/",
                "projectshub://browser/github?state=abcdefghijklmnopqrstuvwxyz_123456"
        ));
        assertFalse(policy.isTrustedGitHubBrowserAction(
                "https://evil.example/",
                "projectshub://browser/github?state=abcdefghijklmnopqrstuvwxyz_123456"
        ));
        assertFalse(policy.isTrustedGitHubBrowserAction(
                "https://projects-hub.kenigevents.ru/",
                "projectshub://browser/github?state=short"
        ));
        assertFalse(policy.isTrustedGitHubBrowserAction(
                "https://projects-hub.kenigevents.ru/",
                "projectshub://browser/github?state=abcdefghijklmnopqrstuvwxyz_123456&next=evil"
        ));
    }

    @Test
    public void updateCheckActionRequiresTrustedProjectsHubPage() {
        assertTrue(policy.isTrustedUpdateCheckAction(
                "https://projects-hub.kenigevents.ru/",
                "projectshub://update/check"
        ));
        assertFalse(policy.isTrustedUpdateCheckAction(
                "https://evil.example/",
                "projectshub://update/check"
        ));
        assertFalse(policy.isTrustedUpdateCheckAction(
                "https://projects-hub.kenigevents.ru/",
                "projectshub://update/install"
        ));
        assertFalse(policy.isTrustedUpdateCheckAction(
                "https://projects-hub.kenigevents.ru/",
                "projectshub://update/check?force=true"
        ));
    }

}