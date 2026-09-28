export type Project = { id: string; name: string; status: string };
export type Bootstrap = {
  actor: { id: string; display_name: string };
  workspace: { id: string; name: string };
  role: string;
  projects: Project[];
};
export type Conversation = {
  id: string;
  workspace_id: string;
  actor_id: string;
  focus_project_id: string | null;
  focus_project_name: string | null;
};
export type AuthConfig =
  | { mode: "yandex_pkce"; supabase_url: string; publishable_key: string; provider: string; redirect_url: string }
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

export class ApiError extends Error {
  status: number;
  code?: string;
  constructor(status: number, message: string, code?: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
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

export const exchangePublicAuth = (accessToken: string) =>
  api<Bootstrap>("/api/auth/exchange", {
    method: "POST",
    body: JSON.stringify({ access_token: accessToken }),
  });

export const login = () =>
  api<Bootstrap>("/api/dev/login", { method: "POST", body: JSON.stringify({}) });

export const bootstrap = () => api<Bootstrap>("/api/bootstrap");

export const createConversation = (workspaceId: string, projectId?: string | null) =>
  api<Conversation>("/api/conversations", {
    method: "POST",
    body: JSON.stringify({ workspace_id: workspaceId, focus_project_id: projectId ?? null }),
  });

export const getConversation = (id: string) => api<Conversation>(`/api/conversations/${id}`);

export const getMemories = (workspaceId: string, projectId?: string | null) => {
  const params = new URLSearchParams({ workspace_id: workspaceId, limit: "12" });
  if (projectId) params.set("project_id", projectId);
  return api<{ items: MemoryItem[] }>(`/api/memories?${params}`);
};

export const getSourceByClient = (conversationId: string, clientSourceId: string) =>
  api<{ source: SourceReceipt }>(
    `/api/conversations/${encodeURIComponent(conversationId)}/sources/by-client/${encodeURIComponent(clientSourceId)}`,
  );
