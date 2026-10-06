import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type PointerEvent,
  type WheelEvent,
} from "react";
import {
  StickyScene,
  type BoardCamera,
} from "./BoardShell";
import type { BoardGeometry, BoardObject } from "./boardApi";
import {
  exchangeGuestToken,
  getGuestBoard,
  GuestBoardSocket,
  type GuestSocketMessage,
} from "./guestApi";
import "./guest.css";

function normalizedObjects(items: BoardObject[]) {
  return new Map(
    items
      .filter((item) => item.deleted_at_ms == null)
      .map((item) => [item.id, item]),
  );
}

function applyGuestEvent(
  current: Map<string, BoardObject>,
  event: Extract<GuestSocketMessage, { type: "event" }>["event"],
) {
  const next = new Map(current);
  if (!event.after || event.after.deleted_at_ms != null) {
    next.delete(event.object_id);
  } else {
    next.set(event.object_id, event.after);
  }
  return next;
}

function hashToken() {
  const raw = window.location.hash.startsWith("#")
    ? window.location.hash.slice(1)
    : window.location.hash;
  return new URLSearchParams(raw).get("token") ?? "";
}

function distance(a: { x: number; y: number }, b: { x: number; y: number }) {
  return Math.hypot(a.x - b.x, a.y - b.y);
}

function midpoint(a: { x: number; y: number }, b: { x: number; y: number }) {
  return { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 };
}

