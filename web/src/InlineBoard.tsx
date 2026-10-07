import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type PointerEvent,
  type WheelEvent,
} from "react";
import { Application, Container, Graphics, Text } from "pixi.js";
import {
  BoardSocket,
  ackBoardUi,
  getBoardSnapshot,
  openBoard,
  updateBoardViewContext,
  type BoardGeometry,
  type BoardObject,
  type BoardSocketMessage,
} from "./boardApi";

export type BoardUiCommand = {
  kind: "board";
  action: "open" | "close" | "view_all" | "focus";
  project_id: string;
  board_id?: string;
  object_id?: string;
  bbox?: BoardGeometry;
  token?: string;
};

type Camera = { x: number; y: number; zoom: number };
const palette: Record<string, number> = {
  yellow: 0xffe58a,
  pink: 0xffb6cc,
  blue: 0xaad9ff,
  green: 0xb8efc0,
  orange: 0xffc78f,
  violet: 0xd8c2ff,
};

function objectMap(items: BoardObject[]) {
  return new Map(items.filter(item => item.deleted_at_ms == null).map(item => [item.id, item]));
}

function fitCamera(
  objects: BoardObject[],
  width: number,
  height: number,
): Camera {
  if (!objects.length || width <= 0 || height <= 0) return { x: width / 2, y: height / 2, zoom: 1 };
  const left = Math.min(...objects.map(item => item.geometry.x));
  const top = Math.min(...objects.map(item => item.geometry.y));
  const right = Math.max(...objects.map(item => item.geometry.x + item.geometry.width));
  const bottom = Math.max(...objects.map(item => item.geometry.y + item.geometry.height));
  const worldW = Math.max(1, right - left);
  const worldH = Math.max(1, bottom - top);
  const zoom = Math.max(0.15, Math.min(2.5, Math.min((width - 80) / worldW, (height - 80) / worldH)));
  return {
    zoom,
    x: width / 2 - (left + worldW / 2) * zoom,
    y: height / 2 - (top + worldH / 2) * zoom,
  };
}

function focusCamera(
  box: BoardGeometry,
  width: number,
  height: number,
): Camera {
  const zoom = Math.max(0.2, Math.min(2.6, Math.min(
    (width * 0.7) / Math.max(1, box.width),
    (height * 0.7) / Math.max(1, box.height),
  )));
  return {
    zoom,
    x: width / 2 - (box.x + box.width / 2) * zoom,
    y: height / 2 - (box.y + box.height / 2) * zoom,
  };
}

function visibleIds(objects: BoardObject[], camera: Camera, width: number, height: number) {
  if (width <= 0 || height <= 0) return [];
  return objects.filter(item => {
    const g = item.geometry;
    const left = camera.x + g.x * camera.zoom;
    const top = camera.y + g.y * camera.zoom;
    const right = left + g.width * camera.zoom;
    const bottom = top + g.height * camera.zoom;
    return right >= 0 && bottom >= 0 && left <= width && top <= height;
  }).slice(0, 40).map(item => item.id);
}

