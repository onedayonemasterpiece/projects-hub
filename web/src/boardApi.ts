export type BoardGeometry = { x: number; y: number; width: number; height: number; z: number };
export type BoardStyle = { color: "yellow" | "pink" | "blue" | "green" | "orange" | "violet" };
export type BoardReference =
  | { kind: "analysis_run" | "document"; id: string }
  | { kind: "https"; url: string }
  | null;

export type BoardObject = {
  id: string;
  type: "sticky" | "document_card";
  object_revision: number;
  geometry: BoardGeometry;
  style: BoardStyle;
  text: string;
  reference: BoardReference;
  created_by: string;
  created_at_ms: number;
  updated_by: string;
  updated_at_ms: number;
  deleted_at_ms: number | null;
};

export type BoardSnapshot = {
  board: { id: string; project_id: string; schema_version: number; seq: number };
  objects: BoardObject[];
};

export type BoardEvent = {
  event_id: string;
  board_seq: number;
  object_id: string;
  resulting_revision: number;
  initiating_actor_id: string;
  execution_origin: "direct_ui" | "mira" | "analysis_publish" | "undo";
  server_time_ms: number;
  command_id: string;
  operation: string;
  changed_fields: string[];
  before: BoardObject | null;
  after: BoardObject | null;
};

export type BoardReceipt = {
  status: "saved";
  board_id: string;
  board_seq: number;
  object_id: string;
  object_revision: number;
  event: BoardEvent;
};

export type BoardSearchHit = {
  object_id: string;
  type: string;
  title: string;
  snippet: string;
  bbox: BoardGeometry;
  object_revision: number;
  board_seq: number;
  match_terms: string[];
};

export type BoardShareGrant = {
  id: string;
  project_id: string;
  board_id: string;
  created_at_ms: number;
  expires_at_ms: number;
  revoked_at_ms?: number | null;
  url?: string;
  warning?: string;
};

export type AnalysisRun = {
  id: string;
  project_id: string;
  board_id: string;
  purpose: "requirements" | "edge_cases" | "architecture" | "code_review" | "ideas";
  model: "kimi_k3" | "deepseek" | "council_free";
  question: string;
  status: "dispatching" | "dispatch_unknown" | "waiting_capacity" | "running" | "completed" | "failed" | "cancelled" | "blocked";
  result_markdown: string;
  error_code: string | null;
  source_changed: boolean | null;
  source_changed_count: number | null;
  created_at_ms: number;
  updated_at_ms: number;
  finished_at_ms: number | null;
};

export type BoardSocketMessage =
  | ({ type: "snapshot" } & BoardSnapshot)
  | { type: "event"; event: BoardEvent }
  | { type: "ack"; receipt: BoardReceipt }
  | { type: "conflict" | "error"; code: string; message: string; command_id?: string }
  | {
      type: "presence";
      actor_id: string;
      client_instance_id: string;
      cursor?: unknown;
      dragging_object_id?: string;
    }
  | { type: "pong" };

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    cache: "no-store",
    credentials: "same-origin",
    ...options,
    headers: {
      ...(options.body ? { "content-type": "application/json" } : {}),
      ...(options.headers || {}),
    },
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = payload?.error ?? payload?.detail ?? {};
    const message = detail?.message || detail?.code || "HTTP " + String(response.status);
    const error = new Error(message) as Error & { code?: string; status?: number };
    error.code = detail?.code;
    error.status = response.status;
    throw error;
  }
  return payload as T;
}

export async function openBoard(workspaceId: string, projectId: string) {
  return request<{ id: string; project_id: string; seq: number }>(
    "/api/projects/" + encodeURIComponent(projectId) + "/board/open",
    {
      method: "POST",
      body: JSON.stringify({ workspace_id: workspaceId, create_if_allowed: true }),
    },
  );
}

export async function getBoardSnapshot(workspaceId: string, boardId: string) {
  const query = new URLSearchParams({ workspace_id: workspaceId });
  return request<BoardSnapshot>(
    "/api/boards/" + encodeURIComponent(boardId) + "/snapshot?" + query.toString(),
  );
}

export async function searchBoard(
  workspaceId: string,
  boardId: string,
  queryText: string,
  limit = 20,
) {
  const query = new URLSearchParams({
    workspace_id: workspaceId,
    q: queryText,
    limit: String(limit),
  });
  return request<{ items: BoardSearchHit[] }>(
    "/api/boards/" + encodeURIComponent(boardId) + "/search?" + query.toString(),
  );
}

