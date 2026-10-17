export function analysisResultId(value) {
  return typeof value === 'string' && /^ca_[A-Za-z0-9_-]{1,128}$/.test(value) ? value : null;
}

export function safeResultLink(value) {
  if (typeof value !== 'string') return null;
  try {
    const url = new URL(value);
    return url.protocol === 'https:' && !url.username && !url.password
      && ['github.com', 'app.slack.com'].includes(url.hostname) ? url.href : null;
  } catch { return null; }
}
