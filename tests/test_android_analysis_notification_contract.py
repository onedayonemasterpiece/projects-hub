from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JAVA = ROOT / 'android/app/src/main/java/com/kenigevents/projectshub'


def test_analytics_reuses_scheduler_without_development_ids_or_briefing_ack():
    job=(JAVA/'DevelopmentCompletionJob.java').read_text()
    analytics=job[job.index('    private static void checkAnalysisAndNotify'):]
    assert 'recentAnalysisCompletions(token)' in analytics
    assert 'analysis_event_[0-9]+' in analytics
    assert 'devrun_' not in analytics
    assert 'firstSync' not in analytics
    assert 'delivered >= 3' in analytics
    assert 'if (notifier.analysisCompleted' in analytics
    assert analytics.index('if (notifier.analysisCompleted') < analytics.index('seen.add(id)')
    assert 'token.equals(new SecureStore(context).getDeviceToken())' in analytics
    assert 'brief_seen' not in analytics
    assert 'api/device/analysis/completed' in (JAVA/'ApiClient.java').read_text()


def test_notification_target_is_private_and_warm_open_preserves_live():
    notifier=(JAVA/'Notifier.java').read_text()
    assert 'VISIBILITY_PRIVATE' in notifier and '.setPublicVersion(' in notifier
    assert 'areNotificationsEnabled()' in notifier
    assert 'IMPORTANCE_NONE' in notifier
    assert 'manager.notify(tag, id, notification)' in notifier
    assert 'FLAG_IMMUTABLE' in notifier
    source=(JAVA/'MainActivity.java').read_text()
    handler=source[source.index('    private boolean handleAnalysisNotification'):source.index('    @Override\n    protected void onResume')]
    assert 'isTrustedPageUrl(webView.getUrl())' in handler
    assert 'AnalysisNotificationTarget.isValid' in handler
    assert 'projects-hub-analysis-result' in handler
    assert 'JSONObject.quote(analysisId)' in handler
    warm=handler[handler.index('if (webView'):handler.index('} else {')]
    assert 'loadUrl' not in warm and 'voice' not in warm.lower().replace('live session', '')
