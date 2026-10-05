import type { BoardGeometry, BoardObject, BoardStyle } from "./boardApi";

export type GuestBoard = {
  id: string;
  project_id: string;
  project_name: string;
  schema_version: number;
  seq: number;
};

export type GuestSnapshot = {
  board: GuestBoard;
  objects: BoardObject[];
  expires_at_ms: number;
};

export type GuestEvent = {
  event_id: string;
  board_seq: number;
  object_id: string;
  resulting_revision: number;
  server_time_ms: number;
  operation: string;
  changed_fields: string[];
  before: BoardObject | null;
  after: BoardObject | null;
};

export type GuestSocketMessage =
  | ({ type: "snapshot" } & GuestSnapshot)
  | { type: "event"; event: GuestEvent }
  | { type: "ping"; expires_at_ms: number };

const GUEST_PROTOCOL = "projects-hub-guest-board-v1";
const GUEST_TICKET_PREFIX = "projects-hub-guest-ticket.";
const GUEST_CLIENT_PREFIX = "projects-hub-guest-client.";

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
    throw new Error(detail?.message || detail?.code || "Guest access unavailable");
  }
  return payload as T;
}

export async function exchangeGuestToken(token: string) {
  return request<{
    expires_at_ms: number;
    grant_id: string;
    board_id: string;
    project_name: string;
  }>("/api/guest/exchange", {
    method: "POST",
    body: JSON.stringify({ token }),
  });
}

export async function getGuestBoard() {
  return request<GuestSnapshot>("/api/guest/board");
}

function wsUrl(relative: string) {
  const url = new URL(relative, window.location.href);
  url.protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return url.toString();
}

export class GuestBoardSocket {
  private socket: WebSocket | null = null;
  private active = false;
  private reconnectTimer: number | null = null;
  private attempt = 0;
  readonly clientInstanceId = "guest-" + crypto.randomUUID();

  constructor(
    private readonly onMessage: (message: GuestSocketMessage) => void,
    private readonly onState: (state: "connecting" | "open" | "closed") => void,
  ) {}

  async connect() {
    this.active = true;
    await this.open();
  }

  private scheduleReconnect() {
    if (!this.active || this.reconnectTimer !== null) return;
    const delay = Math.min(4000, 400 * 2 ** this.attempt++);
    this.reconnectTimer = window.setTimeout(() => {
      this.reconnectTimer = null;
      void this.open().catch(() => this.scheduleReconnect());
    }, delay);
  }

  private async open() {
    if (!this.active || this.socket?.readyState === WebSocket.OPEN) return;
    this.onState("connecting");
    const issued = await request<{
      ticket: string;
      socket_url: string;
      protocol: string;
    }>("/api/guest/board/socket-ticket", {
      method: "POST",
      body: JSON.stringify({ client_instance_id: this.clientInstanceId }),
    });
    if (!this.active) return;
    const socket = new WebSocket(wsUrl(issued.socket_url), [
      GUEST_PROTOCOL,
      GUEST_TICKET_PREFIX + issued.ticket,
      GUEST_CLIENT_PREFIX + this.clientInstanceId,
    ]);
    this.socket = socket;
    socket.onopen = () => {
      this.attempt = 0;
      this.onState("open");
    };
    socket.onmessage = (event) => {
      if (typeof event.data !== "string") return;
      try {
        this.onMessage(JSON.parse(event.data) as GuestSocketMessage);
      } catch {
        // Ignore malformed frames; server state remains authoritative.
      }
    };
    socket.onclose = (event) => {
      if (this.socket === socket) this.socket = null;
      this.onState("closed");
      if (event.code === 4401 || event.code === 4403) {
        this.active = false;
        return;
      }
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
    this.socket?.close(1000, "guest view closed");
    this.socket = null;
  }
}

export const GuestSocketProtocol = {
  main: GUEST_PROTOCOL,
  ticketPrefix: GUEST_TICKET_PREFIX,
  clientPrefix: GUEST_CLIENT_PREFIX,
} as const;