export async function boardHistory(
  workspaceId: string,
  boardId: string,
  objectId: string,
) {
  const query = new URLSearchParams({ workspace_id: workspaceId });
  return request<{ items: BoardEvent[] }>(
    "/api/boards/" +
      encodeURIComponent(boardId) +
      "/objects/" +
      encodeURIComponent(objectId) +
      "/history?" +
      query.toString(),
  );
}

export async function ackBoardUi(
  workspaceId: string,
  boardId: string,
  token: string,
  action: "focus" | "view_all",
  ok: boolean,
) {
  return request<{ token: string; board_id: string; action: string; ok: number }>(
    "/api/boards/" + encodeURIComponent(boardId) + "/ui-ack",
    {
      method: "POST",
      body: JSON.stringify({
        workspace_id: workspaceId,
        token,
        action,
        ok,
      }),
    },
  );
}

export async function createBoardShare(
  workspaceId: string,
  projectId: string,
) {
  return request<BoardShareGrant>(
    "/api/projects/" + encodeURIComponent(projectId) + "/shares",
    {
      method: "POST",
      body: JSON.stringify({ workspace_id: workspaceId }),
    },
  );
}

export async function listBoardShares(
  workspaceId: string,
  projectId: string,
) {
  const query = new URLSearchParams({ workspace_id: workspaceId });
  return request<{ items: BoardShareGrant[] }>(
    "/api/projects/" +
      encodeURIComponent(projectId) +
      "/shares?" +
      query.toString(),
  );
}

export async function revokeBoardShare(
  workspaceId: string,
  shareId: string,
) {
  return request<{
    id: string;
    board_id: string;
    project_id: string;
    revoked_at_ms: number;
  }>("/api/shares/" + encodeURIComponent(shareId) + "/revoke", {
    method: "POST",
    body: JSON.stringify({ workspace_id: workspaceId }),
  });
}

export async function startAnalysis(
  workspaceId: string,
  projectId: string,
  boardId: string,
  objectIds: string[],
  model: AnalysisRun["model"],
  purpose: AnalysisRun["purpose"],
  question: string,
) {
  return request<AnalysisRun>("/api/analysis/runs", {
    method: "POST",
    body: JSON.stringify({
      workspace_id: workspaceId,
      project_id: projectId,
      board_id: boardId,
      object_ids: objectIds,
      command_id: "analysis_ui_" + crypto.randomUUID(),
      model,
      purpose,
      question,
    }),
  });
}

export async function getAnalysisRun(workspaceId: string, runId: string) {
  const query = new URLSearchParams({ workspace_id: workspaceId });
  return request<AnalysisRun>(
    "/api/analysis/runs/" + encodeURIComponent(runId) + "?" + query.toString(),
  );
}

export async function refreshAnalysisRun(workspaceId: string, runId: string) {
  return request<AnalysisRun>(
    "/api/analysis/runs/" + encodeURIComponent(runId) + "/refresh",
    {
      method: "POST",
      body: JSON.stringify({ workspace_id: workspaceId }),
    },
  );
}

export async function cancelAnalysisRun(workspaceId: string, runId: string) {
  return request<AnalysisRun>(
    "/api/analysis/runs/" + encodeURIComponent(runId) + "/cancel",
    {
      method: "POST",
      body: JSON.stringify({ workspace_id: workspaceId }),
    },
  );
}

export async function publishAnalysisRun(
  workspaceId: string,
  runId: string,
  objectId: string,
) {
  return request<BoardReceipt>(
    "/api/analysis/runs/" + encodeURIComponent(runId) + "/publish",
    {
      method: "POST",
      body: JSON.stringify({
        workspace_id: workspaceId,
        command_id: "analysis_publish_ui_" + crypto.randomUUID(),
        object_id: objectId,
      }),
    },
  );
}

export function analysisReportUrl(workspaceId: string, runId: string) {
  const query = new URLSearchParams({ workspace_id: workspaceId });
  return (
    "/api/analysis/runs/" +
    encodeURIComponent(runId) +
    "/report.md?" +
    query.toString()
  );
}

const BOARD_PROTOCOL = "projects-hub-board-v1";
const TICKET_PREFIX = "projects-hub-board-ticket.";
const CLIENT_PREFIX = "projects-hub-board-client.";
const BOARD_CLIENT_INSTANCE_ID = crypto.randomUUID();

function wsUrl(relative: string) {
  const url = new URL(relative, window.location.href);
  url.protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return url.toString();
}

