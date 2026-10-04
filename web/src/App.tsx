import { useCallback, useEffect, useMemo, useRef, useState, type MutableRefObject } from "react";
import {
  createDurableMicrophoneCapture,
  createLiveClient,
  type DurableMicrophoneCapture,
  type LiveClient,
  type LiveEvent,
} from "@onedayonemasterpiece/live-interaction/browser";
import {
  ApiError,
  bootstrap,
  bindGitHubRepository,
  createConversation,
  getConversation,
  getGitHubStatus,
  getMemories,
  getEventCards,
  getTasks,
  getDevelopmentBacklog,
  getDevelopmentCodexStatus,
  getLatestDevelopmentExecution,
  getAuthConfig,
  exchangeInvite,
  login,
  setTaskState,
  startGitHubInstall,
  startGitHubManifest,
  type AuthConfig,
  type Bootstrap,
  type Conversation,
  type GitHubStatus,
  type MemoryItem,
  type EventCard,
  type TaskItem,
  type DevelopmentExecution,
  type CodexStatus,
} from "./api";
import { replayLocalVoiceSource } from "./bufferedReplay";
import {
  acknowledgeDeliveredSource,
  createLocalPersistSink,
  createLocalVoiceSource,
  listPendingVoiceSources,
  recoverInterruptedVoiceSources,
  sealLocalVoiceSource,
  type LocalVoiceSource,
} from "./offlineSources";

type WaitState = null | { elapsed_ms: number; stage: string; can_restart: boolean };
type ChatRole = "user" | "assistant";
type ChatMessage = { role: ChatRole; text: string };

const developmentStageLabel: Record<string, string> = {
  design: "Проектирование",
  implementation: "Разработка",
  testing: "Тестирование",
  ci: "CI",
  review: "Ревью",
  rework: "Доработка",
  delivery: "Поставка",
  deploying: "Развёртывание",
  releasing: "Релиз",
  ready: "Готово",
  capacity_wait: "Ожидание лимита",
  needs_owner: "Нужно решение владельца",
  failed: "Ошибка",
};

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
  offline_recording: "Записываю без сети",
  replaying: "Передаю сохранённую запись",
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

function mergeTranscript(current: string, fragment: string) {
  const clean = fragment.trim();
  if (!clean) return current;
  if (!current) return clean;
  if (clean.startsWith(current)) return clean;
  if (current.endsWith(clean)) return current;
  let overlap = Math.min(current.length, clean.length);
  while (overlap >= 3 && current.slice(-overlap) !== clean.slice(0, overlap)) overlap -= 1;
  return overlap >= 3 ? current + clean.slice(overlap) : current + " " + clean;
}

function friendlyStartError(error: unknown) {
  const name = error && typeof error === "object" && "name" in error
    ? String((error as { name?: unknown }).name ?? "")
    : "";
  if (name === "NotFoundError") {
    return "На этом устройстве не найден доступный микрофон.";
  }
  if (name === "NotAllowedError" || name === "SecurityError") {
    return "Браузер не дал доступ к микрофону.";
  }
  if (error instanceof Error) {
    return /requested device not found/i.test(error.message)
      ? "На этом устройстве не найден доступный микрофон."
      : error.message;
  }
  return "Не удалось начать Live.";
}

