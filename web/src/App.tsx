import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  createLiveClient,
  type LiveClient,
  type LiveEvent,
} from "@onedayonemasterpiece/live-interaction/browser";
import {
  ApiError,
  bootstrap,
  createConversation,
  getConversation,
  getMemories,
  login,
  type Bootstrap,
  type Conversation,
  type MemoryItem,
} from "./api";

type WaitState = null | { elapsed_ms: number; stage: string; can_restart: boolean };

const stateLabel: Record<string, string> = {
  off: "Готова слушать",
  starting: "Подключаюсь…",
  started: "Подключено",
  listening: "Слушаю",
  answering: "Отвечаю",
  reconnecting: "Восстанавливаю разговор…",
  microphone_unavailable: "Нет доступа к микрофону",
  connection_error: "Проблема соединения",
  start_error: "Не удалось начать",
};

function MicIcon() {
  return (
    <svg viewBox="0 0 48 48" aria-hidden="true">
      <rect x="17" y="7" width="14" height="23" rx="7" />
      <path d="M11 24c0 7.18 5.82 13 13 13s13-5.82 13-13M24 37v6M18 43h12" />
    </svg>
  );
}

function formatWait(wait: NonNullable<WaitState>) {
  const seconds = Math.floor(wait.elapsed_ms / 1000);
  const minutes = Math.floor(seconds / 60);
  return `${String(minutes).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
}

export default function App() {
  const [boot, setBoot] = useState<Bootstrap | null>(null);
  const [authReady, setAuthReady] = useState(false);
  const [conversation, setConversation] = useState<Conversation | null>(null);
  const [voiceState, setVoiceState] = useState("off");
  const [answer, setAnswer] = useState("");
  const [notice, setNotice] = useState<string | null>(null);
  const [wait, setWait] = useState<WaitState>(null);
  const [memories, setMemories] = useState<MemoryItem[]>([]);
  const [contextOpen, setContextOpen] = useState(false);
  const [memoryOpen, setMemoryOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const clientRef = useRef<LiveClient | null>(null);
  const turnHasInput = useRef(false);
  const conversationRef = useRef<Conversation | null>(null);

  useEffect(() => {
    conversationRef.current = conversation;
  }, [conversation]);

  const focusProject = useMemo(() => {
    if (!boot) return null;
    const id = conversation?.focus_project_id;
    return id ? boot.projects.find(project => project.id === id) ?? null : null;
  }, [boot, conversation]);

  const loadMemories = useCallback(async () => {
    if (!boot) return;
    const result = await getMemories(
      boot.workspace.id,
      conversationRef.current?.focus_project_id ?? null,
    );
    setMemories(result.items);
  }, [boot]);

  useEffect(() => {
    bootstrap()
      .then(async value => {
        setBoot(value);
        const saved = localStorage.getItem("projects-hub-conversation");
        if (saved) {
          try {
            const current = await getConversation(saved);
            if (current.workspace_id === value.workspace.id) setConversation(current);
            else localStorage.removeItem("projects-hub-conversation");
          } catch {
            localStorage.removeItem("projects-hub-conversation");
          }
        }
      })
      .catch(error => {
        if (!(error instanceof ApiError) || error.status !== 401) setNotice(error.message);
      })
      .finally(() => setAuthReady(true));
  }, []);

  useEffect(() => {
    if (!boot) return;
    const client = createLiveClient({
      onState: state => {
        setVoiceState(state);
        if (state === "listening") setNotice(null);
      },
      onNotice: (kind, error) => {
        if (kind === "microphone_error") setNotice("Браузер не дал доступ к микрофону.");
        else if (kind === "transport_error") setNotice("Уже принятый сервером источник сохранён. Последние непереданные секунды не считаются сохранёнными — остановите и запустите Live снова.");
        else if (kind === "connection_error") setNotice("Связь с Live прервалась. Можно запустить разговор снова.");
        else if (kind === "event_gap") setNotice("Интерфейс пропустил часть служебных событий. Источник на сервере сохраняется отдельно.");
        else if (error instanceof Error) setNotice(error.message);
      },
      onWait: value => setWait(value),
      onEvent: (event: LiveEvent) => {
        if (event.type === "input_transcript") {
          if (!turnHasInput.current) {
            turnHasInput.current = true;
            setAnswer("");
          }
        } else if (event.type === "output_transcript" && typeof event.text === "string") {
          setAnswer(previous => (previous + event.text).slice(-5000));
        } else if (event.type === "turn_complete") {
          turnHasInput.current = false;
        } else if (event.type === "tool_result" && event.status === "ok") {
          if (event.name === "memory_commit_voice_source") {
            void loadMemories().then(() => setMemoryOpen(true));
          }
          if (event.name === "conversation_set_focus" && conversationRef.current) {
            void getConversation(conversationRef.current.id).then(setConversation);
          }
        } else if (event.type === "capability_unavailable" && event.code !== "NOT_CONFIGURED") {
          setNotice("Одна из дополнительных возможностей сейчас недоступна.");
        }
      },
    });
    clientRef.current = client;
    return () => {
      client.stop({ reason: "ui_unmount" });
      clientRef.current = null;
    };
  }, [boot, loadMemories]);

  async function signIn() {
    setBusy(true);
    setNotice(null);
    try {
      const value = await login();
      setBoot(value);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "Не удалось войти.");
    } finally {
      setBusy(false);
    }
  }

  async function ensureConversation() {
    if (!boot) throw new Error("Нет workspace");
    if (conversationRef.current) return conversationRef.current;
    const created = await createConversation(boot.workspace.id, null);
    setConversation(created);
    conversationRef.current = created;
    localStorage.setItem("projects-hub-conversation", created.id);
    return created;
  }

  async function toggleVoice() {
    const client = clientRef.current;
    if (!client || !boot) return;
    const active = voiceState !== "off" && voiceState !== "start_error" && voiceState !== "connection_error";
    if (active || client.sessionId || client.starting) {
      client.stop({ reason: "user_stop" });
      setWait(null);
      return;
    }
    setBusy(true);
    setNotice(null);
    setAnswer("");
    try {
      const current = await ensureConversation();
      await client.start({
        url: `/api/live/${current.id}/sessions`,
        body: {},
        microphone: true,
        captureDuringStart: true,
        authorize: async () => {},
      });
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "Не удалось начать Live.");
      setVoiceState("off");
    } finally {
      setBusy(false);
    }
  }

  async function openMemory() {
    try {
      await loadMemories();
      setMemoryOpen(true);
      setContextOpen(false);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "Не удалось прочитать память.");
    }
  }

  if (!authReady) {
    return <main className="shell loading-shell"><div className="boot-dot" aria-label="Загрузка" /></main>;
  }

  if (!boot) {
    return (
      <main className="shell login-shell">
        <section className="login-island">
          <div className="brand-mark"><span /></div>
          <p className="eyebrow">Projects Hub</p>
          <h1>Проекты — голосом.</h1>
          <p className="login-copy">
            Один Live‑собеседник слышит вас, понимает контекст проекта и вызывает только разрешённые действия.
          </p>
          <button className="primary-action" onClick={signIn} disabled={busy}>
            {busy ? "Вхожу…" : "Войти в пилот"}
          </button>
          {notice && <p className="notice" role="alert">{notice}</p>}
        </section>
      </main>
    );
  }

  const voiceActive = !["off", "start_error", "connection_error", "microphone_unavailable"].includes(voiceState);
  const showWork = Boolean(answer || notice || wait || memoryOpen);
  const projectCount = Math.max(0, boot.projects.length - 1);

  return (
    <main className="shell">
      <header className="context-wrap">
        <button
          className={"island context-island" + (contextOpen ? " is-open" : "")}
          onClick={() => {
            setContextOpen(value => !value);
            setMemoryOpen(false);
          }}
          aria-expanded={contextOpen}
        >
          <span className="context-brand">PH</span>
          <span className="context-copy">
            <span className="context-workspace">{boot.workspace.name}</span>
            <span className="context-project">{focusProject?.name ?? "Несколько проектов"}</span>
          </span>
          {projectCount > 0 && <span className="context-count">+{projectCount}</span>}
          <span className="chevron">⌄</span>
        </button>

        {contextOpen && (
          <section className="island context-sheet" aria-label="Контекст проектов">
            <div className="sheet-heading">
              <div>
                <p className="eyebrow">Контекст</p>
                <h2>{boot.workspace.name}</h2>
              </div>
              <button className="quiet-button" onClick={openMemory}>Память</button>
            </div>
            <div className="project-list">
              {boot.projects.map(project => (
                <div
                  className={"project-row" + (project.id === focusProject?.id ? " current" : "")}
                  key={project.id}
                >
                  <span>{project.name}</span>
                  <span>{project.id === focusProject?.id ? "в фокусе" : "доступен"}</span>
                </div>
              ))}
            </div>
            <p className="sheet-footnote">Сменить проект можно голосом. Live‑агент сам уточнит контекст, если он неоднозначен.</p>
          </section>
        )}
      </header>

      <section className={"work-zone" + (showWork ? " visible" : "")} aria-live="polite">
        {showWork && (
          <article className="island work-island">
            {wait ? (
              <div className="wait-card">
                <p className="eyebrow">{wait.stage === "action" ? "Выполняю действие" : "Live думает"}</p>
                <strong>{formatWait(wait)}</strong>
                <p>{wait.can_restart ? "Можно остановить и начать снова — источник останется сохранён." : "Можно остановить в любой момент."}</p>
              </div>
            ) : memoryOpen ? (
              <div className="memory-card">
                <div className="sheet-heading">
                  <div>
                    <p className="eyebrow">Память проекта</p>
                    <h2>{focusProject?.name ?? "Проекты"}</h2>
                  </div>
                  <button className="quiet-button" onClick={() => setMemoryOpen(false)}>Закрыть</button>
                </div>
                {memories.length ? (
                  <div className="memory-list">
                    {memories.map(item => (
                      <div className="memory-row" key={item.id}>
                        <span className="memory-kind">{item.kind}</span>
                        <strong>{item.title}</strong>
                        {item.semantic_notes && <p>{item.semantic_notes}</p>}
                      </div>
                    ))}
                  </div>
                ) : (
                  <p className="empty-copy">Пока ничего не сохранено. Скажите, что нужно запомнить.</p>
                )}
              </div>
            ) : answer ? (
              <div className="answer-card">
                <p className="eyebrow">Live</p>
                <p className="answer-text">{answer}</p>
              </div>
            ) : (
              <div className="notice-card">
                <p className="eyebrow">Состояние</p>
                <p>{notice}</p>
              </div>
            )}
          </article>
        )}
      </section>

      {(answer || memories.length > 0) && !voiceActive && (
        <nav className="action-islands" aria-label="Контекстные действия">
          {memories.length > 0 && <button className="island action-pill" onClick={openMemory}>Память</button>}
          {answer && <button className="island action-pill" onClick={() => setAnswer("")}>Убрать результат</button>}
        </nav>
      )}

      <section className="voice-dock">
        <div className={"island voice-island state-" + voiceState}>
          <button
            className={"voice-orb" + (voiceActive ? " active" : "")}
            onClick={toggleVoice}
            disabled={busy}
            aria-label={voiceActive ? "Остановить разговор" : "Начать голосовой разговор"}
          >
            <span className="pulse pulse-one" />
            <span className="pulse pulse-two" />
            <span className="orb-core"><MicIcon /></span>
          </button>
          <div className="voice-copy">
            <strong>{stateLabel[voiceState] ?? "Live"}</strong>
            <span>{voiceActive ? "Нажмите, чтобы остановить" : "Нажмите и говорите"}</span>
          </div>
          <span className={"state-dot " + (voiceActive ? "live" : "")} />
        </div>
      </section>
    </main>
  );
}
