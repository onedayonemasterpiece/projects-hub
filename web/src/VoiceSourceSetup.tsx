import { useEffect, useRef, useState } from "react";
import { api } from "./api";
import { canConnectVoiceSource, createVoiceSourceSetup, type VoiceSourceSetupState } from "./voiceSourceSetup.js";
import "./voice-source-setup.css";

export default function VoiceSourceSetup() {
  const [state, setState] = useState<VoiceSourceSetupState>({
    setup: null, busy: false, connected: false, notice: null,
  });
  const controller = useRef<ReturnType<typeof createVoiceSourceSetup> | null>(null);
  useEffect(() => {
    const instance = createVoiceSourceSetup(api, setState);
    controller.current = instance;
    void instance.load();
    return () => { instance.dispose(); controller.current = null; };
  }, []);
  if (!canConnectVoiceSource(state.setup) && !state.connected && !state.notice) return null;
  return <aside className="voice-source-setup" aria-label="Источник голосовых заметок" aria-busy={state.busy}>
    {state.connected ? <p role="status">
      Источник подключён для чтения. Автоматическая обработка включается отдельно.
    </p> : <>
      <p>Для анализа голосовых заметок нужно подключить только чтение папки {state.setup?.source?.prefix}
        {" в "}{state.setup?.source?.repository}. Запись в репозиторий не разрешается.</p>
      {canConnectVoiceSource(state.setup) && <button className="quiet-button" disabled={state.busy}
        onClick={() => void controller.current?.connect()}>
        {state.busy ? "Подключаю источник…" : "Подключить чтение голосовых заметок"}
      </button>}
      {state.notice && <p role="alert">{state.notice}</p>}
    </>}
  </aside>;
}
