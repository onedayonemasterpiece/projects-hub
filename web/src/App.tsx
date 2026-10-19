import { createThemePreferences, renderTheme } from "./themePreferences.js";
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
  beginIdentityRequests,
  getPreferences,
  acknowledgePreference,
  bindGitHubRepository,
  getOrCreatePersonalConversation,
  getConversation,
  getPersonalTimeline,
  getPersonalTimelineMessage,
  upsertPersonalTimelineMessage,
  getGitHubStatus,
  getMemories,
  getEventCards,
  getTasks,
  getDevelopmentBacklog,
  getDevelopmentCodexStatus,
  getLatestDevelopmentExecution,
  getRecentCompletedDevelopment,
  getAuthConfig,
  exchangeInvite,
  login,
  setTaskState,
  startGitHubInstall,
  startGitHubManifest,
  type AuthConfig,
  type Bootstrap,
  type Conversation,
  type PersonalTimelineBlock,
  type PersonalTimelineMessage,
  type GitHubStatus,
  type MemoryItem,
  type EventCard,
  type TaskItem,
  type DevelopmentExecution,
  type CompletedDevelopment,
  type CodexStatus,
} from "./api";
import { replayLocalVoiceSource } from "./bufferedReplay";
import CollaborationTimeline from "./CollaborationTimeline";
import CollaborationQuestions from "./CollaborationQuestions";
import AnalysisResult from "./AnalysisResult";
import InlineBoard, { type BoardUiCommand } from "./InlineBoard";
import { recoverServerVoiceSource } from "./serverRecovery";
import { createNativeVoiceAdapter, type NativeVoiceBridge, type NativeVoiceState } from "./nativeVoiceBridge.js";
import { createNativeTimelineLoader, type NativeTimelineHint } from "./nativeTimelineLoader.js";
import { createNativeCaptionState, reduceNativeCaption } from "./nativeCaptionTimeline.js";
import { mergeTranscript, nativeVoiceFailure, reduceVoiceToolProgress, resolveTerminalVoiceState, selectProvisionalCaption, speechStartsNewUserBubble, voiceStartupFailureNotice, voiceStartupFailureState } from "./voiceUiContract.js";
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
type NativeTimelineContext = {
  actorId: string; workspaceId: string; conversationId: string; sessionId: string;
  owner: ReturnType<typeof createNativeVoiceAdapter>;
};
type CollaborationCard = { view: "activity" | "questions" | "note"; noteId?: string };
type CollaborationUiCommand = { kind: "collaboration"; action: "show" | "close"; view?: CollaborationCard["view"]; note_id?: string };
type ChatRole = "user" | "assistant";
type ChatMessage = {
  id: string;
  conversationId?: string;
  turnId: string;
  role: ChatRole;
  text: string;
  sourceId: string | null;
  transcriptRevision: number;
  revision: number;
  blocks: PersonalTimelineBlock[];
  awaitingTranscript?: boolean;
  provisionalCaption?: boolean;
  deliveryNote?: string;
};

const VOICE_TURN_PLACEHOLDER = "";

function opaqueTimelineId(prefix: "msg" | "turn") {
  return prefix + "_" + crypto.randomUUID().replaceAll("-", "");
}

function widgetMessageId(conversationId: string) {
  return "msg_widgets_" + conversationId;
}

function widgetTurnId(conversationId: string) {
  return "turn_widgets_" + conversationId;
}

function defaultWidgetMessage(conversationId: string): ChatMessage {
  return {
    id: widgetMessageId(conversationId),
    turnId: widgetTurnId(conversationId),
    role: "assistant",
    text: "",
    sourceId: null,
    transcriptRevision: 0,
    revision: 1,
    // Never manufacture UI cards on app launch.
    blocks: [],
  };
}

function timelineFingerprint(message: ChatMessage) {
  return JSON.stringify({
    id: message.id,
    turnId: message.turnId,
    role: message.role,
    text: message.text,
    sourceId: message.sourceId,
    transcriptRevision: message.transcriptRevision,
    revision: message.revision,
    blocks: message.blocks,
  });
}

function timelineMessageFromServer(message: PersonalTimelineMessage): ChatMessage {
  return {
    id: message.id,
    conversationId: message.conversation_id,
    turnId: message.turn_id,
    role: message.role,
    text: message.text,
    sourceId: message.source_id,
    transcriptRevision: message.transcript_revision,
    revision: message.revision,
    blocks: message.blocks,
  };
}

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
  capacity_wait: "Ожидание ресурса",
  recovering: "Автовосстановление",
  validating: "CI / проверка",
  delivery_merge: "Объединение",
  delivery_main_ci: "Main CI / release",
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
  resource_denial: "Лимит Live исчерпан",
  provider_failure: "Ошибка Live-провайдера",
  connection_failure: "Соединение прервано",
  capture_error: "Ошибка записи",
  delayed: "Речь передаётся с задержкой",
  recording_only: "Записываю на устройстве",
  stopping: "Завершаю запись…",
  native_unsupported: "Голосовой модуль недоступен",
};

const inactiveVoiceStates = new Set([
  "off",
  "start_error",
  "connection_error",
  "microphone_unavailable",
  "resource_denial",
  "provider_failure",
  "connection_failure",
  "capture_error",
  "native_unsupported",
]);

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

function friendlyStartError(error: unknown) {
  const liveNotice = voiceStartupFailureNotice(error);
  if (liveNotice) return liveNotice;
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
  const [identityGeneration, setIdentityGeneration] = useState(0);
  const [identityNotice, setIdentityNotice] = useState<string | null>(null);
  const resetIdentity = useCallback((message: string | null = null) => {
    setIdentityNotice(message);
    setIdentityGeneration(value => value + 1);
  }, []);
  // Disposing this subtree discards every private object and pending React update.
  return <ActorApp key={identityGeneration} resetIdentity={resetIdentity} initialNotice={identityNotice} />;
}