export class BoardSocket {
  readonly clientInstanceId: string;
  private socket: WebSocket | null = null;
  private active = false;
  private reconnectTimer: number | null = null;
  private reconnectAttempt = 0;
  private pending = new Map<
    string,
    { resolve: (receipt: BoardReceipt) => void; reject: (error: Error) => void }
  >();

  constructor(
    private readonly workspaceId: string,
    private readonly boardId: string,
    private readonly onMessage: (message: BoardSocketMessage) => void,
    clientInstanceId?: string,
  ) {
    this.clientInstanceId = clientInstanceId || BOARD_CLIENT_INSTANCE_ID;
  }

  async connect() {
    this.active = true;
    await this.open();
  }

  private scheduleReconnect() {
    if (!this.active || this.reconnectTimer !== null) return;
    const delay = Math.min(3000, 300 * 2 ** this.reconnectAttempt++);
    this.reconnectTimer = window.setTimeout(() => {
      this.reconnectTimer = null;
      void this.open().catch(() => this.scheduleReconnect());
    }, delay);
  }

  private async open() {
    if (!this.active || this.socket?.readyState === WebSocket.OPEN) return;
    const issued = await request<{
      ticket: string;
      socket_url: string;
      protocol: string;
    }>("/api/boards/" + encodeURIComponent(this.boardId) + "/socket-ticket", {
      method: "POST",
      body: JSON.stringify({
        workspace_id: this.workspaceId,
        client_instance_id: this.clientInstanceId,
      }),
    });
    if (!this.active) return;
    const socket = new WebSocket(wsUrl(issued.socket_url), [
      BOARD_PROTOCOL,
      TICKET_PREFIX + issued.ticket,
      CLIENT_PREFIX + this.clientInstanceId,
    ]);
    this.socket = socket;
    socket.onopen = () => {
      this.reconnectAttempt = 0;
    };
    socket.onmessage = (event) => {
      if (typeof event.data !== "string") return;
      let message: BoardSocketMessage;
      try {
        message = JSON.parse(event.data) as BoardSocketMessage;
      } catch {
        return;
      }
      if (message.type === "ack") {
        const pending = this.pending.get(message.receipt.event.command_id);
        if (pending) {
          this.pending.delete(message.receipt.event.command_id);
          pending.resolve(message.receipt);
        }
      } else if (message.type === "conflict" || message.type === "error") {
        const commandId = message.command_id || "";
        const pending = this.pending.get(commandId);
        if (pending) {
          this.pending.delete(commandId);
          const error = new Error(message.message) as Error & { code?: string };
          error.code = message.code;
          pending.reject(error);
        }
      }
      this.onMessage(message);
    };
    socket.onclose = () => {
      if (this.socket === socket) this.socket = null;
      this.scheduleReconnect();
    };
    socket.onerror = () => {
      if (socket.readyState !== WebSocket.CLOSED) socket.close();
    };
  }

  sendPresence(payload: {
    cursor?: { x: number; y: number };
    dragging_object_id?: string;
  }) {
    if (this.socket?.readyState !== WebSocket.OPEN) return;
    this.socket.send(JSON.stringify({ type: "presence", ...payload }));
  }

  sendCommand(command: {
    command_id: string;
    operation: string;
    object_id: string;
    expected_object_revision: number | null;
    payload: Record<string, unknown>;
  }) {
    return new Promise<BoardReceipt>((resolve, reject) => {
      if (this.socket?.readyState !== WebSocket.OPEN) {
        reject(new Error("Доска переподключается"));
        return;
      }
      this.pending.set(command.command_id, { resolve, reject });
      this.socket.send(JSON.stringify({ type: "command", ...command }));
      window.setTimeout(() => {
        const pending = this.pending.get(command.command_id);
        if (!pending) return;
        this.pending.delete(command.command_id);
        reject(
          new Error(
            "Нет подтверждения сохранения; состояние будет сверено после переподключения",
          ),
        );
      }, 12_000);
    });
  }

  close() {
    this.active = false;
    if (this.reconnectTimer !== null) window.clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
    this.socket?.close(1000, "board closed");
    this.socket = null;
    for (const pending of this.pending.values()) {
      pending.reject(new Error("Доска закрыта"));
    }
    this.pending.clear();
  }
}

export const BoardSocketProtocol = {
  main: BOARD_PROTOCOL,
  ticketPrefix: TICKET_PREFIX,
  clientPrefix: CLIENT_PREFIX,
} as const;
