/** Personal presentation state. Never interprets speech or recreates a Live client. */
export function validPreference(value) {
  return value && (value.theme === 'light' || value.theme === 'dark')
    && Number.isSafeInteger(value.revision) && value.revision >= 0;
}

export function createThemePreferences({ render, acknowledge, onStatus = (_message) => {} }) {
  let actor = null, preference = { theme: 'dark', revision: 0 }, epoch = 0;
  async function reset(nextActor = null) {
    actor = nextActor;
    preference = { theme: 'dark', revision: 0 };
    ++epoch;
    await render(preference, true);
  }
  async function apply(value, binding = null, isCurrent = () => true) {
    if (!validPreference(value) || !actor || !isCurrent()) return false;
    if (binding && (binding.version !== 1 || binding.actor_id !== actor
        || typeof binding.command_id !== 'string' || !/^theme_[a-f0-9]{64}$/.test(binding.command_id))) return false;
    if (value.revision < preference.revision) return false;
    if (value.revision === preference.revision && value.theme !== preference.theme) {
      onStatus('Настройка темы требует сверки с сервером.');
      return false;
    }
    const appliedActor = actor, applicationEpoch = epoch;
    preference = { theme: value.theme, revision: value.revision };
    const rendered = await render(preference, false);
    if (applicationEpoch !== epoch || actor !== appliedActor || !isCurrent()
        || preference.theme !== value.theme || preference.revision !== value.revision) return false;
    if (!rendered.web || rendered.native === 'failed') {
      onStatus('Настройка сохранена, но применение на этом экране пока не подтверждено.');
      return false;
    }
    if (rendered.native === 'unsupported') onStatus('Тема экрана применена. Для оформления Android обновите приложение.');
    if (binding) await acknowledge(binding, preference);
    return true;
  }
  return { reset, apply, get actor() { return actor; }, get preference() { return { ...preference }; } };
}

export async function renderTheme(value, reset = false) {
  const root = document.documentElement;
  root.dataset.theme = value.theme;
  root.style.colorScheme = value.theme;
  const meta = document.querySelector('meta[name="theme-color"]');
  meta?.setAttribute('content', value.theme === 'light' ? '#f4f5f7' : '#070708');
  const bridge = window.projectsHubTheme;
  let native = new URLSearchParams(location.search).has('native_version') ? 'unsupported' : 'not_required';
  if (bridge?.postMessage) {
    const requestId = crypto.randomUUID();
    native = await new Promise(resolve => {
      const timer = setTimeout(() => { bridge.onmessage = null; resolve('failed'); }, 700);
      bridge.onmessage = event => {
        let message;
        try { message = JSON.parse(event.data); } catch { return; }
        if (message.request_id !== requestId || message.theme !== value.theme || message.revision !== value.revision) return;
        clearTimeout(timer); bridge.onmessage = null; resolve(message.applied === true ? 'applied' : 'failed');
      };
      bridge.postMessage(JSON.stringify({ version: 1, request_id: requestId, theme: value.theme,
        revision: value.revision, reset }));
    });
  }
  await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  return { web: root.dataset.theme === value.theme && getComputedStyle(root).getPropertyValue('--theme-marker').trim() === value.theme, native };
}
