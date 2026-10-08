export type Project = { id: string; name: string; status: string };
export type Bootstrap = {
  actor: { id: string; display_name: string };
  workspace: { id: string; name: string };
  role: string;
  projects: Project[];
  preferences: { theme: "light" | "dark"; revision: number };
};
export type Conversation = {
  id: string;
  workspace_id: string;
  actor_id: string;
  focus_project_id: string | null;
  focus_project_name: string | null;
};
export type PersonalTimelineBlock =
  | { kind: "collaboration_timeline" }
  | { kind: "collaboration_questions" }
  | { kind: "board"; project_id: string; mode: "active" | "reference" };
export type PersonalTimelineMessage = {
  id: string;
  conversation_id: string;
  workspace_id: string;
  turn_id: string;
  role: "user" | "assistant";
  text: string;
  source_id: string | null;
  transcript_revision: number;
  revision: number;
  blocks: PersonalTimelineBlock[];
  created_at_ms: number;
  updated_at_ms: number;
};
export type AuthConfig =
  | { mode: "first_party_invite" }
  | { mode: "loopback_dev" | "disabled" };

export type MemoryItem = {
  id: string;
  source_id: string;
  project_id: string | null;
  title: string;
  kind: string;
  semantic_notes: string;
  revision: number;
  updated_at_ms: number;
};

export type SourceReceipt = {
  id: string;
  conversation_id: string;
  workspace_id: string;
  client_source_id: string | null;
  status: string;
  audio_bytes: number;
  audio_chunks: number;
  transcript_revision: number;
  captured_at_ms: number;
  updated_at_ms: number;
};

export type TaskItem = {
  id: string;
  project_id: string | null;
  event_card_id: string | null;
  title: string;
  description: string;
  assignee_role: string;
  deadline: string | null;
  kind?: string;
  state: "proposed" | "accepted" | "done" | "snoozed" | "rejected";
  updated_at_ms: number;
};

export type DevelopmentStage = {
  id: string;
  execution_id: string;
  stage: "design" | "implementation" | "review" | "rework" | "delivery";
  cycle: number;
  model: string;
  reasoning_effort: string;
  devcoveer_task_id: string | null;
  status: string;
  summary: string;
  review_verdict: "accepted" | "rework_required" | null;
  token_usage: {
    inputTokens?: number;
    cachedInputTokens?: number;
    outputTokens?: number;
    reasoningOutputTokens?: number;
    totalTokens?: number;
  } | null;
  started_at_ms: number | null;
  finished_at_ms: number | null;
  created_at_ms: number;
  updated_at_ms: number;
};

export type DevelopmentExecution = {
  id: string;
  project_id: string;
  task_ids: string[];
  project_hint: string;
  provider: string;
  model_profile: string;
  status: "starting" | "running" | "completed" | "failed" | "cancelled" | "blocked";
  phase: string;
  phase_detail: string;
  devcoveer_task_id: string | null;
  quality_task_id?: string | null;
  implementation_task_id?: string | null;
  review_cycle?: number;
  spec_path?: string | null;
  quota_remaining_percent: number | null;
  result_summary: string;
  error_code: string | null;
  stages: DevelopmentStage[];
  token_usage_by_model: Record<string, Record<string, number>>;
  created_at_ms: number;
  updated_at_ms: number;
  started_at_ms: number | null;
  finished_at_ms: number | null;
  update_check_recommended?: boolean;
};

export type CompletedDevelopment = {
  id: string;
  project_id: string;
  project_name: string;
  titles: string[];
  finished_at_ms: number;
  main_sha: string;
  summary: string;
  android_update: boolean;
};

export type CodexStatus = {
  status: string | null;
  observed_at: string | null;
  remaining_percent: number | null;
  eligible: boolean;
  reserve_percent: number | null;
  reason: string | null;
  profile: {
    requested: string | null;
    model: string | null;
    reasoning_effort: string | null;
    catalog_available: boolean;
  };
  models: Array<{
    id: string;
    display_name: string | null;
    reasoning_efforts: string[];
    default_reasoning_effort: string | null;
    availability: string | null;
  }>;
};

