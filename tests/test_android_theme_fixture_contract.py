from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_theme_fixture_requires_exact_local_https_document_without_network():
    source=(ROOT/'android/app/src/androidTest/java/com/kenigevents/projectshub/MainActivitySmokeTest.java').read_text()
    helper=source[source.index('    private void page('):source.index('    private int themedRootColor')]
    assert 'shouldInterceptRequest' in helper
    assert 'url.equals(request.getUrl().toString())' in source
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


def test_fixture_intercepts_service_worker_requests_and_restores_global_client():
    source=(ROOT/'android/app/src/androidTest/java/com/kenigevents/projectshub/MainActivitySmokeTest.java').read_text()
    assert 'new ServiceWorkerClient()' in source
    assert source.count('return fixtureResponse(url, request);') == 2
    assert '@After public void restoreServiceWorkerClient()' in source
    cleanup=source[source.index('    @After public void restoreServiceWorkerClient()'):source.index('    private WebResourceResponse fixtureResponse(')]
    assert 'setServiceWorkerClient(new ServiceWorkerClient()' in cleanup
    assert 'return null;' in cleanup  # null response preserves the real network path
    assert 'fixtureResponse(' not in cleanup
    assert 'setServiceWorkerClient(null)' not in source
    assert 'bodyLength:document.body?.textContent?.length' in source
    assert 'worker:navigator.serviceWorker?.controller?.scriptURL' in source
