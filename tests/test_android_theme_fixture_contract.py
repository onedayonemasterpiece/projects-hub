from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_theme_fixture_requires_exact_local_https_document_without_network():
    source=(ROOT/'android/app/src/androidTest/java/com/kenigevents/projectshub/MainActivitySmokeTest.java').read_text()
    helper=source[source.index('    private void page('):source.index('    private int themedRootColor')]
    assert 'shouldInterceptRequest' in helper
    assert 'url.equals(request.getUrl().toString())' in helper
    assert 'view.loadUrl(url)' in helper
    assert 'view.loadDataWithBaseURL' not in helper
    assert 'location.href===' in helper
    assert "document.readyState==='complete'" in helper
    assert "document.body?.textContent==='Theme bridge fixture'" in helper
    assert helper.index('loaded.await(50') < helper.index('assertTrue(')
    assert 'assertSame(original[0], activity.themeWebView())' in source
    assert 'message(scenario, "light", 1)' in source
    assert 'message(scenario, "dark", 2)' in source
    assert 'https://foreign.invalid/__theme_fixture__' in source
    assert 'typeof projectsHubTheme' in source