export type EventCard = {
  id: string;
  project_id: string | null;
  device_event_id: string | null;
  title: string;
  starts_at: string;
  event_type: "generic" | "podcast";
  status: string;
  ready: boolean;
  incomplete_count: number;
  checklist: Array<{ key: string; label: string; done: boolean }>;
  tasks: TaskItem[];
};

export type GitHubInstallation = {
  installation_id: number;
  account_id: number;
  account_login: string;
  account_type: string;
  html_url: string;
  repository_selection: string;
  state: string;
  suspended_at_ms: number | null;
  last_verified_at_ms: number;
};

export type RepositoryConnection = {
  id: string;
  installation_id: number;
  repository_id: number;
  full_name: string;
  default_branch: string;
  private: boolean;
  project_id: string | null;
  role: string;
  access_mode: string;
  allowed_paths: string[];
  state: string;
  installation_state: string;
};

export type GitHubStatus = {
  configured: boolean;
  bootstrap_available: boolean;
  installations: GitHubInstallation[];
  repositories: RepositoryConnection[];
};

export class ApiError extends Error {
  status: number;
  code?: string;
  constructor(status: number, message: string, code?: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

let identityRequestGeneration = 0;
export function beginIdentityRequests(): number { return ++identityRequestGeneration; }

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const requestGeneration = identityRequestGeneration;
  const response = await fetch(path, {
    credentials: "same-origin",
    cache: "no-store",
    ...options,
    headers: {
      ...(options.body ? { "content-type": "application/json" } : {}),
      ...(options.headers || {}),
    },
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 401) window.dispatchEvent(new CustomEvent("projects-hub-authentication-expired", {
      detail: { requestGeneration },
    }));
    const detail = payload?.error ?? payload?.detail ?? {};
    throw new ApiError(
      response.status,
      detail.message ?? detail.code ?? `HTTP ${response.status}`,
      detail.code,
    );
  }
  return payload as T;
}

export const getAuthConfig = () => api<AuthConfig>("/api/auth/config");

export const exchangeInvite = (token: string) =>
  api<Bootstrap>("/api/auth/invite", {
    method: "POST",
    body: JSON.stringify({ token }),
  });

export const login = () =>
  api<Bootstrap>("/api/dev/login", { method: "POST", body: JSON.stringify({}) });

export const getPreferences = () => api<Bootstrap["preferences"] & { actor_id: string }>("/api/preferences");
export const acknowledgePreference = (binding: {conversation_id: string; session_id: string; command_id: string}, value: Bootstrap["preferences"] & {web_status: "applied"; native_status: "applied" | "failed" | "unsupported" | "not_required"}) =>
  api(`/api/live/${encodeURIComponent(binding.conversation_id)}/sessions/${encodeURIComponent(binding.session_id)}/preferences/${encodeURIComponent(binding.command_id)}/applied`, {
    method: "POST", body: JSON.stringify(value),
  });

export const bootstrap = () => api<Bootstrap>("/api/bootstrap");

export const createConversation = (workspaceId: string, projectId?: string | null) =>
  api<Conversation>("/api/conversations", {
    method: "POST",
    body: JSON.stringify({ workspace_id: workspaceId, focus_project_id: projectId ?? null }),
  });

export const getOrCreatePersonalConversation = (
  workspaceId: string,
  projectId?: string | null,
) =>
  api<Conversation>("/api/conversations/personal", {
    method: "POST",
    body: JSON.stringify({ workspace_id: workspaceId, focus_project_id: projectId ?? null }),
  });

export const getConversation = (id: string) => api<Conversation>(`/api/conversations/${id}`);

export const getPersonalTimeline = (
  conversationId: string,
  workspaceId: string,
  limit = 200,
) => {
  const params = new URLSearchParams({
    workspace_id: workspaceId,
    limit: String(limit),
  });
  return api<{ items: PersonalTimelineMessage[] }>(
    `/api/conversations/${encodeURIComponent(conversationId)}/timeline?${params}`,
  );
};

export const upsertPersonalTimelineMessage = (
  conversationId: string,
  message: {
    id: string;
    workspace_id: string;
    turn_id: string;
    role: "user" | "assistant";
    text: string;
    source_id?: string | null;
    transcript_revision: number;
    revision: number;
    blocks: PersonalTimelineBlock[];
  },
) =>
  api<PersonalTimelineMessage>(
    `/api/conversations/${encodeURIComponent(conversationId)}/timeline/messages/${encodeURIComponent(message.id)}`,
    {
      method: "PUT",
      body: JSON.stringify({
        workspace_id: message.workspace_id,
        turn_id: message.turn_id,
        role: message.role,
        text: message.text,
        source_id: message.source_id ?? null,
        transcript_revision: message.transcript_revision,
        revision: message.revision,
        blocks: message.blocks,
      }),
    },
  );

export const getMemories = (workspaceId: string, projectId?: string | null) => {
  const params = new URLSearchParams({ workspace_id: workspaceId, limit: "12" });
  if (projectId) params.set("project_id", projectId);
  return api<{ items: MemoryItem[] }>(`/api/memories?${params}`);
};

export const getEventCards = (workspaceId: string, projectId?: string | null) => {
  const params = new URLSearchParams({ workspace_id: workspaceId, limit: "8" });
  if (projectId) params.set("project_id", projectId);
  return api<{ items: EventCard[] }>(`/api/event-cards?${params}`);
};

export const getTasks = (workspaceId: string, projectId?: string | null) => {
  const params = new URLSearchParams({ workspace_id: workspaceId, limit: "50" });
  if (projectId) params.set("project_id", projectId);
  return api<{ items: TaskItem[] }>(`/api/tasks?${params}`);
};

export const getDevelopmentBacklog = (workspaceId: string, projectId?: string | null) => {
  const params = new URLSearchParams({ workspace_id: workspaceId, limit: "50" });
  if (projectId) params.set("project_id", projectId);
  return api<{ items: TaskItem[] }>(`/api/development/backlog?${params}`);
};

export const getDevelopmentCodexStatus = (workspaceId: string) => {
  const params = new URLSearchParams({ workspace_id: workspaceId });
  return api<CodexStatus>(`/api/development/codex-status?${params}`);
};

export const getRecentCompletedDevelopment = (workspaceId: string) => {
  const params = new URLSearchParams({ workspace_id: workspaceId, days: "7" });
  return api<{ items: CompletedDevelopment[] }>(
    `/api/development/completed?${params}`,
  );
};

export const getLatestDevelopmentExecution = (workspaceId: string, sync = true) => {
  const params = new URLSearchParams({
    workspace_id: workspaceId,
    sync: sync ? "true" : "false",
  });
  return api<{ execution: DevelopmentExecution | null }>(
    `/api/development/executions/latest?${params}`,
  );
};

export const setTaskState = (
  taskId: string,
  workspaceId: string,
  state: "accepted" | "done" | "snoozed" | "rejected",
) =>
  api<TaskItem>(`/api/tasks/${encodeURIComponent(taskId)}/state`, {
    method: "POST",
    body: JSON.stringify({ workspace_id: workspaceId, state }),
  });

export const getSourceByClient = (conversationId: string, clientSourceId: string) =>
  api<{ source: SourceReceipt }>(
    `/api/conversations/${encodeURIComponent(conversationId)}/sources/by-client/${encodeURIComponent(clientSourceId)}`,
  );

export const getGitHubStatus = (workspaceId: string) => {
  const params = new URLSearchParams({ workspace_id: workspaceId });
  return api<GitHubStatus>(`/api/github/status?${params}`);
};

export const startGitHubManifest = (
  workspaceId: string,
  conversationId?: string | null,
) =>
  api<{
    launch_url: string;
    action_url: string;
    manifest: string;
    state: string;
    expires_at_ms: number;
  }>("/api/github/app-manifest/start", {
    method: "POST",
    body: JSON.stringify({
      workspace_id: workspaceId,
      conversation_id: conversationId ?? null,
    }),
  });

export const startGitHubInstall = (
  workspaceId: string,
  conversationId?: string | null,
) =>
  api<{ install_url: string; expires_at_ms: number }>("/api/github/install/start", {
    method: "POST",
    body: JSON.stringify({
      workspace_id: workspaceId,
      conversation_id: conversationId ?? null,
    }),
  });

export const bindGitHubRepository = (
  repositoryId: number,
  payload: {
    workspace_id: string;
    project_id?: string | null;
    role: "memory_store" | "project_docs" | "source_dataset" | "external_owning_repo" | "generated_artifacts";
    access_mode: "read_only" | "app_managed_write";
    allowed_paths?: string[];
  },
) =>
  api<RepositoryConnection>(`/api/github/repositories/${repositoryId}/bind`, {
    method: "POST",
    body: JSON.stringify({
      ...payload,
      allowed_paths: payload.allowed_paths ?? [],
    }),
  });


export type ProjectNote = {
  id: string;
  project_id: string;
  author: { id: string; display_name: string };
  author_roles: string[];
  title: string;
  body: string;
  source_text: string;
  structured: Record<string, unknown> | null;
  chatgpt_analysis?: {
    status: "completed";
    note_id: string;
    route_id: string;
    source_sha: string;
    result_sha: string;
    repository_path: string;
    generated_at_utc: string;
    markdown: string;
  } | null;
  audience: "project";
  status: "accepted" | "processing" | "waiting_repository" | "blocked" | "ready";
  processing: {
    model: string | null;
    prompt_version: string | null;
    request_uid: string | null;
    attempts: number;
    error: string | null;
  };
  repository: {
    repository_id: number;
    full_name: string;
    path: string;
    sha: string;
    commit_sha: string | null;
  };
  revision: number;
  created_at_ms: number;
  updated_at_ms: number;
};

export type ProjectReply = {
  id: string;
  note_id: string;
  project_id: string;
  author: { id: string; display_name: string };
  body: string;
  created_at_ms: number;
};

export type CollaborationEvent = {
  id: number;
  project_id: string;
  project_name: string;
  actor_id: string;
  kind: "note_created" | "note_replied" | string;
  object_kind: "note" | "reply" | string;
  object_id: string;
  parent_object_id: string | null;
  addressed_to_actor_id: string | null;
  summary: string;
  created_at_ms: number;
};

export const getCollaborationTimeline = (workspaceId: string, afterId = 0) => {
  const params = new URLSearchParams({
    workspace_id: workspaceId,
    after_id: String(afterId),
    limit: "100",
  });
  return api<{ items: CollaborationEvent[] }>(`/api/collaboration/timeline?${params}`);
};

export const getProjectNote = (workspaceId: string, noteId: string) => {
  const params = new URLSearchParams({ workspace_id: workspaceId });
  return api<ProjectNote>(
    `/api/collaboration/notes/${encodeURIComponent(noteId)}?${params}`,
  );
};

export const getProjectNoteReplies = (workspaceId: string, noteId: string) => {
  const params = new URLSearchParams({ workspace_id: workspaceId });
  return api<{ items: ProjectReply[] }>(
    `/api/collaboration/notes/${encodeURIComponent(noteId)}/replies?${params}`,
  );
};

export const postProjectNoteReply = (
  workspaceId: string,
  noteId: string,
  body: string,
) => api<ProjectReply>(
  `/api/collaboration/notes/${encodeURIComponent(noteId)}/replies`,
  {
    method: "POST",
    body: JSON.stringify({
      workspace_id: workspaceId,
      command_id: `ui.reply.${Date.now()}.${crypto.randomUUID().slice(0, 8)}`,
      body,
    }),
  },
);


export type CollaborationQuestion = {
  id: string;
  source_kind: "analysis" | "owner_development";
  analysis_id: string;
  execution_id?: string;
  project_id: string;
  asked_by_actor_id: string;
  addressed_to_actor_id: string;
  addressed_role: string;
  prompt: string;
  shared_context: string;
  blocking: boolean;
  alternatives: Array<"answer" | "unknown" | "skip" | "later">;
  state: "open" | "resolved" | "skipped" | "unknown" | "deferred";
  disposition: "answer" | "unknown" | "skip" | "later" | null;
  answer_text: string | null;
  answered_by_actor_id: string | null;
  deferred_until_ms: number | null;
  created_at_ms: number;
  updated_at_ms: number;
};

export const getCollaborationQuestions = (workspaceId: string) => {
  const params = new URLSearchParams({ workspace_id: workspaceId, limit: "50" });
  return api<{ items: CollaborationQuestion[] }>(
    `/api/collaboration/questions/inbox?${params}`,
  );
};

export const answerCollaborationQuestions = (
  workspaceId: string,
  analysisId: string,
  responses: Array<{
    question_id: string;
    disposition: "answer" | "unknown" | "skip" | "later";
    body?: string;
    deferred_until_ms?: number;
  }>,
) => api<{
  analysis_id: string;
  questions: CollaborationQuestion[];
  continuation: "queued" | "deferred" | "blocked" | "not_required";
}>(
  `/api/collaboration/analyses/${encodeURIComponent(analysisId)}/answers`,
  {
    method: "POST",
    body: JSON.stringify({
      workspace_id: workspaceId,
      command_id: `ui.questions.${Date.now()}.${crypto.randomUUID().slice(0, 8)}`,
      responses,
    }),
  },
);


export const answerSingleCollaborationQuestion = (
  workspaceId: string,
  questionId: string,
  disposition: "answer" | "unknown" | "skip" | "later",
  body = "",
  deferredUntilMs?: number,
) => api<{
  question_id: string;
  execution_id: string;
  state: string;
  disposition: string;
  continuation: "resumed" | "deferred" | "blocked";
}>(
  `/api/collaboration/questions/${encodeURIComponent(questionId)}/respond`,
  {
    method: "POST",
    body: JSON.stringify({
      workspace_id: workspaceId,
      command_id: `ui.question.${Date.now()}.${crypto.randomUUID().slice(0, 8)}`,
      disposition,
      body,
      deferred_until_ms: deferredUntilMs,
    }),
  },
);


export type CollaborationBrief = {
  personal: CollaborationEvent[];
  personal_unread_count: number;
  personal_through_id: number;
  general_available: boolean;
  general_count: number;
  general_preview: CollaborationEvent[];
  general_through_id: number;
  general_news_enabled: boolean;
};

export const getCollaborationBrief = (workspaceId: string) => {
  const params = new URLSearchParams({ workspace_id: workspaceId });
  return api<CollaborationBrief>(`/api/collaboration/brief?${params}`);
};

export const markCollaborationBriefSeen = (
  workspaceId: string,
  personalThroughId?: number,
  generalThroughId?: number,
) => api<{
  personal_cursor: number;
  general_cursor: number;
  general_news_enabled: boolean;
}>("/api/collaboration/brief/seen", {
  method: "POST",
  body: JSON.stringify({
    workspace_id: workspaceId,
    personal_through_id: personalThroughId,
    general_through_id: generalThroughId,
  }),
});

export const setCollaborationGeneralNews = (
  workspaceId: string,
  enabled: boolean,
) => api<{
  personal_cursor: number;
  general_cursor: number;
  general_news_enabled: boolean;
}>("/api/collaboration/preferences/general-news", {
  method: "POST",
  body: JSON.stringify({ workspace_id: workspaceId, enabled }),
});