export default function InlineBoard({
  workspaceId,
  conversationId,
  projectId,
  clientInstanceId,
  command,
  onClose,
}: {
  workspaceId: string;
  conversationId: string;
  projectId: string;
  clientInstanceId: string;
  command: BoardUiCommand | null;
  onClose: () => void;
}) {
  const [host, setHost] = useState<HTMLDivElement | null>(null);
  const [boardId, setBoardId] = useState<string | null>(null);
  const [objects, setObjects] = useState<Map<string, BoardObject>>(new Map());
  const [seq, setSeq] = useState(0);
  const [camera, setCamera] = useState<Camera>({ x: 80, y: 70, zoom: 1 });
  const [size, setSize] = useState({ width: 1, height: 1 });
  const [expanded, setExpanded] = useState(false);
  const [status, setStatus] = useState("Подключение…");
  const [focusedId, setFocusedId] = useState<string | null>(null);
  const appRef = useRef<Application | null>(null);
  const worldRef = useRef<Container | null>(null);
  const socketRef = useRef<BoardSocket | null>(null);
  const dragRef = useRef<{
    pointerId: number;
    startX: number;
    startY: number;
    camera: Camera;
  } | null>(null);

  const ordered = useMemo(
    () => [...objects.values()].sort((a, b) => a.geometry.z - b.geometry.z || a.id.localeCompare(b.id)),
    [objects],
  );

  useEffect(() => {
    if (!host) return;
    const observer = new ResizeObserver(entries => {
      const rect = entries[0]?.contentRect;
      if (rect) setSize({ width: rect.width, height: rect.height });
    });
    observer.observe(host);
    return () => observer.disconnect();
  }, [host]);

  useEffect(() => {
    if (!host) return;
    let disposed = false;
    const app = new Application();
    void app.init({
      resizeTo: host,
      preference: "webgl",
      antialias: true,
      backgroundAlpha: 0,
      autoDensity: true,
      resolution: Math.min(window.devicePixelRatio || 1, 2),
    }).then(() => {
      if (disposed) {
        app.destroy(true);
        return;
      }
      const canvas = app.canvas as HTMLCanvasElement;
      canvas.className = "inline-board-canvas";
      host.appendChild(canvas);
      const world = new Container();
      app.stage.addChild(world);
      appRef.current = app;
      worldRef.current = world;
      setStatus("Доска готова");
    }).catch(() => setStatus("WebGL недоступен"));

    return () => {
      disposed = true;
      appRef.current = null;
      worldRef.current = null;
      try { app.destroy(true); } catch { /* already disposed */ }
    };
  }, [host]);

  useEffect(() => {
    const world = worldRef.current;
    if (!world) return;
    world.removeChildren().forEach(child => child.destroy({ children: true }));
    world.position.set(camera.x, camera.y);
    world.scale.set(camera.zoom);

    for (const item of ordered) {
      const g = item.geometry;
      const group = new Container();
      group.position.set(g.x, g.y);
      group.rotation = ((item.id.charCodeAt(item.id.length - 1) % 7) - 3) * 0.0025;
      group.addChild(
        new Graphics().roundRect(7, 9, g.width, g.height, 15)
          .fill({ color: 0x000000, alpha: 0.18 }),
      );
      const paper = new Graphics().roundRect(0, 0, g.width, g.height, 14)
        .fill({ color: palette[item.style.color] ?? palette.yellow })
        .roundRect(1, 1, g.width - 2, Math.max(16, g.height * 0.14), 13)
        .fill({ color: 0xffffff, alpha: 0.17 });
      if (item.id === focusedId) {
        paper.roundRect(-3, -3, g.width + 6, g.height + 6, 18)
          .stroke({ color: 0xffffff, alpha: 0.9, width: 3 });
      }
      group.addChild(paper);
      group.addChild(
        new Graphics().moveTo(g.width - 26, 1).lineTo(g.width - 1, 1)
          .lineTo(g.width - 1, 26).closePath()
          .fill({ color: 0xffffff, alpha: 0.24 }),
      );
      const copy = new Text({
        text: item.text || "Стикер",
        style: {
          fontFamily: "Inter, system-ui, sans-serif",
          fontSize: 18,
          fontWeight: "600",
          fill: 0x172033,
          wordWrap: true,
          wordWrapWidth: Math.max(80, g.width - 34),
          breakWords: true,
          lineHeight: 23,
        },
      });
      copy.position.set(17, 20);
      group.addChild(copy);
      world.addChild(group);
    }
  }, [camera, focusedId, ordered]);

  useEffect(() => {
    let cancelled = false;
    socketRef.current?.close();
    socketRef.current = null;
    setBoardId(null);
    setObjects(new Map());
    setFocusedId(null);
    setStatus("Подключение…");

    void (async () => {
      try {
        const board = await openBoard(workspaceId, projectId);
        if (cancelled) return;
        setBoardId(board.id);
        const snapshot = await getBoardSnapshot(workspaceId, board.id);
        if (cancelled) return;
        setObjects(objectMap(snapshot.objects));
        setSeq(snapshot.board.seq);
        setCamera(fitCamera(snapshot.objects, size.width, size.height));
        const socket = new BoardSocket(
          workspaceId,
          board.id,
          clientInstanceId,
          message => {
            if (message.type === "snapshot") {
              setObjects(objectMap(message.objects));
              setSeq(message.board.seq);
              setStatus("Синхронизировано");
            } else if (message.type === "event") {
              setObjects(current => {
                const next = new Map(current);
                if (!message.event.after || message.event.after.deleted_at_ms != null) {
                  next.delete(message.event.object_id);
                } else {
                  next.set(message.event.object_id, message.event.after);
                }
                return next;
              });
              setSeq(current => Math.max(current, message.event.board_seq));
              setStatus("Сохранено");
            } else if (message.type === "error") {
              setStatus(message.message || "Ошибка доски");
            }
          },
        );
        socketRef.current = socket;
        await socket.connect();
      } catch (error) {
        if (!cancelled) setStatus(error instanceof Error ? error.message : "Доска недоступна");
      }
    })();

    return () => {
      cancelled = true;
      socketRef.current?.close();
      socketRef.current = null;
    };
  }, [clientInstanceId, projectId, workspaceId]);

  useEffect(() => {
    if (!boardId || !command || command.project_id !== projectId) return;
    if (command.action === "view_all") {
      setFocusedId(null);
      setCamera(fitCamera(ordered, size.width, size.height));
      if (command.token) void ackBoardUi(workspaceId, boardId, command.token, "view_all", true);
      return;
    }
    if (command.action === "focus" && command.bbox) {
      setFocusedId(command.object_id ?? null);
      setCamera(focusCamera(command.bbox, size.width, size.height));
      if (command.token) void ackBoardUi(workspaceId, boardId, command.token, "focus", true);
    }
  }, [boardId, command, ordered, projectId, size, workspaceId]);

  useEffect(() => {
    if (!boardId || !conversationId) return;
    const timer = window.setTimeout(() => {
      void updateBoardViewContext(conversationId, {
        workspace_id: workspaceId,
        project_id: projectId,
        board_id: boardId,
        client_instance_id: clientInstanceId,
        board_seq: seq,
        camera: {
          x: camera.x,
          y: camera.y,
          zoom: camera.zoom,
          width: size.width,
          height: size.height,
        },
        visible_object_ids: visibleIds(ordered, camera, size.width, size.height),
        selected_object_ids: [],
        focused_object_id: focusedId,
      }).catch(() => {});
    }, 180);
    return () => window.clearTimeout(timer);
  }, [
    boardId, camera, clientInstanceId, conversationId, focusedId,
    ordered, projectId, seq, size, workspaceId,
  ]);

  const pointerDown = useCallback((event: PointerEvent<HTMLDivElement>) => {
    event.currentTarget.setPointerCapture(event.pointerId);
    dragRef.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      camera,
    };
  }, [camera]);

  const pointerMove = useCallback((event: PointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    setCamera({
      ...drag.camera,
      x: drag.camera.x + event.clientX - drag.startX,
      y: drag.camera.y + event.clientY - drag.startY,
    });
  }, []);

  const pointerUp = useCallback((event: PointerEvent<HTMLDivElement>) => {
    if (dragRef.current?.pointerId === event.pointerId) dragRef.current = null;
  }, []);

  const wheel = useCallback((event: WheelEvent<HTMLDivElement>) => {
    event.preventDefault();
    const rect = event.currentTarget.getBoundingClientRect();
    const px = event.clientX - rect.left;
    const py = event.clientY - rect.top;
    setCamera(current => {
      const zoom = Math.max(0.15, Math.min(3, current.zoom * Math.exp(-event.deltaY * 0.001)));
      const worldX = (px - current.x) / current.zoom;
      const worldY = (py - current.y) / current.zoom;
      return {
        zoom,
        x: px - worldX * zoom,
        y: py - worldY * zoom,
      };
    });
  }, []);

  return (
    <article className={"inline-board-widget" + (expanded ? " expanded" : "")}>
      <header>
        <div>
          <span className="eyebrow">Доска проекта</span>
          <strong>{status} · {ordered.length} объектов</strong>
        </div>
        <div className="inline-board-actions">
          <button className="mini-action" onClick={() => setCamera(fitCamera(ordered, size.width, size.height))}>
            Показать всё
          </button>
          <button className="mini-action" onClick={() => setExpanded(value => !value)}>
            {expanded ? "Свернуть" : "Развернуть"}
          </button>
          <button className="quiet-button" onClick={onClose}>Закрыть</button>
        </div>
      </header>
      <div
        className="inline-board-host"
        ref={setHost}
        onPointerDown={pointerDown}
        onPointerMove={pointerMove}
        onPointerUp={pointerUp}
        onPointerCancel={pointerUp}
        onWheel={wheel}
        aria-label="Доска проекта, только просмотр. Перетаскивание двигает камеру."
      />
      <footer>Объекты изменяет Мира · здесь доступны только панорама, масштаб и фокус.</footer>
    </article>
  );
}