export default function GuestBoard() {
  const [host, setHost] = useState<HTMLDivElement | null>(null);
  const [objects, setObjects] = useState<Map<string, BoardObject>>(new Map());
  const [boardName, setBoardName] = useState("Доска проекта");
  const [seq, setSeq] = useState(0);
  const [expiresAt, setExpiresAt] = useState<number | null>(null);
  const [status, setStatus] = useState("Открываю ссылку…");
  const [rendererState, setRendererState] = useState<
    "ready" | "lost" | "unavailable"
  >("ready");
  const [camera, setCamera] = useState<BoardCamera>({ x: 120, y: 100, zoom: 1 });
  const cameraRef = useRef(camera);
  cameraRef.current = camera;
  const socketRef = useRef<GuestBoardSocket | null>(null);
  const pointers = useRef(new Map<number, { x: number; y: number }>());
  const panRef = useRef<{
    pointerId: number;
    start: { x: number; y: number };
    camera: BoardCamera;
  } | null>(null);
  const pinchRef = useRef<{
    ids: [number, number];
    startDistance: number;
    startZoom: number;
    worldAtMid: { x: number; y: number };
  } | null>(null);

  const objectList = useMemo(() => [...objects.values()], [objects]);

  const handleMessage = useCallback((message: GuestSocketMessage) => {
    if (message.type === "snapshot") {
      setObjects(normalizedObjects(message.objects));
      setSeq(message.board.seq);
      setBoardName(message.board.project_name || "Доска проекта");
      setExpiresAt(message.expires_at_ms);
      setStatus("Только просмотр · синхронизировано");
      return;
    }
    if (message.type === "event") {
      setObjects((current) => applyGuestEvent(current, message.event));
      setSeq((current) => Math.max(current, message.event.board_seq));
      setStatus("Только просмотр · обновлено");
      return;
    }
    if (message.type === "ping") {
      setExpiresAt(message.expires_at_ms);
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    const socket = new GuestBoardSocket(handleMessage, (state) => {
      if (cancelled) return;
      if (state === "connecting") setStatus("Переподключение…");
      if (state === "closed") setStatus("Соединение закрыто");
    });
    socketRef.current = socket;

    void (async () => {
      try {
        const token = hashToken();
        if (token) {
          const exchanged = await exchangeGuestToken(token);
          if (cancelled) return;
          setExpiresAt(exchanged.expires_at_ms);
          setBoardName(exchanged.project_name || "Доска проекта");
          window.history.replaceState(
            null,
            "",
            window.location.pathname + window.location.search,
          );
        }
        const snapshot = await getGuestBoard();
        if (cancelled) return;
        handleMessage({ type: "snapshot", ...snapshot });
        await socket.connect();
      } catch (error) {
        if (!cancelled) {
          setStatus(
            error instanceof Error
              ? error.message
              : "Ссылка недействительна или срок действия истёк",
          );
        }
      }
    })();

    return () => {
      cancelled = true;
      socket.close();
      socketRef.current = null;
    };
  }, [handleMessage]);

  const fitAll = useCallback(() => {
    if (!host || objectList.length === 0) return;
    const left = Math.min(...objectList.map((item) => item.geometry.x));
    const top = Math.min(...objectList.map((item) => item.geometry.y));
    const right = Math.max(
      ...objectList.map((item) => item.geometry.x + item.geometry.width),
    );
    const bottom = Math.max(
      ...objectList.map((item) => item.geometry.y + item.geometry.height),
    );
    const bounds: BoardGeometry = {
      x: left,
      y: top,
      width: Math.max(1, right - left),
      height: Math.max(1, bottom - top),
      z: 0,
    };
    const rect = host.getBoundingClientRect();
    const zoom = Math.max(
      0.25,
      Math.min(
        2.5,
        Math.min(
          (rect.width * 0.84) / bounds.width,
          (rect.height * 0.84) / bounds.height,
        ),
      ),
    );
    setCamera({
      zoom,
      x: rect.width / 2 - (bounds.x + bounds.width / 2) * zoom,
      y: rect.height / 2 - (bounds.y + bounds.height / 2) * zoom,
    });
  }, [host, objectList]);

  const beginPinch = () => {
    const entries = [...pointers.current.entries()];
    if (entries.length < 2) {
      pinchRef.current = null;
      return;
    }
    const [first, second] = entries.slice(0, 2);
    const mid = midpoint(first[1], second[1]);
    const current = cameraRef.current;
    pinchRef.current = {
      ids: [first[0], second[0]],
      startDistance: Math.max(1, distance(first[1], second[1])),
      startZoom: current.zoom,
      worldAtMid: {
        x: (mid.x - current.x) / current.zoom,
        y: (mid.y - current.y) / current.zoom,
      },
    };
    panRef.current = null;
  };

  const onPointerDown = (event: PointerEvent<HTMLDivElement>) => {
    event.currentTarget.setPointerCapture(event.pointerId);
    pointers.current.set(event.pointerId, { x: event.clientX, y: event.clientY });
    if (pointers.current.size >= 2) {
      beginPinch();
      return;
    }
    panRef.current = {
      pointerId: event.pointerId,
      start: { x: event.clientX, y: event.clientY },
      camera: cameraRef.current,
    };
  };

  const onPointerMove = (event: PointerEvent<HTMLDivElement>) => {
    if (!pointers.current.has(event.pointerId)) return;
    pointers.current.set(event.pointerId, { x: event.clientX, y: event.clientY });

    if (pointers.current.size >= 2 && pinchRef.current) {
      const [idA, idB] = pinchRef.current.ids;
      const a = pointers.current.get(idA);
      const b = pointers.current.get(idB);
      if (!a || !b) {
        beginPinch();
        return;
      }
      const mid = midpoint(a, b);
      const nextZoom = Math.max(
        0.22,
        Math.min(
          3,
          pinchRef.current.startZoom *
            (distance(a, b) / pinchRef.current.startDistance),
        ),
      );
      setCamera({
        zoom: nextZoom,
        x: mid.x - pinchRef.current.worldAtMid.x * nextZoom,
        y: mid.y - pinchRef.current.worldAtMid.y * nextZoom,
      });
      return;
    }

    const pan = panRef.current;
    if (!pan || pan.pointerId !== event.pointerId) return;
    setCamera({
      ...pan.camera,
      x: pan.camera.x + event.clientX - pan.start.x,
      y: pan.camera.y + event.clientY - pan.start.y,
    });
  };

  const onPointerUp = (event: PointerEvent<HTMLDivElement>) => {
    pointers.current.delete(event.pointerId);
    if (pointers.current.size >= 2) {
      beginPinch();
    } else if (pointers.current.size === 1) {
      pinchRef.current = null;
      const [id, point] = [...pointers.current.entries()][0];
      panRef.current = {
        pointerId: id,
        start: point,
        camera: cameraRef.current,
      };
    } else {
      pinchRef.current = null;
      panRef.current = null;
    }
  };

  const onWheel = (event: WheelEvent<HTMLDivElement>) => {
    event.preventDefault();
    const current = cameraRef.current;
    const rect = event.currentTarget.getBoundingClientRect();
    const px = event.clientX - rect.left;
    const py = event.clientY - rect.top;
    const worldX = (px - current.x) / current.zoom;
    const worldY = (py - current.y) / current.zoom;
    const nextZoom = Math.max(
      0.22,
      Math.min(3, current.zoom * Math.exp(-event.deltaY * 0.0013)),
    );
    setCamera({
      zoom: nextZoom,
      x: px - worldX * nextZoom,
      y: py - worldY * nextZoom,
    });
  };

  const enterFullscreen = async () => {
    try {
      if (!document.fullscreenElement) {
        await document.documentElement.requestFullscreen();
      } else {
        await document.exitFullscreen();
      }
    } catch {
      setStatus("Браузер не разрешил полноэкранный режим");
    }
  };

  return (
    <main className="guest-board">
      <header className="guest-toolbar">
        <div>
          <strong>{boardName}</strong>
          <span>
            {status} · seq {seq}
          </span>
          {expiresAt && (
            <span>
              Ссылка действует до {new Date(expiresAt).toLocaleString()}
            </span>
          )}
        </div>
        <div className="guest-actions">
          <button onClick={fitAll}>Показать всё</button>
          <button onClick={() => void enterFullscreen()}>На весь экран</button>
        </div>
      </header>
      <div
        ref={setHost}
        className="guest-viewport"
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
        onWheel={onWheel}
      >
        <StickyScene
          host={host}
          objects={objectList}
          camera={camera}
          selectedId={null}
          onRendererState={setRendererState}
        />
        {rendererState !== "ready" && (
          <div className="guest-renderer-warning">
            {rendererState === "lost"
              ? "Графический контекст восстанавливается…"
              : "WebGL недоступен на этом устройстве."}
          </div>
        )}
      </div>
      <footer className="guest-footnote">
        Только просмотр. По этой ссылке видны текущие стикеры и будущие изменения
        доски до истечения срока или отзыва. Закрытые документы не раскрываются.
      </footer>
    </main>
  );
}
