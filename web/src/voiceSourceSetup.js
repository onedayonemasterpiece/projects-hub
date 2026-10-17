const SETUP = '/api/voice-analysis/setup';

export function canConnectVoiceSource(setup) {
  return setup?.state === 'source_binding_required' && setup.can_connect_source === true
    && setup.source?.access_mode === 'read_only'
    && ['inbox/voice', 'inbox/voice/'].includes(setup.source?.prefix)
    && typeof setup.source?.repository === 'string' && setup.source.repository.length > 0;
}

// One instance belongs to one signed-in actor/workspace mount. No credentials,
// caller-selected repositories or broad GitHub discovery cross this boundary.
export function createVoiceSourceSetup(request, onChange) {
  let active = true;
  let busy = false;
  let state = { setup: null, busy: false, connected: false, notice: null };
  const emit = (change) => {
    if (!active) return;
    state = { ...state, ...change };
    onChange(state);
  };
  return {
    async load() {
      try {
        const setup = await request(SETUP);
        emit({ setup });
      } catch { /* Optional owner-only setup must not interrupt normal use. */ }
    },
    async connect() {
      if (!active || busy || !canConnectVoiceSource(state.setup)) return;
      busy = true;
      emit({ busy: true, notice: null });
      try {
        await request(SETUP + '/connect-source', { method: 'POST' });
        // The UI claims success only after an authoritative readback, not just
        // after accepting the POST or changing local state.
        if (!active) return;
        const setup = await request(SETUP);
        emit({ setup, connected: setup.state === 'ready',
          notice: setup.state === 'ready' ? null : 'Подключение источника пока не подтверждено.' });
      } catch {
        if (!active) return;
        // POST may have succeeded before a network interruption. Reconcile once
        // before offering another idempotent attempt; never blindly resend.
        try {
          const setup = await request(SETUP);
          emit({ setup, connected: setup.state === 'ready',
            notice: setup.state === 'ready' ? null : 'Не удалось подключить источник. Можно повторить попытку.' });
        } catch {
          emit({ notice: 'Не удалось проверить подключение. Попробуйте ещё раз при доступной сети.' });
        }
      } finally {
        busy = false;
        emit({ busy: false });
      }
    },
    dispose() { active = false; },
  };
}
