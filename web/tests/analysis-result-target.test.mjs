import test from 'node:test';
import assert from 'node:assert/strict';
import { analysisResultId, safeResultLink } from '../src/analysisResultTarget.js';

test('notification target is an opaque analysis ID, never a URL or script', () => {
  assert.equal(analysisResultId('ca_abc123'), 'ca_abc123');
  for (const bad of [null, '', 'devrun_1', 'https://evil.example', 'ca_../../secret', 'ca_\";alert(1)', 'ca_'+'x'.repeat(129)]) {
    assert.equal(analysisResultId(bad), null);
  }
});
test('result links allow only credential-free HTTPS GitHub or verified Slack destinations', () => {
  assert.equal(safeResultLink('https://github.com/org/repo/blob/main/docs/source.md'), 'https://github.com/org/repo/blob/main/docs/source.md');
  assert.ok(safeResultLink('https://app.slack.com/archives/C123/p123'));
  for (const bad of ['javascript:alert(1)', 'https://github.com.evil.com/', 'https://user:pass@github.com/', 'http://github.com/', '//github.com/', null]) {
    assert.equal(safeResultLink(bad), null);
  }
});