function ActorApp({ resetIdentity, initialNotice }: { resetIdentity: (message?: string | null) => void; initialNotice: string | null }) {
  const activeIdentity = useRef(true);
  const identityAbort = useRef(new AbortController());
  const isAndroidApp = navigator.userAgent.includes("ProjectsHubAndroid/");
  const nativeBridge = useMemo(() => (window as Window & { projectsHubVoice?: NativeVoiceBridge }).projectsHubVoice, []);
  const nativeSurface = isAndroidApp || Boolean(nativeBridge);
  const nativeVoiceRef = useRef<ReturnType<typeof createNativeVoiceAdapter> | null>(null);
  const nativeStateRef = useRef<NativeVoiceState | null>(null);
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
  const clientInstanceId = useMemo(() => {
    const key = "projects-hub-client-instance";
    const current = sessionStorage.getItem(key);
    if (current && /^[0-9A-Za-z._:-]{8,128}$/.test(current)) return current;
    const created = "web_" + crypto.randomUUID().replaceAll("-", "");
    sessionStorage.setItem(key, created);
    return created;
  }, []);
  const setAndroidVoiceAudioFocus = useCallback((enabled: boolean) => {
    if (!isAndroidApp) return;
    window.location.href = enabled
      ? "projectshub://audio/focus/acquire"
      : "projectshub://audio/focus/release";
  }, [isAndroidApp]);
  const [boot, setBoot] = useState<Bootstrap | null>(null);
  const [authConfig, setAuthConfig] = useState<AuthConfig | null>(null);
  const [authReady, setAuthReady] = useState(false);
  const [conversation, setConversation] = useState<Conversation | null>(null);
  const [voiceState, setVoiceState] = useState("off");
  const [voiceClientReady, setVoiceClientReady] = useState(false);
  const [nativeSessionId, setNativeSessionId] = useState<string | null>(null);
  const [chatMessages, setChatMessages] = useState<ChatMessage[]>([]);
  const [timelineLoadedConversationId, setTimelineLoadedConversationId] = useState<string | null>(null);
  const [collaborationRefresh, setCollaborationRefresh] = useState(0);
  const [boardProjectId, setBoardProjectId] = useState<string | null>(null);
  const [boardCommand, setBoardCommand] = useState<BoardUiCommand | null>(null);
  const [collaborationCard, setCollaborationCard] = useState<CollaborationCard | null>(null);
  const [interimInputTranscript, setInterimInputTranscript] = useState("");
  const [nativeCaptionUnavailable, setNativeCaptionUnavailable] = useState(false);
  const nativeCaptionRef = useRef(createNativeCaptionState());
  const [inputTranscriptSeen, setInputTranscriptSeen] = useState(false);
  const [speechActive, setSpeechActive] = useState(false);
  const [speechPending, setSpeechPending] = useState(false);
  const [playbackProblem, setPlaybackProblem] = useState<string | null>(null);
  const [recoverableSourceId, setRecoverableSourceId] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(initialNotice);
  const [microphoneSettingsAvailable, setMicrophoneSettingsAvailable] = useState(false);
  const [wait, setWait] = useState<WaitState>(null);
  const [toolProgress, setToolProgress] = useState<null | { id: string; elapsed_ms: number }>(null);
  const [memories, setMemories] = useState<MemoryItem[]>([]);
  const [eventCards, setEventCards] = useState<EventCard[]>([]);
  const [backlogTasks, setBacklogTasks] = useState<TaskItem[]>([]);
  const [codexStatus, setCodexStatus] = useState<CodexStatus | null>(null);
  const [developmentExecution, setDevelopmentExecution] = useState<DevelopmentExecution | null>(null);
  const [completedDevelopment, setCompletedDevelopment] = useState<CompletedDevelopment[]>([]);
  const [completedDevelopmentActor, setCompletedDevelopmentActor] = useState<string | null>(null);
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
  const themeRef = useRef(createThemePreferences({
    render: renderTheme,
    acknowledge: acknowledgePreference,
    onStatus: (message: string) => setNotice(message),
  }));
  const clientRef = useRef<LiveClient | null>(null);
  const clearPrivateIdentity = useCallback((reason: string, message: string | null = null) => {
    if (!activeIdentity.current) return;
    activeIdentity.current = false;
    identityAbort.current.abort();
    void nativeVoiceRef.current?.stop({ reason: reason === "actor_change" ? "account_changed" : "logout" }).catch(() => {});
    clientRef.current?.stop({ reason });
    nativeCaptionRef.current = createNativeCaptionState();
    setInterimInputTranscript("");
    setNativeCaptionUnavailable(false);
    setToolProgress(null);
    setWait(null);
    setAndroidVoiceAudioFocus(false);
    void themeRef.current.reset();
    setCompletedDevelopment([]);
    setCompletedDevelopmentActor(null);
    setBoot(null); setConversation(null); conversationRef.current = null;
    setCollaborationCard(null);
    resetIdentity(message);
  }, [resetIdentity, setAndroidVoiceAudioFocus]);
  useEffect(() => {
    const requestGeneration = beginIdentityRequests();
    const expired = (event: Event) => {
      if (event instanceof CustomEvent && event.detail?.requestGeneration === requestGeneration
          && themeRef.current.actor) clearPrivateIdentity("authentication_expired");
    };
    window.addEventListener("projects-hub-authentication-expired", expired);
    return () => window.removeEventListener("projects-hub-authentication-expired", expired);
  }, [clearPrivateIdentity]);
  const hydratePreferences = useCallback(async (value: Bootstrap) => {
    if (!activeIdentity.current) return;
    if (themeRef.current.actor && themeRef.current.actor !== value.actor.id) {
      clearPrivateIdentity("actor_change");
      return;
    }
    if (themeRef.current.actor !== value.actor.id) {
      clientRef.current?.stop({ reason: "actor_change" });
      setConversation(null); conversationRef.current = null;
      await themeRef.current.reset(value.actor.id);
    }
    if (activeIdentity.current) await themeRef.current.apply(value.preferences);
  }, [clearPrivateIdentity]);
  const applyAuthenticatedBootstrap = useCallback(async (value: Bootstrap, isCurrent: () => boolean = () => true) => {
    if (!activeIdentity.current || !isCurrent()) return;
    await hydratePreferences(value);
    const currentIdentity = () => activeIdentity.current && isCurrent() && themeRef.current.actor === value.actor.id;
    if (!currentIdentity()) return;
    setBoot(value);
    const saved = localStorage.getItem("projects-hub-conversation");
    if (!saved) {
      // A fresh installation has no local pointer. The server owns the single
      // canonical personal conversation; a new login must hydrate it before
      // the first Live command, without making another project chat.
      const canonical = await getOrCreatePersonalConversation(value.workspace.id, null);
      if (!currentIdentity() || conversationRef.current) return;
      if (canonical.actor_id !== value.actor.id || canonical.workspace_id !== value.workspace.id) {
        throw new Error("Personal conversation identity mismatch.");
      }
      conversationRef.current = canonical;
      setConversation(canonical);
      localStorage.setItem("projects-hub-conversation", canonical.id);
      return;
    }
    try {
      const current = await getConversation(saved);
      if (!currentIdentity() || localStorage.getItem("projects-hub-conversation") !== saved
          || conversationRef.current) return;
      if (current.id === saved && current.workspace_id === value.workspace.id && current.actor_id === value.actor.id) {
        conversationRef.current = current;
        setConversation(current);
      } else {
        localStorage.removeItem("projects-hub-conversation");
      }
    } catch (error) {
      if (!currentIdentity() || localStorage.getItem("projects-hub-conversation") !== saved) return;
      if (error instanceof ApiError && [403, 404].includes(error.status)) {
        localStorage.removeItem("projects-hub-conversation");
      } else {
        throw error;
      }
    }
  }, [hydratePreferences]);
  const reconcilePreferences = useCallback(() => {
    const actor = themeRef.current.actor;
    if (!actor) return;
    void getPreferences().then(async value => {
      if (!activeIdentity.current || themeRef.current.actor !== actor) return;
      if (value.actor_id !== actor) {
        clearPrivateIdentity("actor_change");
        return;
      }
      return themeRef.current.apply(value);
    }).catch(error => {
      if (error instanceof ApiError && error.status === 401 && themeRef.current.actor === actor) {
        clearPrivateIdentity("authentication_expired");
      }
    });
  }, [clearPrivateIdentity]);
  useEffect(() => {
    const resume = () => { if (document.visibilityState === "visible") reconcilePreferences(); };
    window.addEventListener("focus", resume);
    document.addEventListener("visibilitychange", resume);
    window.addEventListener("projects-hub-theme-resume", resume);
    return () => {
      window.removeEventListener("focus", resume);
      document.removeEventListener("visibilitychange", resume);
      window.removeEventListener("projects-hub-theme-resume", resume);
    };
  }, [reconcilePreferences]);
  useEffect(() => {
    activeIdentity.current = true;
    if (identityAbort.current.signal.aborted) identityAbort.current = new AbortController();
    return () => {
      activeIdentity.current = false;
      identityAbort.current.abort();
      // Seal captured audio instead of leaving capture alive across account changes.
      if (offlineCaptureRef.current) void stopOfflineCapture();
    };
  }, []);
  const currentSourceIdRef = useRef<string | null>(null);
  const currentTurnIdRef = useRef<string | null>(null);
  const timelinePersistedRef = useRef<Map<string, string>>(new Map());
  const timelineInFlightRef = useRef<Set<string>>(new Set());
  const userTranscriptIndex = useRef(-1);
  const nativeTimelineLoader = useRef<{
    request: (hint: NativeTimelineHint, context: NativeTimelineContext) => boolean;
    close: () => void;
  } | null>(null);
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
  const userTurnBoundaryPendingRef = useRef(false);
  const userTurnAwaitingFinalRef = useRef(false);
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
  }, [chatMessages, interimInputTranscript, collaborationCard]);

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
      setCompletedDevelopment([]);
      return;
    }

    try {
      const [latest, completed] = await Promise.all([
        getLatestDevelopmentExecution(boot.workspace.id, true),
        getRecentCompletedDevelopment(boot.workspace.id),
      ]);
      setDevelopmentAccess(true);
      setDevelopmentExecution(latest.execution);
      setCompletedDevelopment(completed.items);
      setCompletedDevelopmentActor(boot.actor.id);
      // Quota availability must never hide durable work or completed results.
      try {
        setCodexStatus(await getDevelopmentCodexStatus(boot.workspace.id));
      } catch {
        setCodexStatus(null);
      }
    } catch (error) {
      if (error instanceof ApiError && error.status === 403) {
        setDevelopmentAccess(false);
        setCodexStatus(null);
        setDevelopmentExecution(null);
        setCompletedDevelopment([]);
        return;
      }
      throw error;
    }
  }, [boot]);

  const refreshPendingSources = useCallback(async () => {
    if (!boot || !activeIdentity.current) return;
    const current = conversationRef.current;
    if (!current || current.actor_id !== boot.actor.id || current.workspace_id !== boot.workspace.id) {
      setPendingSources([]);
      return;
    }
    const sources = await listPendingVoiceSources(boot.workspace.id);
    if (!activeIdentity.current || themeRef.current.actor !== boot.actor.id
        || conversationRef.current?.id !== current.id) return;
    setPendingSources(sources.filter(source => source.conversation_id === current.id));
  }, [boot]);

  useEffect(() => {
    if (!boot || !conversation) return;
    void refreshPendingSources().catch(() => {});
  }, [boot, conversation?.id, refreshPendingSources]);

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
        const [latest, completed] = await Promise.all([
          getLatestDevelopmentExecution(boot.workspace.id, true),
          getRecentCompletedDevelopment(boot.workspace.id),
        ]);
        if (cancelled) return;
        setDevelopmentAccess(true);
        setDevelopmentExecution(latest.execution);
        setCompletedDevelopment(completed.items);
      setCompletedDevelopmentActor(boot.actor.id);
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
          setCompletedDevelopment([]);
        }
      }
    };

    const syncOnForeground = () => {
      if (document.visibilityState === "visible") void syncDevelopment();
    };
    void syncDevelopment();
    const timer = window.setInterval(syncDevelopment, 15_000);
    window.addEventListener("focus", syncOnForeground);
    document.addEventListener("visibilitychange", syncOnForeground);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
      window.removeEventListener("focus", syncOnForeground);
      document.removeEventListener("visibilitychange", syncOnForeground);
    };
  }, [boot, developmentAccess, isAndroidApp]);

  const updateWidgetBlocks = useCallback((
    update: (blocks: PersonalTimelineBlock[]) => PersonalTimelineBlock[],
  ) => {
    const current = conversationRef.current;
    if (!current) return;
    const id = widgetMessageId(current.id);
    setChatMessages(previous => {
      const messages = [...previous];
      const index = messages.findIndex(item => item.id === id);
      const base = index >= 0 ? messages[index] : defaultWidgetMessage(current.id);
      const blocks = update(base.blocks);
      if (index >= 0 && JSON.stringify(blocks) === JSON.stringify(base.blocks)) {
        return previous;
      }
      const next = {
        ...base,
        blocks,
        revision: index >= 0 ? base.revision + 1 : base.revision,
      };
      if (index >= 0) messages[index] = next;
      else messages.push(next);
      return messages;
    });
  }, []);

  const mergeChatMessage = useCallback((
    role: ChatRole,
    fragment: string,
    preferred: MutableRefObject<number>,
  ) => {
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
        const turnId = currentTurnIdRef.current ?? opaqueTimelineId("turn");
        currentTurnIdRef.current = turnId;
        messages.push({
          id: opaqueTimelineId("msg"),
          turnId,
          role,
          text: clean,
          sourceId: role === "user" ? currentSourceIdRef.current : null,
          transcriptRevision: role === "user" ? 1 : 0,
          revision: 1,
          blocks: [],
        });
        preferred.current = messages.length - 1;
        return messages;
      }
      const current = messages[index];
      if (role === "user" && current.awaitingTranscript) {
        // The primary conversational Live model is authoritative. Its final
        // input transcript replaces any provisional sidecar caption using the
        // same durable message/turn identity.
        messages[index] = {
          ...current,
          text: clean,
          sourceId: current.sourceId ?? currentSourceIdRef.current,
          transcriptRevision: current.transcriptRevision + 1,
          revision: current.revision + 1,
          awaitingTranscript: false,
          provisionalCaption: false,
          deliveryNote: undefined,
        };
        return messages;
      }
      messages[index] = {
        ...current,
        text: mergeTranscript(current.text, clean),
        transcriptRevision: role === "user"
          ? current.transcriptRevision + 1
          : current.transcriptRevision,
        revision: current.revision + 1,
      };
      return messages;
    });
  }, []);

  const settleCurrentVoiceBubble = useCallback((
    note?: string,
    keepAwaiting = false,
  ) => {
    setChatMessages(previous => {
      const index = userTranscriptIndex.current;
      if (
        index < 0
        || index >= previous.length
        || previous[index]?.role !== "user"
        || !previous[index]?.awaitingTranscript
      ) {
        return previous;
      }
      const messages = [...previous];
      const current = messages[index];
      const hasVisibleCaption = Boolean(current.provisionalCaption && current.text);
      messages[index] = {
        ...current,
        awaitingTranscript: keepAwaiting,
        deliveryNote: note
          ?? current.deliveryNote
          ?? (hasVisibleCaption ? undefined : "Текст не удалось отобразить"),
      };
      return messages;
    });
    if (!keepAwaiting) userTurnAwaitingFinalRef.current = false;
  }, []);

  const reserveUserVoiceBubble = useCallback(() => {
    userTurnAwaitingFinalRef.current = true;
    const turnId = opaqueTimelineId("turn");
    const placeholderId = opaqueTimelineId("msg");
    currentTurnIdRef.current = turnId;
    setChatMessages(previous => {
      const messages = [...previous];
      messages.push({
        id: placeholderId,
        conversationId: conversationRef.current?.id,
        turnId,
        role: "user",
        text: VOICE_TURN_PLACEHOLDER,
        sourceId: currentSourceIdRef.current,
        transcriptRevision: 0,
        revision: 1,
        blocks: [],
        awaitingTranscript: true,
      });
      userTranscriptIndex.current = messages.length - 1;
      return messages;
    });
  }, []);

  const applyCaptionToUserBubble = useCallback((fragment: string, _final: boolean) => {
    const clean = fragment.trim();
    if (!clean) return;
    setChatMessages(previous => {
      const messages = [...previous];
      const index = userTranscriptIndex.current;
      if (
        index < 0
        || index >= messages.length
        || messages[index]?.role !== "user"
        || !messages[index]?.awaitingTranscript
      ) {
        return previous;
      }
      // Google interim transcription is a dynamically updated hypothesis, so
      // replace it rather than appending fragments. The main Mira transcript
      // later replaces this entire caption. A final caption may also correct
      // an earlier hypothesis with a shorter result.
      const text = selectProvisionalCaption(messages[index].text, clean, _final);
      messages[index] = {
        ...messages[index],
        text,
        revision: messages[index].revision + 1,
        awaitingTranscript: true,
        provisionalCaption: true,
      };
      return messages;
    });
  }, []);

  const applyPreferenceEvent = useCallback((event: LiveEvent, isCurrent: () => boolean) => {
    if (event.type !== "preferences_changed" || !activeIdentity.current || !isCurrent()) return;
    const current = () => activeIdentity.current && isCurrent();
    void themeRef.current.apply(event, event, current).then(applied => {
      if (!applied && isCurrent()) reconcilePreferences();
    }).catch(() => {
      if (!current()) return;
      setNotice("Настройка сохранена, но применение на этом экране пока не подтверждено.");
      reconcilePreferences();
    });
  }, [reconcilePreferences]);

  const applyNativeCaption = useCallback((event: LiveEvent) => {
    if (!nativeSurface) return;
    const state = nativeVoiceRef.current?.state;
    const next = reduceNativeCaption(nativeCaptionRef.current,
      userStoppedVoiceRef.current ? { type: "caption_reset" } : event, {
        sessionId: state?.session_id ?? null, conversationId: conversationRef.current?.id ?? null,
      });
    if (next === nativeCaptionRef.current) return;
    nativeCaptionRef.current = next;
    setInterimInputTranscript(next.text);
    setNativeCaptionUnavailable(next.unavailable);
    if (next.text) setInputTranscriptSeen(true);
  }, [nativeSurface]);

  const applyNativeTimeline = useCallback((event: LiveEvent): boolean => {
    const raw = event.timeline_message;
    if (!raw || typeof raw !== "object") return false;
    const item = raw as PersonalTimelineMessage;
    if (item.conversation_id !== conversationRef.current?.id
        || typeof item.id !== "string" || typeof item.turn_id !== "string"
        || !["user", "assistant"].includes(item.role) || typeof item.text !== "string"
        || !Number.isSafeInteger(item.revision) || !Array.isArray(item.blocks)) return false;
    applyNativeCaption({ type: "caption_canonical", timeline_message: item });
    const canonical = timelineMessageFromServer(item);
    setChatMessages(previous => {
      const messages = [...previous];
      const index = messages.findIndex(message => message.id === item.id);
      if (index >= 0 && messages[index].revision > item.revision) return previous;
      if (index < 0) {
        messages.push(canonical);
      } else messages[index] = canonical;
      timelinePersistedRef.current.set(item.id, timelineFingerprint(canonical));
      return messages;
    });
    return true;
  }, [applyNativeCaption]);

  useEffect(() => {
    if (!nativeSurface || !boot || !nativeSessionId) return;
    const actorId = boot.actor.id;
    const workspaceId = boot.workspace.id;
    const loader = createNativeTimelineLoader<NativeTimelineContext, PersonalTimelineMessage>({
      load: (hint, signal) => getPersonalTimelineMessage(hint.conversation_id, workspaceId, hint.id, signal),
      isCurrent: context => activeIdentity.current && !userStoppedVoiceRef.current
        && context.actorId === actorId && context.workspaceId === workspaceId
        && conversationRef.current?.id === context.conversationId
        && nativeVoiceRef.current === context.owner
        && context.owner.state?.session_id === context.sessionId
        && context.owner.state?.conversation_id === context.conversationId,
      apply: message => {
        applyNativeTimeline({ type: message.role === "user" ? "input_transcript" : "output_transcript",
          timeline_message: message });
      },
      onError: () => setNotice("Текст сохранён на сервере. Не удалось полностью загрузить сообщение; откройте разговор снова после восстановления связи."),
    });
    nativeTimelineLoader.current = loader;
    return () => {
      loader.close();
      if (nativeTimelineLoader.current === loader) nativeTimelineLoader.current = null;
    };
  }, [nativeSurface, boot?.actor.id, boot?.workspace.id, nativeSessionId, applyNativeTimeline]);

  const applyLiveEvent = useCallback((event: LiveEvent) => {
    if (!activeIdentity.current) return;
    setToolProgress(current => reduceVoiceToolProgress(current, event));
    if (event.type === "tool_progress" && event.status === "pending") setSpeechPending(false);
    const timelineRef = event.timeline_ref;
    if (nativeSurface && (event.type === "input_transcript" || event.type === "output_transcript")
        && timelineRef && typeof timelineRef === "object") {
      const hint = timelineRef as NativeTimelineHint;
      const owner = nativeVoiceRef.current;
      const state = owner?.state;
      const currentConversation = conversationRef.current;
      if (owner && state?.session_id && currentConversation
          && hint.conversation_id === currentConversation.id) {
        if (event.type === "input_transcript") {
          setInputTranscriptSeen(true);
          userTurnAwaitingFinalRef.current = false;
          turnHasInput.current = true;
        }
        nativeTimelineLoader.current?.request(hint, {
          actorId: currentConversation.actor_id, workspaceId: currentConversation.workspace_id,
          conversationId: hint.conversation_id, sessionId: state.session_id, owner,
        });
      }
      return;
    }
    if (nativeSurface && event.type.startsWith("caption_")) {
      applyNativeCaption(event);
      return;
    }
    if (event.type === "caption_interim_transcript" && typeof event.text === "string") {
      setInputTranscriptSeen(true);
      setInterimInputTranscript("");
      applyCaptionToUserBubble(event.text, false);
    } else if (event.type === "caption_final_transcript" && typeof event.text === "string") {
      setInputTranscriptSeen(true);
      setInterimInputTranscript("");
      applyCaptionToUserBubble(event.text, true);
    } else if (event.type === "caption_unavailable") {
      // Optional captions do not change Mira's voice or canonical messages.
    } else if (event.type === "interim_input_transcript" && typeof event.text === "string") {
      if (nativeSurface) {
        // Main provider provisional text has no accepted-audio correlation.
        // Native uses only scoped sidecar captions and identified timeline rows.
        return;
      }
      if (userTurnBoundaryPendingRef.current) {
        userTurnBoundaryPendingRef.current = false;
        userTranscriptIndex.current = -1;
        assistantTranscriptIndex.current = -1;
        turnHasInput.current = false;
      }
      setInputTranscriptSeen(true);
      if (userTranscriptIndex.current >= 0) {
        setInterimInputTranscript("");
        applyCaptionToUserBubble(event.text, false);
      } else {
        setInterimInputTranscript(event.text.trim());
      }
    } else if (event.type === "input_transcript" && typeof event.text === "string") {
      setInputTranscriptSeen(true);
      userTurnAwaitingFinalRef.current = false;
      if (!turnHasInput.current) turnHasInput.current = true;
      if (nativeSurface) {
        applyNativeTimeline(event);
      } else {
        setInterimInputTranscript("");
        mergeChatMessage("user", event.text, userTranscriptIndex);
      }
    } else if (event.type === "output_transcript" && typeof event.text === "string") {
      if (nativeSurface) {
        applyNativeTimeline(event);
      } else {
        mergeChatMessage("assistant", event.text, assistantTranscriptIndex);
      }
    } else if (event.type === "turn_complete" || event.type === "interrupted") {
      // Provider input transcription can arrive after the turn boundary.
      // Preserve the provisional text and current user bubble until either that
      // late final arrives or the first interim fragment of the next user turn.
      userTurnBoundaryPendingRef.current = true;
      turnHasInput.current = false;
    } else if (event.type === "tool_result" && event.status === "ok") {
      const toolResult = (
        event.result && typeof event.result === "object"
          ? event.result
          : null
      ) as { ui_command?: unknown } | null;
      const uiCommand = (
        toolResult?.ui_command && typeof toolResult.ui_command === "object"
          ? toolResult.ui_command
          : null
      ) as BoardUiCommand | CollaborationUiCommand | null;
      if (uiCommand && uiCommand.kind === "board") {
        if (uiCommand.action === "close") {
          updateWidgetBlocks(blocks => blocks.map(block =>
            block.kind === "board" ? { ...block, mode: "reference" } : block
          ));
          setBoardProjectId(null);
          setBoardCommand(null);
        } else if (uiCommand.project_id) {
          updateWidgetBlocks(blocks => [
            ...blocks.filter(block => block.kind !== "board"),
            { kind: "board", project_id: uiCommand.project_id!, mode: "active" },
          ]);
          setBoardProjectId(uiCommand.project_id);
          setBoardCommand(uiCommand);
        }
      }
      if (uiCommand?.kind === "collaboration") {
        if (uiCommand.action === "close") setCollaborationCard(null);
        else if (
          uiCommand.action === "show"
          && ["activity", "questions", "note"].includes(uiCommand.view ?? "")
          && (uiCommand.view !== "note" || uiCommand.note_id)
        ) setCollaborationCard({ view: uiCommand.view!, noteId: uiCommand.note_id });
      }
      if ([
        "project_note_create",
        "project_note_reply",
      ].includes(event.name ?? "")) {
        setCollaborationRefresh(value => value + 1);
      }
      if (event.name === "memory_commit_voice_source") {
        void loadMemories();
      }
      if ([
        "calendar_create_event_on_device",
        "event_readiness_set",
        "task_create_follow_up",
        "task_set_state",
      ].includes(event.name ?? "")) {
        void loadEventCards();
        if (["task_create_follow_up", "task_set_state"].includes(event.name ?? "")) {
          void loadBacklog();
        }
      }
      if ([
        "development_execute_backlog",
        "development_execution_status",
        "development_codex_status",
      ].includes(event.name ?? "")) {
        void loadBacklog();
      }
      if (event.name === "conversation_set_focus" && conversationRef.current) {
        void getConversation(conversationRef.current.id).then(setConversation);
        void loadEventCards();
      }
    } else if (event.type === "capability_transition_requested") {
      setNotice("Подключаю нужную возможность…");
    } else if (event.type === "capability_ready") {
      setNotice(null);
    } else if (event.type === "capability_transition_error" || event.type === "capability_transition_rejected") {
      setNotice("Не удалось подключить эту возможность. Можно продолжить разговор или остановить его.");
    } else if (event.type === "capability_unavailable" && event.code !== "NOT_CONFIGURED") {
      setNotice("Одна из дополнительных возможностей сейчас недоступна.");
    }
  }, [applyCaptionToUserBubble, applyNativeCaption, applyNativeTimeline, nativeSurface, loadBacklog, loadEventCards, loadMemories, mergeChatMessage, updateWidgetBlocks]);

  useEffect(() => {
    if (!boot || !conversation) {
      setTimelineLoadedConversationId(null);
      return;
    }
    let cancelled = false;
    const conversationId = conversation.id;
    setTimelineLoadedConversationId(null);
    void getPersonalTimeline(conversationId, boot.workspace.id)
      .then(result => {
        if (cancelled) return;
        const restored = result.items.map(timelineMessageFromServer);
        const persisted = new Map<string, string>();
        restored.forEach(message => persisted.set(message.id, timelineFingerprint(message)));

        // Preserve durable timeline history, but never resurrect presentation widgets.
        setCollaborationCard(null);
        setBoardProjectId(null);
        setBoardCommand(null);
        userTranscriptIndex.current = -1;
        assistantTranscriptIndex.current = -1;
        currentTurnIdRef.current = null;
        timelinePersistedRef.current = persisted;
        setChatMessages(previous => {
          if (!nativeSurface) return restored;
          // A native event can arrive while this HTTP history read is in flight.
          // Keep only this conversation and the newest revision of each message.
          const merged = new Map(restored.map(message => [message.id, message]));
          for (const message of previous) {
            if (message.conversationId !== conversationId) continue;
            const saved = merged.get(message.id);
            if (!saved || message.revision > saved.revision) merged.set(message.id, message);
          }
          return [...merged.values()];
        });
        setTimelineLoadedConversationId(conversationId);
      })
      .catch(error => {
        if (!cancelled) {
          setNotice(error instanceof Error ? error.message : "Не удалось восстановить личную ленту.");
        }
      });
    return () => {
      cancelled = true;
    };
  }, [boot?.workspace.id, conversation?.id, nativeSurface]);

  useEffect(() => {
    if (
      !boot
      || !conversation
      || timelineLoadedConversationId !== conversation.id
    ) return;
    let cancelled = false;

    const sync = async () => {
      for (const message of chatMessages) {
        // Native canonical text is already committed before its WSS event.
        // Only presentation widgets retain the existing browser write path.
        if (nativeSurface && message.id !== widgetMessageId(conversation.id)) continue;
        if (
          !message.text.trim()
          && message.blocks.length === 0
          && !message.sourceId
        ) continue;
        const fingerprint = timelineFingerprint(message);
        if (
          timelinePersistedRef.current.get(message.id) === fingerprint
          || timelineInFlightRef.current.has(message.id)
        ) continue;
        timelineInFlightRef.current.add(message.id);
        try {
          const saved = await upsertPersonalTimelineMessage(
            conversation.id,
            {
              id: message.id,
              workspace_id: boot.workspace.id,
              turn_id: message.turnId,
              role: message.role,
              text: message.text,
              source_id: message.sourceId,
              transcript_revision: message.transcriptRevision,
              revision: message.revision,
              blocks: message.blocks,
            },
          );
          if (cancelled) return;
          const canonical = timelineMessageFromServer(saved);
          timelinePersistedRef.current.set(
            canonical.id,
            timelineFingerprint(canonical),
          );
          if (canonical.revision > message.revision) {
            setChatMessages(previous => previous.map(item =>
              item.id === canonical.id ? { ...canonical } : item
            ));
          }
        } catch {
          // Keep the message dirty; the bounded sync loop retries without
          // changing the visible conversation or inventing another identity.
        } finally {
          timelineInFlightRef.current.delete(message.id);
        }
      }
    };

    void sync();
    const timer = window.setInterval(() => void sync(), 2_000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [
    boot?.workspace.id,
    conversation?.id,
    timelineLoadedConversationId,
    chatMessages,
    nativeSurface,
  ]);

  useEffect(() => {
    let cancelled = false;

    async function applyBootstrap(value: Bootstrap) {
      if (cancelled) return;
      setBoot(value);
      // Server-side create is idempotent for actor+workspace and therefore
      // recovers the same personal conversation on a new browser/device.
      const current = await getOrCreatePersonalConversation(value.workspace.id, null);
      if (cancelled) return;
      setConversation(current);
      conversationRef.current = current;
      localStorage.setItem("projects-hub-conversation", current.id);
    }

    async function initialize() {
      try {
        const config = await getAuthConfig();
        if (cancelled) return;
        setAuthConfig(config);

        try {
          await applyAuthenticatedBootstrap(await bootstrap(), () => !cancelled);
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
  }, [applyAuthenticatedBootstrap]);

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
    if (!boot || nativeSurface) return;
    void recoverInterruptedVoiceSources(boot.workspace.id)
      .then(refreshPendingSources)
      .catch(error => {
        setNotice(error instanceof Error ? error.message : "Не удалось восстановить локальные записи.");
      });
  }, [boot, nativeSurface, refreshPendingSources]);

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

  const applyVoiceTiming = useCallback((event: string) => {
    if (!activeIdentity.current) return;
    if (event === "speech_end") {
      setSpeechActive(false);
      setSpeechPending(true);
      return;
    }
    if (event === "first_output_audio") setSpeechPending(false);
    if (event === "audio_scheduled") setPlaybackProblem(null);
    if (!speechStartsNewUserBubble(event)) return;
    setToolProgress(current => reduceVoiceToolProgress(current, { type: event }));
    setSpeechActive(true);
    setSpeechPending(false);
    setInterimInputTranscript("");
    setInputTranscriptSeen(false);
    // Native VAD has no canonical turn identity. It controls voice status only;
    // the server timeline supplies the permanent message rows.
    if (nativeSurface) {
      applyNativeCaption({ type: "caption_local_speech_start" });
      return;
    }
    if (userTurnAwaitingFinalRef.current) settleCurrentVoiceBubble();
    userTurnBoundaryPendingRef.current = false;
    userTranscriptIndex.current = -1;
    assistantTranscriptIndex.current = -1;
    turnHasInput.current = false;
    reserveUserVoiceBubble();
  }, [nativeSurface, applyNativeCaption, reserveUserVoiceBubble, settleCurrentVoiceBubble]);

  useEffect(() => {
    if (!boot) {
      setVoiceClientReady(false);
      return;
    }
    setVoiceClientReady(false);
    if (nativeSurface) {
      let attached = true;
      if (!nativeBridge) {
        setVoiceState("native_unsupported");
        setNotice("Для голосовой связи требуется обновление приложения.");
        return;
      }
      const native = createNativeVoiceAdapter({
        bridge: nativeBridge,
        onState: state => {
          if (!attached || !activeIdentity.current) return;
          if (nativeStateRef.current?.session_id !== state.session_id
              || nativeStateRef.current?.conversation_id !== state.conversation_id
              || ["idle", "error", "unsupported"].includes(state.status)) {
            nativeCaptionRef.current = createNativeCaptionState();
            setInterimInputTranscript("");
            setNativeCaptionUnavailable(false);
          }
          nativeStateRef.current = state;
          setNativeSessionId(["idle", "error", "unsupported"].includes(state.status) ? null : state.session_id);
          currentSourceIdRef.current = state.source_id;
          const failure = nativeVoiceFailure(state.code ?? (state.status === "unsupported" ? "NATIVE_VOICE_UNSUPPORTED" : null));
          const mapped = state.status === "live" ? (state.playing ? "answering" : "listening")
            : state.status === "idle" ? "off"
              : state.status === "error" ? failure.state
                : state.status === "unsupported" ? "native_unsupported" : state.status;
          setVoiceState(mapped);
          if (state.status === "idle" || state.status === "error" || state.status === "unsupported") {
            setSpeechActive(false);
            setSpeechPending(false);
            setInterimInputTranscript("");
            setWait(null);
            setToolProgress(null);
          }
          if (failure.reauthenticate) {
            clearPrivateIdentity("authentication_expired", failure.notice);
            return;
          }
          if (state.status === "live") {
            setNotice(null);
            setMicrophoneSettingsAvailable(false);
          } else if (state.status === "delayed") {
            setNotice("Связь замедлилась. Речь сохраняется и передаётся с задержкой.");
          } else if (state.status === "recording_only") {
            setNotice("Ответы пока недоступны. Запись сохраняется на устройстве; уже записанные фрагменты будут доставлены после восстановления связи.");
          } else if (state.status === "error" || state.status === "unsupported") {
            setNotice(failure.notice);
            setMicrophoneSettingsAvailable(isAndroidApp && failure.microphoneSettings);
          }
          if (state.conversation_id && conversationRef.current?.id !== state.conversation_id) {
            const nativeConversationId = state.conversation_id;
            void getConversation(nativeConversationId).then(restored => {
              if (!attached || !activeIdentity.current || native.state?.conversation_id !== nativeConversationId) return;
              if (restored.actor_id !== boot.actor.id || restored.workspace_id !== boot.workspace.id) {
                clearPrivateIdentity("actor_change");
                return;
              }
              conversationRef.current = restored;
              setConversation(restored);
              localStorage.setItem("projects-hub-conversation", restored.id);
            }).catch(() => {});
          }
        },
        onEvent: event => {
          if (!attached || !activeIdentity.current || nativeVoiceRef.current !== native) return;
          const state = native.state;
          if (!state || (event.conversation_id && event.conversation_id !== state.conversation_id)
              || (event.session_id && event.session_id !== state.session_id)) return;
          if (event.type === "native_timing" && typeof event.stage === "string") {
            applyVoiceTiming(event.stage);
            return;
          }
          if (["speech_start", "speech_end", "first_output_audio", "audio_scheduled"].includes(event.type)) {
            applyVoiceTiming(event.type);
            return;
          }
          if (conversationRef.current?.id !== state.conversation_id) return;
          const current = () => attached && activeIdentity.current && nativeVoiceRef.current === native
            && native.state?.session_id === event.session_id && native.state?.conversation_id === event.conversation_id;
          applyPreferenceEvent(event as LiveEvent, current);
          applyLiveEvent(event as LiveEvent);
        },
        onError: error => {
          if (!attached || !activeIdentity.current) return;
          const failure = nativeVoiceFailure(error);
          if (failure.reauthenticate) {
            clearPrivateIdentity("authentication_expired", failure.notice);
          } else {
            // A UI/bridge failure does not override the native capture state or disable Stop.
            setNotice(failure.notice);
            setMicrophoneSettingsAvailable(isAndroidApp && failure.microphoneSettings);
          }
        },
      });
      nativeVoiceRef.current = native;
      void native.attach().then(result => {
        if (!attached || !activeIdentity.current) return;
        setVoiceClientReady(result.supported || result.state.capturing || result.state.playing);
        if (!result.supported && !result.state.capturing && !result.state.playing) {
          setVoiceState("native_unsupported");
          setNotice("На этом устройстве не удалось включить сжатую голосовую связь. Переписка остаётся доступной.");
        }
      }).catch(() => {
        if (!attached || !activeIdentity.current) return;
        setVoiceState("native_unsupported");
        setNotice("Не удалось связаться с голосовым модулем. Откройте приложение снова.");
      });
      return () => {
        attached = false;
        native.detach();
        if (nativeVoiceRef.current === native) nativeVoiceRef.current = null;
        setVoiceClientReady(false);
      };
    }
    const client = createLiveClient({
      transport: "wss",
      voiceControl: null,
      // Adaptive duplex rejects Mira playback echo while preserving sustained real barge-in.
      suppressCaptureDuringPlayback: "adaptive",
      // Provider speech-start proved unreliable in real Gemini 3.8 Live canaries,
      // so keep the proven explicit client activity boundary. Only endpoint latency
      // is tightened; the same Live model remains the sole speech/semantic authority.
      manualActivityDetection: true,
      continuousCapture: false,
      // Keep genuinely short commands responsive, but promote sustained speech
      // to a conservative tail before normal thinking pauses can split the turn.
      speechEndSilenceMs: 1200,
      // Require sustained speech onset before opening provider activity so taps,
      // keyboard clicks and finger snaps do not become semantic user turns.
      speechStartMs: 180,
      longSpeechEndSilenceMs: 2500,
      longSpeechAfterMs: 2500,
      onTiming: applyVoiceTiming,
      onState: (state, detail) => {
        if (!activeIdentity.current) return;
        const terminalReason = resolveTerminalVoiceState(state, detail);
        setVoiceState(terminalReason || state);
        if (terminalReason || inactiveVoiceStates.has(state) || state === "budget_wait") {
          setAndroidVoiceAudioFocus(false);
        }
        if (terminalReason || inactiveVoiceStates.has(state) || state === "reconnecting" || state === "budget_wait") {
          setSpeechActive(false);
          setSpeechPending(false);
          setToolProgress(null);
        }
        if (state === "starting") setPlaybackProblem(null);
        if (state === "listening" || state === "reconnecting") reconcilePreferences();
        if (terminalReason) {
          if (userTurnAwaitingFinalRef.current) {
            settleCurrentVoiceBubble(
              terminalReason === "connection_failure"
                ? "Связь прервалась до подтверждения фразы"
                : undefined,
              true,
            );
          }
          if (currentSourceIdRef.current) setRecoverableSourceId(currentSourceIdRef.current);
        }
        if (
          ["start_error", "connection_error", "microphone_unavailable"].includes(state)
          || (state === "off" && !terminalReason)
        ) {
          setInterimInputTranscript("");
        }
        if (state === "starting") setInputTranscriptSeen(false);
        if (state === "listening") {
          setAndroidVoiceAudioFocus(true);
          setSpeechPending(false);
          setNotice(null);
          setMicrophoneSettingsAvailable(false);
        }
      },
      onNotice: (kind, error) => {
        if (!activeIdentity.current) return;
        if (typeof error === "object" && error !== null && "status" in error && error.status === 401) {
          clearPrivateIdentity("authentication_expired");
          return;
        }
        if (kind === "playback_error") {
          setPlaybackProblem("Не удалось включить звук. Ответ Миры можно прочитать в переписке. Остановите и снова начните разговор, чтобы повторно включить звук.");
        } else if (kind === "microphone_error") {
          clientRef.current?.stop({ reason: "microphone_unavailable" });
          setMicrophoneSettingsAvailable(isAndroidApp);
          setNotice("Android/WebView не получил микрофон. Откройте настройки приложения и разрешите микрофон; если он уже разрешён, проверьте системный переключатель доступа к микрофону.");
        } else if (kind === "transport_error") {
          if (userTurnAwaitingFinalRef.current) {
            settleCurrentVoiceBubble("Связь прервалась до подтверждения фразы", true);
            if (currentSourceIdRef.current) setRecoverableSourceId(currentSourceIdRef.current);
          }
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
          if (userTurnAwaitingFinalRef.current) {
            settleCurrentVoiceBubble("Связь прервалась до подтверждения фразы", true);
            if (currentSourceIdRef.current) setRecoverableSourceId(currentSourceIdRef.current);
          }
          setNotice("Связь с Live нестабильна. Пытаюсь переподключиться…");
        } else if (kind === "resource_denial") {
          if (userTurnAwaitingFinalRef.current) settleCurrentVoiceBubble(undefined, true);
          setNotice(voiceStartupFailureNotice(error) ?? "Live остановилась из-за ресурсного лимита. Уже принятый голосовой источник сохранён и доступен для восстановления.");
        } else if (kind === "provider_failure") {
          if (userTurnAwaitingFinalRef.current) settleCurrentVoiceBubble(undefined, true);
          setNotice("Live-провайдер завершил сессию с ошибкой. Уже принятый голосовой источник сохранён.");
        } else if (kind === "start_error" && voiceStartupFailureNotice(error)) {
          // A failed new Live setup has not failed the user's task or erased
          // their conversation. Keep the existing chat and recovery action.
          setVoiceState(voiceStartupFailureState(error));
          setAndroidVoiceAudioFocus(false);
          setNotice(voiceStartupFailureNotice(error));
        } else if (kind === "capture_error") {
          if (userTurnAwaitingFinalRef.current) settleCurrentVoiceBubble(undefined, true);
          setNotice("Запись с микрофона прервалась. Уже подтверждённая часть источника сохранена.");
        }
        else if (kind === "event_gap") { reconcilePreferences(); setNotice("Интерфейс пропустил часть служебных событий. Источник на сервере сохраняется отдельно."); }
        else if (error) setNotice(friendlyStartError(error));
      },
      onWait: value => { if (activeIdentity.current) setWait(value); },
      onEvent: (event, generation) => {
        const current = () => activeIdentity.current && clientRef.current === client && client.generation === generation
          && client.sessionId === event.session_id && conversationRef.current?.id === event.conversation_id;
        applyPreferenceEvent(event, current);
        if (clientRef.current === client && client.generation === generation) applyLiveEvent(event);
      },
    });
    clientRef.current = client;
    setVoiceClientReady(true);
    return () => {
      setAndroidVoiceAudioFocus(false);
      client.stop({ reason: "ui_unmount" });
      if (clientRef.current === client) clientRef.current = null;
      setVoiceClientReady(false);
    };
  }, [applyLiveEvent, applyPreferenceEvent, applyVoiceTiming, reconcilePreferences, clearPrivateIdentity, boot, nativeSurface, nativeBridge, setAndroidVoiceAudioFocus, settleCurrentVoiceBubble]);

  async function signIn() {
    setBusy(true);
    setNotice(null);
    try {
      if (authConfig?.mode === "first_party_invite") {
        const value = inviteCode.trim();
        if (!value) throw new Error("Введите одноразовый код приглашения.");
        const authenticated = await exchangeInvite(value);
        await applyAuthenticatedBootstrap(authenticated);
        setInviteCode("");
        return;
      }
      if (authConfig?.mode === "disabled") {
        throw new Error("Вход сейчас недоступен.");
      }
      const value = await login();
      await applyAuthenticatedBootstrap(value);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "Не удалось войти.");
    } finally {
      setBusy(false);
    }
  }

  async function ensureConversation() {
    if (!boot) throw new Error("Нет workspace");
    if (conversationRef.current) return conversationRef.current;
    const created = await getOrCreatePersonalConversation(boot.workspace.id, null);
    if (!activeIdentity.current) throw new Error("Account changed.");
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
    if (!activeIdentity.current) return;
    const sink = createLocalPersistSink(source.id);
    const capture = createDurableMicrophoneCapture({
      persist: sink.persist,
      onError: error => {
        setAndroidVoiceAudioFocus(false);
        setNotice(error instanceof Error ? error.message : "Локальная запись остановлена.");
        setVoiceState("off");
      },
    });
    offlineSourceRef.current = { source, sink };
    offlineCaptureRef.current = capture;
    try {
      setAndroidVoiceAudioFocus(true);
      await capture.start();
      setVoiceState("offline_recording");
      setNotice("Связи нет. Речь сохраняется на этом устройстве и никуда не отправляется.");
    } catch (error) {
      offlineCaptureRef.current = null;
      offlineSourceRef.current = null;
      await acknowledgeDeliveredSource(source.id);
      setAndroidVoiceAudioFocus(false);
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
      setAndroidVoiceAudioFocus(false);
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
        signal: identityAbort.current.signal,
        onState: state => {
          if (state !== "off") setVoiceState(state);
        },
        onEvent: applyLiveEvent,
        onPreferenceEvent: applyPreferenceEvent,
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
      client_instance_id: clientInstanceId,
    };
  }

  function adoptPendingVoiceRecovery(started: any): boolean {
    const pending = Array.isArray(started?.pending_voice_sources)
      ? started.pending_voice_sources
      : [];
    const recoverable = pending.find((item: any) => {
      const verdict = String(item?.utterance_verdict ?? "");
      return verdict === "no_turn_closed" || verdict === "turn_closed_no_transcript";
    });
    const sourceId = typeof recoverable?.id === "string" ? recoverable.id : "";
    if (!sourceId) return false;
    setRecoverableSourceId(sourceId);
    return true;
  }

  async function recoverLiveConversation() {
    if (nativeSurface) return;
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
      setAndroidVoiceAudioFocus(true);
      const started = await client.start({
        url: `/api/live/${current.id}/sessions`,
        body: liveStartBody(),
        microphone: true,
        captureDuringStart: true,
        authorize: async () => {},
      });
      if (!activeIdentity.current) return;
      if (typeof started?.source_id === "string") currentSourceIdRef.current = started.source_id;
      adoptPendingVoiceRecovery(started);
      if (client.sessionId) setNotice("Разговор восстановлен.");
      else setNotice(previous => previous || "Не удалось восстановить Live. Нажмите микрофон, чтобы повторить подключение.");
    } catch {
      setAndroidVoiceAudioFocus(false);
      setNotice(previous => previous || "Не удалось восстановить Live. Нажмите микрофон, чтобы повторить подключение.");
    }
  }

  async function recoverFailedVoiceSource() {
    const sourceId = recoverableSourceId;
    const current = conversationRef.current;
    if (!sourceId || !current || !networkOnline || replayingRef.current) return;
    replayingRef.current = true;
    setBusy(true);
    setVoiceState("replaying");
    setNotice("Восстанавливаю сохранённую реплику той же Live-моделью…");
    try {
      await recoverServerVoiceSource(current.id, sourceId, {
        clientVersion: nativeVersion,
        clientTimezone,
        callbacks: {
          onState: state => {
            if (state !== "off") setVoiceState(state);
          },
          onEvent: applyLiveEvent,
          onNotice: message => setNotice(message),
        },
      });
      setRecoverableSourceId(null);
      setNotice("Реплика восстановлена. Нажмите микрофон и скажите «продолжи» — повторять содержание не нужно.");
    } catch {
      setNotice("Не удалось восстановить сохранённую реплику. Исходный PCM остаётся на сервере; можно повторить восстановление.");
    } finally {
      replayingRef.current = false;
      setBusy(false);
      setVoiceState("off");
    }
  }

  async function toggleVoice() {
    const native = nativeVoiceRef.current;
    if (nativeSurface) {
      if (!native || !boot) return;
      const state = native.state;
      // A slow WebView may still display the previous Stop state after the
      // native owner has already returned to idle. A tap intended as Stop
      // must never silently become a new microphone Start in that interval.
      if (!inactiveVoiceStates.has(voiceState) || state?.capturing || state?.playing
          || (state && ["starting", "live", "delayed", "recording_only", "stopping"].includes(state.status))) {
        userStoppedVoiceRef.current = true;
        nativeCaptionRef.current = createNativeCaptionState();
        setInterimInputTranscript("");
        setNativeCaptionUnavailable(false);
        setNativeSessionId(null);
        // Posting Stop precedes every network/flush await; the service releases
        // microphone and playback itself. A pending Start cannot re-enable it.
        void native.stop({ reason: "user" }).catch(() => {
          setNotice("Не получено подтверждение остановки. Используйте «Стоп» в уведомлении голосовой связи.");
        });
        setWait(null);
        setToolProgress(null);
        return;
      }
      setBusy(true);
      setNotice(null);
      userStoppedVoiceRef.current = false;
      try {
        const current = await ensureConversation();
        if (!activeIdentity.current || nativeVoiceRef.current !== native || userStoppedVoiceRef.current) return;
        await native.start({ conversationId: current.id });
      } catch (error) {
        if (!activeIdentity.current || nativeVoiceRef.current !== native || userStoppedVoiceRef.current) return;
        const failure = nativeVoiceFailure(error);
        if (failure.reauthenticate) {
          clearPrivateIdentity("authentication_expired", failure.notice);
        } else {
          // An unacknowledged Start can already own the microphone. Keep its Stop available.
          if (!native.state || ["idle", "error", "unsupported"].includes(native.state.status)) setVoiceState(failure.state);
          setNotice(failure.notice);
          setMicrophoneSettingsAvailable(isAndroidApp && failure.microphoneSettings);
        }
      } finally {
        setBusy(false);
      }
      return;
    }
    const client = clientRef.current;
    if (!client || !boot || replayingRef.current) return;
    if (offlineCaptureRef.current) {
      await stopOfflineCapture();
      return;
    }
    const active = !inactiveVoiceStates.has(voiceState);
    if (active || client.sessionId || client.starting) {
      userStoppedVoiceRef.current = true;
      setAndroidVoiceAudioFocus(false);
      client.stop({ reason: "user_stop" });
      setWait(null);
      setToolProgress(null);
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
      if (!activeIdentity.current) return;
      setAndroidVoiceAudioFocus(true);
      const started = await client.start({
        url: `/api/live/${current.id}/sessions`,
        body: liveStartBody(),
        microphone: true,
        captureDuringStart: true,
        authorize: async () => {},
      });
      if (!activeIdentity.current) return;
      if (typeof started?.source_id === "string") currentSourceIdRef.current = started.source_id;
      adoptPendingVoiceRecovery(started);
      if (!client.sessionId && !navigator.onLine) {
        await startOfflineCapture();
      }
    } catch (error) {
      client.stop({ reason: "start_error" });
      setAndroidVoiceAudioFocus(false);
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

  const voiceActive = !inactiveVoiceStates.has(voiceState)
    || Boolean(nativeSurface && (nativeStateRef.current?.capturing || nativeStateRef.current?.playing));
  const showWork = Boolean(
    eventOpen || memoryOpen || backlogOpen || ((notice || wait || toolProgress) && chatMessages.length === 0)
  );
  const projectCount = Math.max(0, boot.projects.length - 1);
  const pendingCount = nativeSurface ? 0 : pendingSources.length;
  const voiceHeadline =
    voiceState === "off" && !networkOnline
      ? "Можно говорить без сети"
      : stateLabel[voiceState] ?? "Live";
  const voiceHint =
    voiceState === "recording_only"
      ? "Речь сохраняется; ответы появятся после восстановления связи"
      : voiceState === "delayed"
        ? "Новая речь имеет приоритет при восстановлении связи"
        : voiceState === "native_unsupported"
          ? "Переписка остаётся доступной"
          : voiceState === "offline_recording"
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
      <AnalysisResult workspaceId={boot.workspace.id} />
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

      {((voiceActive || chatMessages.length > 0 || interimInputTranscript || playbackProblem) || boot) && (
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
              {chatMessages.filter(message =>
                Boolean(message.text || message.deliveryNote)
                || message.blocks.some(block =>
                  block.kind === "board" && block.mode === "active"
                  && boardProjectId === block.project_id
                )
              ).map(message => (
                <div className={"chat-row " + message.role} key={message.id}>
                  <div className={"chat-message " + message.role}>
                    {message.text && (
                      <div
                        className={
                          "chat-bubble " + message.role
                          + (message.awaitingTranscript ? " awaiting-transcript" : "")
                          + (message.provisionalCaption ? " sidecar-caption" : "")
                        }
                        aria-label={
                          (message.role === "user" ? "Вы" : "Мира") + ": " + message.text
                        }
                      >
                        {message.text}
                      </div>
                    )}
                    {message.deliveryNote && (
                      <span className="message-delivery-note">{message.deliveryNote}</span>
                    )}
                    {boot && message.blocks.map(block => {
                      if (block.kind === "collaboration_timeline"
                        || block.kind === "collaboration_questions") return null;
                      if (
                        block.kind === "board"
                        && block.mode === "active"
                        && conversation
                        && boardProjectId === block.project_id
                      ) {
                        return (
                          <InlineBoard
                            key={"board:" + block.project_id}
                            workspaceId={boot.workspace.id}
                            conversationId={conversation.id}
                            projectId={block.project_id}
                            clientInstanceId={clientInstanceId}
                            command={boardCommand}
                            onClose={() => {
                              updateWidgetBlocks(blocks => blocks.map(item =>
                                item.kind === "board"
                                  ? { ...item, mode: "reference" }
                                  : item
                              ));
                              setBoardProjectId(null);
                              setBoardCommand(null);
                            }}
                          />
                        );
                      }
                      return null;
                    })}
                  </div>
                </div>
              ))}
              {collaborationCard && boot && (
                <div className="chat-row assistant requested-card-row" aria-label="Карточка по просьбе Мире">
                  <div className="chat-message assistant">
                    <section className="collaboration-requested">
                      <div className="collaboration-requested-heading">
                        <strong>{collaborationCard.view === "questions" ? "Вопросы аналитики"
                          : collaborationCard.view === "note" ? "Проектная заметка"
                          : "Новости команды и результаты"}</strong>
                        <button className="quiet-button" onClick={() => setCollaborationCard(null)}
                          aria-label="Закрыть карточку">Закрыть</button>
                      </div>
                      {collaborationCard.view === "questions"
                        ? <CollaborationQuestions workspaceId={boot.workspace.id}
                            refreshKey={collaborationRefresh} />
                        : <CollaborationTimeline workspaceId={boot.workspace.id}
                            actorId={boot.actor.id} refreshKey={collaborationRefresh}
                            mode={collaborationCard.view === "note" ? "note" : "activity"}
                            noteId={collaborationCard.noteId} />}
                    </section>
                  </div>
                </div>
              )}
              {!nativeSurface && interimInputTranscript && (
                <div className="chat-row user interim" aria-live="polite">
                  <div
                    className="chat-bubble user interim"
                    aria-label={"Вы, сейчас: " + interimInputTranscript}
                  >
                    {interimInputTranscript}
                  </div>
                </div>
              )}
              {voiceActive && speechPending && !notice && !toolProgress && (
                <div className="chat-status" aria-live="polite">
                  Жду ответ Миры…
                </div>
              )}
              {playbackProblem && (
                <div className="chat-status" role="alert">{playbackProblem}</div>
              )}
              {(wait || notice || toolProgress) && (
                <div className="chat-status" role={notice ? "alert" : undefined} aria-live="polite">
                  {notice ?? (toolProgress ? "Мира ждёт результат…" : wait?.stage === "action" ? "Мира выполняет действие…" : "Мира думает…")}
                  {recoverableSourceId && networkOnline && !voiceActive && (
                    <button className="mini-action" onClick={recoverFailedVoiceSource} disabled={busy}>
                      Восстановить фразу
                    </button>
                  )}
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

                {developmentAccess === true && (
                  <section className="development-status">
                    {codexStatus && <div className="development-heading">
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
                    </div>}
                    {codexStatus && !codexStatus.profile.catalog_available && codexStatus.models.length > 0 && (
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

                {developmentAccess === true && completedDevelopmentActor === boot.actor.id && (
                  <section className="completed-developments" aria-label="Готовые задачи за последние семь дней">
                    <h3>Готовые задачи · 7 дней</h3>
                    {completedDevelopment.length ? completedDevelopment.map(item => (
                      <div className="completed-development-row" key={item.id}>
                        <strong>{item.titles.join(", ") || "Разработка"}</strong>
                        <span>{item.project_name} · {new Date(item.finished_at_ms).toLocaleDateString("ru-RU")}</span>
                        <span>{item.android_update ? "Обновление приложения выпущено" : "Результат готов к проверке"}</span>
                      </div>
                    )) : <p className="empty-copy">За последнюю неделю нет завершённых разработок.</p>}
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
            ) : toolProgress ? (
              <div className="wait-card" aria-live="polite">
                <p className="eyebrow">Мира ждёт результат</p>
                <p>Можно продолжить разговор или остановить его в любой момент.</p>
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
                {recoverableSourceId && networkOnline && (
                  <button className="mini-action" onClick={recoverFailedVoiceSource} disabled={busy}>
                    Восстановить фразу
                  </button>
                )}
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
        </nav>
      )}
      <section className="voice-dock">
        <div className={"island voice-island state-" + voiceState}>
          <button
            className={"voice-orb" + (voiceActive ? " active" : "")}
            onClick={toggleVoice}
            disabled={!voiceActive && (busy || !voiceClientReady)}
            aria-label={voiceActive ? "Остановить разговор" : "Начать голосовой разговор"}
          >
            <span className="pulse pulse-one" />
            <span className="pulse pulse-two" />
            <span className="orb-core"><MicIcon /></span>
          </button>
          <div className="voice-copy">
            <strong>{voiceHeadline}</strong>
            <span>{voiceHint}</span>
            {nativeSurface && interimInputTranscript && (
              <span className="native-voice-caption" role="status" aria-live="polite"
                aria-label="Предварительная расшифровка">
                Распознаю: {interimInputTranscript}
              </span>
            )}
            {nativeSurface && voiceActive && nativeCaptionUnavailable && (
              <span className="native-caption-unavailable" role="status">
                Промежуточные подписи недоступны.
              </span>
            )}
            {speechActive && networkOnline && (
              <button
                className="mini-action finish-turn-action"
                onClick={() => {
                  if (nativeVoiceRef.current) {
                    void nativeVoiceRef.current.finishTurn().then(accepted => {
                      if (accepted) { setSpeechActive(false); setSpeechPending(true); }
                    }).catch(() => setNotice("Не удалось подтвердить конец реплики. Запись продолжается."));
                    return;
                  }
                  if (clientRef.current?.finishTurn()) {
                    setSpeechActive(false);
                    setSpeechPending(true);
                  }
                }}
                disabled={busy}
              >
                Готово, отвечай
              </button>
            )}
          </div>
          <span className={"state-dot " + (voiceActive ? "live" : "")} />
        </div>
      </section>
    </main>
  );
}
