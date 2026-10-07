export type BoardGeometry = {
  x: number; y: number; width: number; height: number; z: number;
};
export type BoardStyle = {
  color: "yellow" | "pink" | "blue" | "green" | "orange" | "violet";
};
export type BoardObject = {
  id: string;
  type: "sticky" | "document_card";
  object_revision: number;
  geometry: BoardGeometry;
  style: BoardStyle;
  text: string;
  reference: unknown;
  created_by: string;
  created_at_ms: number;
  updated_by: string;
  updated_at_ms: number;
  deleted_at_ms: number | null;
};
export type BoardEvent = {
  event_id: string;
  board_seq: number;
  object_id: string;
  resulting_revision: number;
  operation: string;
  after: BoardObject | null;
};
export type BoardSnapshot = {
  board: { id: string; project_id: string; schema_version: number; seq: number };
  objects: BoardObject[];
};
export type BoardSocketMessage =
  | ({ type: "snapshot" } & BoardSnapshot)
  | { type: "event"; event: BoardEvent }
  | { type: "conflict" | "error"; code: string; message: string }
  | { type: "pong" }
  | { type: "presence"; actor_id: string; client_instance_id: string };

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
    const error = new Error(detail?.message || detail?.code || `HTTP ${response.status}`) as
      Error & { code?: string; status?: number };
    error.code = detail?.code;
    error.status = response.status;
    throw error;
  }
  return payload as T;
}

export function openBoard(workspaceId: string, projectId: string) {
  return request<{ id: string; project_id: string; seq: number }>(
    `/api/projects/${encodeURIComponent(projectId)}/board/open`,
    {
      method: "POST",
      body: JSON.stringify({ workspace_id: workspaceId, create_if_allowed: true }),
    },
  );
}

export function getBoardSnapshot(workspaceId: string, boardId: string) {
  const query = new URLSearchParams({ workspace_id: workspaceId });
  return request<BoardSnapshot>(
    `/api/boards/${encodeURIComponent(boardId)}/snapshot?${query}`,
  );
}

export function updateBoardViewContext(
  conversationId: string,
  payload: {
    workspace_id: string;
    project_id: string;
    board_id: string;
    client_instance_id: string;
    board_seq: number;
    camera: { x: number; y: number; zoom: number; width: number; height: number };
    visible_object_ids: string[];
    selected_object_ids: string[];
    focused_object_id: string | null;
  },
) {
  return request(
    `/api/conversations/${encodeURIComponent(conversationId)}/board-view-context`,
    { method: "PUT", body: JSON.stringify(payload) },
  );
}

export function ackBoardUi(
  workspaceId: string,
  boardId: string,
  token: string,
  action: "focus" | "view_all",
  ok: boolean,
) {
  return request(
    `/api/boards/${encodeURIComponent(boardId)}/ui-ack`,
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

const MAIN = "projects-hub-board-v1";
const TICKET = "projects-hub-board-ticket.";
const CLIENT = "projects-hub-board-client.";

function wsUrl(relative: string) {
  const url = new URL(relative, window.location.href);
  url.protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return url.toString();
}

export class BoardSocket {
  private socket: WebSocket | null = null;
  private active = false;
  private reconnectTimer: number | null = null;
  private reconnectAttempt = 0;

  constructor(
    private readonly workspaceId: string,
    private readonly boardId: string,
    private readonly clientInstanceId: string,
    private readonly onMessage: (message: BoardSocketMessage) => void,
  ) {}

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
      ticket: string; socket_url: string; protocol: string;
    }>(`/api/boards/${encodeURIComponent(this.boardId)}/socket-ticket`, {
      method: "POST",
      body: JSON.stringify({
        workspace_id: this.workspaceId,
        client_instance_id: this.clientInstanceId,
      }),
    });
    if (!this.active) return;
    const socket = new WebSocket(wsUrl(issued.socket_url), [
      MAIN,
      TICKET + issued.ticket,
      CLIENT + this.clientInstanceId,
    ]);
    this.socket = socket;
    socket.onopen = () => { this.reconnectAttempt = 0; };
    socket.onmessage = event => {
      if (typeof event.data !== "string") return;
      try {
        this.onMessage(JSON.parse(event.data) as BoardSocketMessage);
      } catch {
        return;
      }
    };
    socket.onclose = () => {
      if (this.socket === socket) this.socket = null;
      this.scheduleReconnect();
    };
    socket.onerror = () => {
      if (socket.readyState !== WebSocket.CLOSED) socket.close();
    };
  }

  close() {
    this.active = false;
    if (this.reconnectTimer !== null) window.clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
    this.socket?.close(1000, "board closed");
    this.socket = null;
  }
}