export default function App() {
  const isAndroidApp = navigator.userAgent.includes("ProjectsHubAndroid/");
  const nativeVersion = useMemo(() => {
    const queryVersion = new URLSearchParams(window.location.search).get("native_version");
    if (queryVersion) return queryVersion;
    const match = navigator.userAgent.match(/ProjectsHubAndroid\/([^\s]+)/);
    return match?.[1] ?? null;
  }, []);
  const clientTimezone = useMemo(
    () => Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
    [],
  );
  const [boot, setBoot] = useState<Bootstrap | null>(null);
  const [authConfig, setAuthConfig] = useState<AuthConfig | null>(null);
  const [authReady, setAuthReady] = useState(false);
  const [conversation, setConversation] = useState<Conversation | null>(null);
  const [voiceState, setVoiceState] = useState("off");
  const [chatMessages, setChatMessages] = useState<ChatMessage[]>([]);
  const [notice, setNotice] = useState<string | null>(null);
  const [microphoneSettingsAvailable, setMicrophoneSettingsAvailable] = useState(false);
  const [wait, setWait] = useState<WaitState>(null);
  const [memories, setMemories] = useState<MemoryItem[]>([]);
  const [eventCards, setEventCards] = useState<EventCard[]>([]);
  const [backlogTasks, setBacklogTasks] = useState<TaskItem[]>([]);
  const [codexStatus, setCodexStatus] = useState<CodexStatus | null>(null);
  const [developmentExecution, setDevelopmentExecution] = useState<DevelopmentExecution | null>(null);
  const [developmentAccess, setDevelopmentAccess] = useState<boolean | null>(null);
  const [contextOpen, setContextOpen] = useState(false);
  const [memoryOpen, setMemoryOpen] = useState(false);
  const [eventOpen, setEventOpen] = useState(false);
  const [backlogOpen, setBacklogOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [inviteCode, setInviteCode] = useState("");
  const [networkOnline, setNetworkOnline] = useState(() => navigator.onLine);
  const [pendingSources, setPendingSources] = useState<LocalVoiceSource[]>([]);
  const [githubStatus, setGitHubStatus] = useState<GitHubStatus | null>(null);
  const [githubBusy, setGitHubBusy] = useState(false);
  const clientRef = useRef<LiveClient | null>(null);
  const userTranscriptIndex = useRef(-1);
  const assistantTranscriptIndex = useRef(-1);
  const chatScrollRef = useRef<HTMLDivElement | null>(null);
  const chatFollowRef = useRef(true);
  const offlineCaptureRef = useRef<DurableMicrophoneCapture | null>(null);
  const offlineSourceRef = useRef<{
    source: LocalVoiceSource;
    sink: ReturnType<typeof createLocalPersistSink>;
  } | null>(null);
  const replayingRef = useRef(false);
  const turnHasInput = useRef(false);
  const conversationRef = useRef<Conversation | null>(null);
  const lastDevelopmentUpdateCheckRef = useRef(
    localStorage.getItem("projects-hub-development-update-check") ?? "",
  );
  const userStoppedVoiceRef = useRef(false);
  const autoRecoveryAtRef = useRef(0);

  useEffect(() => {
    conversationRef.current = conversation;
  }, [conversation]);

  useEffect(() => {
    const element = chatScrollRef.current;
    if (!element || !chatFollowRef.current) return;
    element.scrollTop = element.scrollHeight;
  }, [chatMessages]);

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

  const loadEventCards = useCallback(async () => {
    if (!boot) return;
    const result = await getEventCards(
      boot.workspace.id,
      conversationRef.current?.focus_project_id ?? null,
    );
    setEventCards(result.items);
  }, [boot]);

  const loadBacklog = useCallback(async () => {
    if (!boot) return;
    const projectId = conversationRef.current?.focus_project_id ?? null;
    const tasks = boot.role === "owner"
      ? await getDevelopmentBacklog(boot.workspace.id, projectId)
      : await getTasks(boot.workspace.id, projectId);
    setBacklogTasks(tasks.items);

    if (boot.role !== "owner") {
      setDevelopmentAccess(false);
      setCodexStatus(null);
      setDevelopmentExecution(null);
      return;
    }

    try {
      const [capacity, latest] = await Promise.all([
        getDevelopmentCodexStatus(boot.workspace.id),
        getLatestDevelopmentExecution(boot.workspace.id, true),
      ]);
      setDevelopmentAccess(true);
      setCodexStatus(capacity);
      setDevelopmentExecution(latest.execution);
    } catch (error) {
      if (error instanceof ApiError && error.status === 403) {
        setDevelopmentAccess(false);
        setCodexStatus(null);
        setDevelopmentExecution(null);
        return;
      }
      throw error;
    }
  }, [boot]);

  const refreshPendingSources = useCallback(async () => {
    if (!boot) return;
    setPendingSources(await listPendingVoiceSources(boot.workspace.id));
  }, [boot]);

  const refreshGitHub = useCallback(async () => {
    if (!boot || boot.role !== "owner") {
      setGitHubStatus(null);
      return;
    }
    setGitHubStatus(await getGitHubStatus(boot.workspace.id));
  }, [boot]);

  useEffect(() => {
    if (!boot || boot.role !== "owner" || developmentAccess === false) return;
    let cancelled = false;

    const syncDevelopment = async () => {
      try {
        const latest = await getLatestDevelopmentExecution(boot.workspace.id, true);
        if (cancelled) return;
        setDevelopmentAccess(true);
        setDevelopmentExecution(latest.execution);
        const execution = latest.execution;
        if (
          execution
          && execution.update_check_recommended
          && ["completed", "failed", "cancelled"].includes(execution.status)
          && isAndroidApp
        ) {
          const key = execution.id + ":" + execution.updated_at_ms;
          if (lastDevelopmentUpdateCheckRef.current !== key) {
            lastDevelopmentUpdateCheckRef.current = key;
            localStorage.setItem("projects-hub-development-update-check", key);
            window.location.href = "projectshub://update/check";
          }
        }
      } catch (error) {
        if (cancelled) return;
        if (error instanceof ApiError && error.status === 403) {
          setDevelopmentAccess(false);
          setDevelopmentExecution(null);
        }
      }
    };

    void syncDevelopment();
    const timer = window.setInterval(syncDevelopment, 15_000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [boot, developmentAccess, isAndroidApp]);

  const mergeChatMessage = useCallback((role: ChatRole, fragment: string, preferred: MutableRefObject<number>) => {
    const clean = fragment.trim();
    if (!clean) return;
    setChatMessages(previous => {
      const messages = [...previous];
      const index = preferred.current >= 0
        && preferred.current < messages.length
        && messages[preferred.current]?.role === role
        ? preferred.current
        : -1;
      if (index < 0) {
        messages.push({ role, text: clean.slice(0, 4000) });
        if (messages.length > 48) messages.splice(0, messages.length - 48);
        preferred.current = messages.length - 1;
        return messages;
      }
      messages[index] = {
        ...messages[index],
        text: mergeTranscript(messages[index].text, clean).slice(0, 4000),
      };
      return messages;
    });
  }, []);

  const applyLiveEvent = useCallback((event: LiveEvent) => {
    if (event.type === "input_transcript" && typeof event.text === "string") {
      if (!turnHasInput.current) turnHasInput.current = true;
      mergeChatMessage("user", event.text, userTranscriptIndex);
    } else if (event.type === "output_transcript" && typeof event.text === "string") {
      mergeChatMessage("assistant", event.text, assistantTranscriptIndex);
    } else if (event.type === "turn_complete") {
      turnHasInput.current = false;
      userTranscriptIndex.current = -1;
      assistantTranscriptIndex.current = -1;
    } else if (event.type === "tool_result" && event.status === "ok") {
      if (event.name === "memory_commit_voice_source") {
        void loadMemories().then(() => setMemoryOpen(true));
      }
      if ([
        "calendar_create_event_on_device",
        "event_readiness_set",
        "task_create_follow_up",
        "task_set_state",
      ].includes(event.name ?? "")) {
        void loadEventCards().then(() => {
          setEventOpen(true);
          setMemoryOpen(false);
          setBacklogOpen(false);
        });
        if (["task_create_follow_up", "task_set_state"].includes(event.name ?? "")) {
          void loadBacklog();
        }
      }
      if ([
        "development_execute_backlog",
        "development_execution_status",
        "development_codex_status",
      ].includes(event.name ?? "")) {
        void loadBacklog().then(() => {
          setBacklogOpen(true);
          setEventOpen(false);
          setMemoryOpen(false);
        });
      }
      if (event.name === "conversation_set_focus" && conversationRef.current) {
        void getConversation(conversationRef.current.id).then(setConversation);
        void loadEventCards();
      }
    } else if (event.type === "capability_unavailable" && event.code !== "NOT_CONFIGURED") {
      setNotice("Одна из дополнительных возможностей сейчас недоступна.");
    }
  }, [loadBacklog, loadEventCards, loadMemories, mergeChatMessage]);

  useEffect(() => {
    let cancelled = false;

    async function applyBootstrap(value: Bootstrap) {
      if (cancelled) return;
      setBoot(value);
      const saved = localStorage.getItem("projects-hub-conversation");
      if (!saved) return;
      try {
        const current = await getConversation(saved);
        if (cancelled) return;
        if (current.workspace_id === value.workspace.id) setConversation(current);
        else localStorage.removeItem("projects-hub-conversation");
      } catch {
        localStorage.removeItem("projects-hub-conversation");
      }
    }

    async function initialize() {
      try {
        const config = await getAuthConfig();
        if (cancelled) return;
        setAuthConfig(config);

        try {
          await applyBootstrap(await bootstrap());
        } catch (error) {
          if (!(error instanceof ApiError) || error.status !== 401) throw error;
        }
      } catch (error) {
        if (!cancelled) {
          setNotice(error instanceof Error ? error.message : "Не удалось проверить вход.");
        }
      } finally {
        if (!cancelled) setAuthReady(true);
      }
    }

    void initialize();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    const online = () => {
      setNetworkOnline(true);
      void refreshPendingSources();
    };
    const offline = () => setNetworkOnline(false);
    window.addEventListener("online", online);
    window.addEventListener("offline", offline);
    return () => {
      window.removeEventListener("online", online);
      window.removeEventListener("offline", offline);
    };
  }, [refreshPendingSources]);

  useEffect(() => {
    if (!boot) return;
    void loadEventCards().catch(() => {});
  }, [boot, loadEventCards]);

  useEffect(() => {
    if (!boot) return;
    void recoverInterruptedVoiceSources(boot.workspace.id)
      .then(refreshPendingSources)
      .catch(error => {
        setNotice(error instanceof Error ? error.message : "Не удалось восстановить локальные записи.");
      });
  }, [boot, refreshPendingSources]);

  useEffect(() => {
    if (!boot || boot.role !== "owner") return;
    void refreshGitHub()
      .then(() => {
        const params = new URLSearchParams(window.location.search);
        if (params.get("github") === "connected") {
          params.delete("github");
          const query = params.toString();
          window.history.replaceState({}, "", window.location.pathname + (query ? "?" + query : "") + window.location.hash);
          setNotice("GitHub подключён. Доступны только repositories, разрешённые установкой.");
        }
      })
      .catch(error => {
        setNotice(error instanceof Error ? error.message : "Не удалось прочитать GitHub connections.");
      });
  }, [boot, refreshGitHub]);

  useEffect(() => {
    if (!boot) return;
    const client = createLiveClient({
      transport: "wss",
      voiceControl: null,
      suppressCaptureDuringPlayback: true,
      onState: state => {
        setVoiceState(state);
        if (state === "listening") {
          setNotice(null);
          setMicrophoneSettingsAvailable(false);
        }
      },
      onNotice: (kind, error) => {
        if (kind === "microphone_error") {
          clientRef.current?.stop({ reason: "microphone_unavailable" });
          setMicrophoneSettingsAvailable(isAndroidApp);
          setNotice("Android/WebView не получил микрофон. Откройте настройки приложения и разрешите микрофон; если он уже разрешён, проверьте системный переключатель доступа к микрофону.");
        } else if (kind === "transport_error") {
          setNotice("Связь с Live прервалась. Восстанавливаю разговор…");
          const now = Date.now();
          if (
            !userStoppedVoiceRef.current
            && navigator.onLine
            && now - autoRecoveryAtRef.current > 20_000
          ) {
            autoRecoveryAtRef.current = now;
            window.setTimeout(() => void recoverLiveConversation(), 1200);
          }
        } else if (kind === "connection_error") {
          setNotice("Связь с Live нестабильна. Пытаюсь переподключиться…");
        }
        else if (kind === "event_gap") setNotice("Интерфейс пропустил часть служебных событий. Источник на сервере сохраняется отдельно.");
        else if (error) setNotice(friendlyStartError(error));
      },
      onWait: value => setWait(value),
      onEvent: applyLiveEvent,
    });
    clientRef.current = client;
    return () => {
      client.stop({ reason: "ui_unmount" });
      clientRef.current = null;
    };
  }, [applyLiveEvent, boot]);

  async function signIn() {
    setBusy(true);
    setNotice(null);
    try {
      if (authConfig?.mode === "first_party_invite") {
        const value = inviteCode.trim();
        if (!value) throw new Error("Введите одноразовый код приглашения.");
        setBoot(await exchangeInvite(value));
        setInviteCode("");
        return;
      }
      if (authConfig?.mode === "disabled") {
        throw new Error("Вход сейчас недоступен.");
      }
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

  async function startOfflineCapture() {
    if (!boot) return;
    const current = conversationRef.current;
    if (!current) {
      throw new Error("Для работы без сети сначала откройте Projects Hub один раз при подключении.");
    }
    const source = await createLocalVoiceSource(boot.workspace.id, current.id);
    const sink = createLocalPersistSink(source.id);
    const capture = createDurableMicrophoneCapture({
      persist: sink.persist,
      onError: error => {
        setNotice(error instanceof Error ? error.message : "Локальная запись остановлена.");
        setVoiceState("off");
      },
    });
    offlineSourceRef.current = { source, sink };
    offlineCaptureRef.current = capture;
    try {
      await capture.start();
      setVoiceState("offline_recording");
      setNotice("Связи нет. Речь сохраняется на этом устройстве и никуда не отправляется.");
    } catch (error) {
      offlineCaptureRef.current = null;
      offlineSourceRef.current = null;
      await acknowledgeDeliveredSource(source.id);
      throw error;
    }
  }

  async function stopOfflineCapture() {
    const capture = offlineCaptureRef.current;
    const local = offlineSourceRef.current;
    if (!capture || !local) return;
    setBusy(true);
    try {
      await capture.stop();
      await local.sink.drain();
      const sealed = await sealLocalVoiceSource(local.source.id);
      setNotice(
        sealed.chunk_count > 0
          ? "Запись сохранена на устройстве. Когда связь появится, её можно передать Live."
          : "Речь не обнаружена; пустая запись не будет отправлена.",
      );
    } finally {
      offlineCaptureRef.current = null;
      offlineSourceRef.current = null;
      setVoiceState("off");
      setBusy(false);
      await refreshPendingSources();
    }
  }

  async function deliverSavedSource() {
    if (!networkOnline || !pendingSources.length || replayingRef.current) return;
    const source = pendingSources[0];
    replayingRef.current = true;
    setBusy(true);
    setVoiceState("replaying");
    setNotice("Передаю сохранённую запись центральному Live‑агенту…");
    try {
      const result = await replayLocalVoiceSource(source, {
        onState: state => {
          if (state !== "off") setVoiceState(state);
        },
        onEvent: applyLiveEvent,
        onNotice: message => setNotice(message),
      });
      if (result.status === "delivered") {
        setNotice(
          result.skipped_provider
            ? "Запись уже была подтверждена сервером. Локальная копия очищена."
            : "Сохранённая запись обработана и подтверждена.",
        );
      }
      await loadMemories();
    } catch (error) {
      setNotice(
        "Передача не завершена. Запись остаётся на устройстве и её можно повторно отправить без нового наговаривания.",
      );
    } finally {
      replayingRef.current = false;
      setBusy(false);
      setVoiceState("off");
      await refreshPendingSources();
    }
  }

  function liveStartBody() {
    return {
      ...(nativeVersion ? { client_version: nativeVersion } : {}),
      client_timezone: clientTimezone,
    };
  }

  async function recoverLiveConversation() {
    const client = clientRef.current;
    const current = conversationRef.current;
    if (
      !client
      || !current
      || userStoppedVoiceRef.current
      || !navigator.onLine
      || client.sessionId
      || client.starting
    ) {
      return;
    }
    try {
      await client.start({
        url: `/api/live/${current.id}/sessions`,
        body: liveStartBody(),
        microphone: true,
        captureDuringStart: true,
        authorize: async () => {},
      });
      if (client.sessionId) setNotice("Разговор восстановлен.");
    } catch {
      setNotice("Не удалось автоматически восстановить Live. Нажмите микрофон, чтобы продолжить.");
    }
  }

  async function toggleVoice() {
    const client = clientRef.current;
    if (!client || !boot || replayingRef.current) return;
    if (offlineCaptureRef.current) {
      await stopOfflineCapture();
      return;
    }
    const active = voiceState !== "off" && voiceState !== "start_error" && voiceState !== "connection_error";
    if (active || client.sessionId || client.starting) {
      userStoppedVoiceRef.current = true;
      client.stop({ reason: "user_stop" });
      setWait(null);
      return;
    }
    userStoppedVoiceRef.current = false;
    setBusy(true);
    setNotice(null);
    setMicrophoneSettingsAvailable(false);
    try {
      if (!networkOnline) {
        await startOfflineCapture();
        return;
      }
      const current = await ensureConversation();
      await client.start({
        url: `/api/live/${current.id}/sessions`,
        body: liveStartBody(),
        microphone: true,
        captureDuringStart: true,
        authorize: async () => {},
      });
      if (!client.sessionId && !navigator.onLine) {
        await startOfflineCapture();
      }
    } catch (error) {
      client.stop({ reason: "start_error" });
      setNotice(friendlyStartError(error));
      setVoiceState("off");
    } finally {
      if (!offlineCaptureRef.current) setBusy(false);
      else setBusy(false);
    }
  }

  function openAndroidMicrophoneSettings() {
    if (!isAndroidApp) return;
    window.location.href = "projectshub://settings/microphone";
  }

  async function openMemory() {
    try {
      await loadMemories();
      setMemoryOpen(true);
      setEventOpen(false);
      setContextOpen(false);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "Не удалось прочитать память.");
    }
  }

  async function openEvents() {
    try {
      await loadEventCards();
      setEventOpen(true);
      setMemoryOpen(false);
      setContextOpen(false);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "Не удалось прочитать готовность.");
    }
  }

  async function openBacklog() {
    try {
      await loadBacklog();
      setBacklogOpen(true);
      setEventOpen(false);
      setMemoryOpen(false);
      setContextOpen(false);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "Не удалось прочитать backlog.");
    }
  }

  async function changeTaskState(
    taskId: string,
    state: "accepted" | "done" | "snoozed" | "rejected",
  ) {
    if (!boot) return;
    try {
      await setTaskState(taskId, boot.workspace.id, state);
      await Promise.all([loadEventCards(), loadBacklog()]);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "Не удалось изменить задачу.");
    }
  }

  async function connectGitHub() {
    if (!boot || boot.role !== "owner" || githubBusy) return;
    setGitHubBusy(true);
    setNotice(null);
    try {
      if (!githubStatus?.configured) {
        const manifest = await startGitHubManifest(
          boot.workspace.id,
          conversationRef.current?.id ?? null,
        );
        window.location.assign(manifest.launch_url);
        return;
      }
      const result = await startGitHubInstall(
        boot.workspace.id,
        conversationRef.current?.id ?? null,
      );
      window.location.assign(result.install_url);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "Не удалось начать подключение GitHub.");
      setGitHubBusy(false);
    }
  }

  async function bindRepository(
    repositoryId: number,
    role: "project_docs" | "memory_store" | "external_owning_repo",
    accessMode: "read_only" | "app_managed_write",
  ) {
    if (!boot || githubBusy) return;
    setGitHubBusy(true);
    setNotice(null);
    try {
      await bindGitHubRepository(repositoryId, {
        workspace_id: boot.workspace.id,
        project_id: focusProject?.id ?? null,
        role,
        access_mode: accessMode,
        allowed_paths: [],
      });
      await refreshGitHub();
      setNotice("GitHub repository привязан к workspace policy.");
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "Не удалось привязать repository.");
    } finally {
      setGitHubBusy(false);
    }
  }

  function manageGitHubAccess() {
    const url = githubStatus?.installations.find(item => item.state === "active")?.html_url;
    if (url) window.location.assign(url);
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
          {authConfig?.mode === "first_party_invite" && (
            <>
              <label className="invite-label" htmlFor="invite-code">Одноразовый код</label>
              <input
                id="invite-code"
                className="invite-input"
                type="text"
                inputMode="text"
                autoCapitalize="none"
                autoCorrect="off"
                spellCheck={false}
                value={inviteCode}
                onChange={event => setInviteCode(event.target.value)}
                onKeyDown={event => {
                  if (event.key === "Enter" && !busy && inviteCode.trim()) void signIn();
                }}
                placeholder="Вставьте код из Telegram"
              />
              <p className="invite-help">Код выдаёт Projects Hub. Яндекс и сторонний auth‑посредник для входа не нужны.</p>
            </>
          )}
          <button
            className="primary-action"
            onClick={signIn}
            disabled={busy || (authConfig?.mode === "first_party_invite" && !inviteCode.trim())}
          >
            {busy
              ? "Вхожу…"
              : authConfig?.mode === "first_party_invite"
                ? "Войти по приглашению"
                : "Войти в пилот"}
          </button>
          {notice && <p className="notice" role="alert">{notice}</p>}
        </section>
      </main>
    );
  }

  const voiceActive = !["off", "start_error", "connection_error", "microphone_unavailable"].includes(voiceState);
  const showWork = Boolean(
    eventOpen || memoryOpen || backlogOpen || ((notice || wait) && chatMessages.length === 0)
  );
  const projectCount = Math.max(0, boot.projects.length - 1);
  const pendingCount = pendingSources.length;
  const voiceHeadline =
    voiceState === "off" && !networkOnline
      ? "Можно говорить без сети"
      : stateLabel[voiceState] ?? "Live";
  const voiceHint =
    voiceState === "offline_recording"
      ? "Нажмите, чтобы надёжно закрыть запись"
      : voiceState === "replaying"
        ? "Сохранённая речь уже передаётся"
        : !networkOnline
          ? "Запись останется на этом устройстве"
          : voiceActive
            ? "Нажмите, чтобы остановить"
            : "Нажмите и говорите";

  return (
    <main className="shell">
      <header className="context-wrap">
        <button
          className={"island context-island" + (contextOpen ? " is-open" : "")}
          onClick={() => {
            setContextOpen(value => {
              const next = !value;
              if (next && boot.role === "owner") void refreshGitHub();
              return next;
            });
            setMemoryOpen(false);
            setEventOpen(false);
            setBacklogOpen(false);
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
              <div className="sheet-actions">
                <button className="quiet-button" onClick={openBacklog}>Бэклог</button>
                <button className="quiet-button" onClick={openEvents}>Готовность</button>
                <button className="quiet-button" onClick={openMemory}>Память</button>
              </div>
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

            {boot.role === "owner" && (
              <div className="integration-card" aria-label="GitHub integration">
                <div className="integration-heading">
                  <div>
                    <p className="eyebrow">Интеграция</p>
                    <strong>GitHub</strong>
                  </div>
                  {githubStatus?.configured && githubStatus.installations.length > 0 ? (
                    <button className="quiet-button" onClick={manageGitHubAccess} disabled={githubBusy}>
                      Изменить доступ
                    </button>
                  ) : githubStatus?.configured || githubStatus?.bootstrap_available ? (
                    <button className="quiet-button" onClick={connectGitHub} disabled={githubBusy}>
                      Подключить GitHub
                    </button>
                  ) : null}
                </div>

                {!githubStatus ? (
                  <p className="integration-copy">Проверяю подключение…</p>
                ) : !githubStatus.configured ? (
                  <p className="integration-copy">
                    Подключение настраивается один раз владельцем. Остальным участникам проекта GitHub не потребуется.
                  </p>
                ) : githubStatus.installations.length === 0 ? (
                  <p className="integration-copy">
                    Владелец workspace устанавливает GitHub App и выбирает только нужные repositories.
                  </p>
                ) : (
                  <>
                    <p className="integration-copy">
                      {githubStatus.repositories.filter(item => item.state === "available").length} repositories доступны установке.
                    </p>
                    <div className="repository-list">
                      {githubStatus.repositories
                        .filter(item => item.state === "available")
                        .slice(0, 8)
                        .map(repository => (
                          <div className="repository-row" key={repository.repository_id}>
                            <div className="repository-copy">
                              <strong>{repository.full_name}</strong>
                              <span>
                                {repository.role === "unassigned"
                                  ? "ещё не привязан к продуктовой роли"
                                  : repository.role + " · " + repository.access_mode}
                              </span>
                            </div>
                            {repository.role === "unassigned" && (
                              <div className="repository-actions">
                                <button
                                  className="mini-action"
                                  onClick={() => bindRepository(repository.repository_id, "project_docs", "read_only")}
                                  disabled={githubBusy}
                                >
                                  Документы
                                </button>
                                <button
                                  className="mini-action"
                                  onClick={() => bindRepository(repository.repository_id, "memory_store", "app_managed_write")}
                                  disabled={githubBusy}
                                >
                                  Память
                                </button>
                                <button
                                  className="mini-action"
                                  onClick={() => bindRepository(repository.repository_id, "external_owning_repo", "read_only")}
                                  disabled={githubBusy}
                                >
                                  Внешний read-only
                                </button>
                              </div>
                            )}
                          </div>
                        ))}
                    </div>
                  </>
                )}
              </div>
            )}
          </section>
        )}
      </header>

      {chatMessages.length > 0 && (
        <section className="chat-canvas" aria-label="Диалог с Мирой">
          <div
            className="chat-thread"
            ref={chatScrollRef}
            onScroll={event => {
              const element = event.currentTarget;
              chatFollowRef.current =
                element.scrollHeight - element.scrollTop - element.clientHeight < 72;
            }}
          >
            <div className="chat-stack">
              {chatMessages.map((message, index) => (
                <div className={"chat-row " + message.role} key={index}>
                  <div
                    className={"chat-bubble " + message.role}
                    aria-label={(message.role === "user" ? "Вы" : "Мира") + ": " + message.text}
                  >
                    {message.text}
                  </div>
                </div>
              ))}
              {(wait || notice) && (
                <div className="chat-status" role={notice ? "alert" : undefined}>
                  {notice ?? (wait?.stage === "action" ? "Мира выполняет действие…" : "Мира думает…")}
                  {microphoneSettingsAvailable && (
                    <button className="mini-action microphone-settings-action" onClick={openAndroidMicrophoneSettings}>
                      Открыть настройки микрофона
                    </button>
                  )}
                </div>
              )}
            </div>
          </div>
        </section>
      )}

      <section className={"work-zone" + (showWork ? " visible" : "")} aria-live="polite">
        {showWork && (
          <article className="island work-island">
            {eventOpen ? (
              <div className="event-board">
                <div className="sheet-heading">
                  <div>
                    <p className="eyebrow">Готовность к событиям</p>
                    <h2>{focusProject?.name ?? "Проекты"}</h2>
                  </div>
                  <button className="quiet-button" onClick={() => setEventOpen(false)}>Закрыть</button>
                </div>
                {eventCards.length ? (
                  <div className="event-list">
                    {eventCards.map(card => (
                      <section className={"event-card" + (card.ready ? " is-ready" : "")} key={card.id}>
                        <div className="event-heading">
                          <div>
                            <span className="event-time">{card.starts_at}</span>
                            <strong>{card.title}</strong>
                          </div>
                          <span className="readiness-badge">
                            {card.ready ? "Готово" : `Осталось · ${card.incomplete_count}`}
                          </span>
                        </div>
                        <div className="checklist">
                          {card.checklist.map(item => (
                            <div className={"check-row" + (item.done ? " done" : "")} key={item.key}>
                              <span>{item.done ? "✓" : "○"}</span>
                              <p>{item.label}</p>
                            </div>
                          ))}
                        </div>
                        {card.tasks.length > 0 && (
                          <div className="task-list">
                            {card.tasks.map(task => (
                              <div className="task-row" key={task.id}>
                                <div>
                                  <strong>{task.title}</strong>
                                  <span>{task.state}{task.deadline ? " · " + task.deadline : ""}</span>
                                </div>
                                {task.state !== "done" && task.state !== "rejected" && (
                                  <div className="task-actions">
                                    {task.state === "proposed" && (
                                      <button className="mini-action" onClick={() => changeTaskState(task.id, "accepted")}>Принять</button>
                                    )}
                                    <button className="mini-action" onClick={() => changeTaskState(task.id, "done")}>Готово</button>
                                    <button className="mini-action" onClick={() => changeTaskState(task.id, "snoozed")}>Отложить</button>
                                    <button className="mini-action" onClick={() => changeTaskState(task.id, "rejected")}>Отказаться</button>
                                  </div>
                                )}
                              </div>
                            ))}
                          </div>
                        )}
                      </section>
                    ))}
                  </div>
                ) : (
                  <p className="empty-copy">Событий с checklist пока нет. Создайте событие голосом.</p>
                )}
              </div>
            ) : backlogOpen ? (
              <div className="backlog-board">
                <div className="sheet-heading">
                  <div>
                    <p className="eyebrow">Бэклог</p>
                    <h2>{focusProject?.name ?? "Проекты"}</h2>
                  </div>
                  <button className="quiet-button" onClick={() => setBacklogOpen(false)}>Закрыть</button>
                </div>

                {developmentAccess === true && codexStatus && (
                  <section className="development-status">
                    <div className="development-heading">
                      <div>
                        <span>Codex</span>
                        <strong>
                          {typeof codexStatus.remaining_percent === "number"
                            ? `Остаток · ${Math.round(codexStatus.remaining_percent)}%`
                            : "Лимит неизвестен"}
                        </strong>
                      </div>
                      <span className={codexStatus.eligible ? "readiness-badge is-ok" : "readiness-badge"}>
                        {codexStatus.eligible ? "можно запускать" : "резерв / недоступен"}
                      </span>
                    </div>
                    {!codexStatus.profile.catalog_available && codexStatus.models.length > 0 && (
                      <div className="model-list">
                        <span>Owner profile сейчас недоступен. Доступны:</span>
                        {codexStatus.models.slice(0, 6).map(model => (
                          <code key={model.id}>
                            {model.id} · {model.default_reasoning_effort ?? model.reasoning_efforts[0] ?? "default"}
                          </code>
                        ))}
                      </div>
                    )}
                    {developmentExecution && (
                      <div className="execution-card">
                        <div>
                          <strong>
                            {developmentExecution.phase_detail
                              || developmentStageLabel[developmentExecution.phase]
                              || developmentExecution.phase
                              || developmentExecution.status}
                          </strong>
                          <span>
                            {developmentExecution.model_profile}
                            {typeof developmentExecution.quota_remaining_percent === "number"
                              ? ` · остаток ${Math.round(developmentExecution.quota_remaining_percent)}%`
                              : ""}
                          </span>
                          <span>Технически: {developmentExecution.status}</span>
                        </div>
                        {developmentExecution.stages?.length > 0 && (
                          <div className="execution-stages">
                            {developmentExecution.stages.map(stage => (
                              <div className={"execution-stage " + stage.status} key={stage.id}>
                                <div>
                                  <strong>
                                    {developmentStageLabel[stage.stage] ?? stage.stage}
                                    {stage.cycle > 0 ? " · цикл " + stage.cycle : ""}
                                  </strong>
                                  <span>{stage.model} · {stage.reasoning_effort}</span>
                                </div>
                                <div className="execution-stage-meta">
                                  <span>{stage.status}</span>
                                  {stage.review_verdict && <span>{stage.review_verdict}</span>}
                                  {typeof stage.token_usage?.totalTokens === "number" && (
                                    <span>{stage.token_usage.totalTokens.toLocaleString()} ток.</span>
                                  )}
                                </div>
                              </div>
                            ))}
                          </div>
                        )}
                        {Object.keys(developmentExecution.token_usage_by_model ?? {}).length > 0 && (
                          <div className="execution-usage">
                            <span>Расход по моделям</span>
                            {Object.entries(developmentExecution.token_usage_by_model).map(([model, usage]) => (
                              <code key={model}>
                                {model}: {typeof usage.totalTokens === "number"
                                  ? usage.totalTokens.toLocaleString() + " ток."
                                  : "usage неизвестен"}
                              </code>
                            ))}
                          </div>
                        )}
                        {developmentExecution.result_summary && (
                          <p>{developmentExecution.result_summary}</p>
                        )}
                        {developmentExecution.error_code && (
                          <p className="notice">Ошибка: {developmentExecution.error_code}</p>
                        )}
                      </div>
                    )}
                  </section>
                )}

                {backlogTasks.length ? (
                  <div className="backlog-list">
                    {backlogTasks.map(task => (
                      <section className="backlog-task" key={task.id}>
                        <div>
                          <strong>{task.title}</strong>
                          <span>
                            {task.state}
                            {task.deadline ? " · " + task.deadline : ""}
                          </span>
                          {task.description && <p>{task.description}</p>}
                        </div>
                        {task.state !== "done" && task.state !== "rejected" && (
                          <div className="task-actions">
                            {task.state === "proposed" && (
                              <button className="mini-action" onClick={() => changeTaskState(task.id, "accepted")}>Принять</button>
                            )}
                            <button className="mini-action" onClick={() => changeTaskState(task.id, "done")}>Готово</button>
                            <button className="mini-action" onClick={() => changeTaskState(task.id, "snoozed")}>Отложить</button>
                          </div>
                        )}
                      </section>
                    ))}
                  </div>
                ) : (
                  <p className="empty-copy">В бэклоге текущего проекта пока нет задач. Можно добавить задачу голосом.</p>
                )}

                {developmentAccess === true && (
                  <p className="sheet-footnote">
                    Чтобы выполнить задачу через Codex, скажите Мире, какую существующую задачу или набор задач запустить. Само добавление задачи в бэклог разработку не запускает.
                  </p>
                )}
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
            ) : wait ? (
              <div className="wait-card">
                <p className="eyebrow">{wait.stage === "action" ? "Выполняю действие" : "Live думает"}</p>
                <strong>{formatWait(wait)}</strong>
                <p>{wait.can_restart ? "Можно остановить и начать снова — источник останется сохранён." : "Можно остановить в любой момент."}</p>
              </div>
            ) : (
              <div className="notice-card">
                <p className="eyebrow">Состояние</p>
                <p>{notice}</p>
                {microphoneSettingsAvailable && (
                  <button className="mini-action microphone-settings-action" onClick={openAndroidMicrophoneSettings}>
                    Открыть настройки микрофона
                  </button>
                )}
              </div>
            )}
          </article>
        )}
      </section>

      {(chatMessages.length > 0 || memories.length > 0 || eventCards.length > 0 || (pendingCount > 0 && networkOnline)) && !voiceActive && (
        <nav className="action-islands" aria-label="Контекстные действия">
          {pendingCount > 0 && networkOnline && (
            <button className="island action-pill" onClick={deliverSavedSource} disabled={busy}>
              {pendingCount === 1 ? "Передать запись" : `Передать записи · ${pendingCount}`}
            </button>
          )}
          {eventCards.length > 0 && <button className="island action-pill" onClick={openEvents}>Готовность</button>}
          {memories.length > 0 && <button className="island action-pill" onClick={openMemory}>Память</button>}
          {chatMessages.length > 0 && (
            <button
              className="island action-pill"
              onClick={() => {
                setChatMessages([]);
                userTranscriptIndex.current = -1;
                assistantTranscriptIndex.current = -1;
              }}
            >
              Очистить диалог
            </button>
          )}
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
            <strong>{voiceHeadline}</strong>
            <span>{voiceHint}</span>
          </div>
          <span className={"state-dot " + (voiceActive ? "live" : "")} />
        </div>
      </section>
    </main>
  );
}
