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
  analysisReportUrl,
  boardHistory,
  cancelAnalysisRun,
  createBoardShare,
  getAnalysisMaterialization,
  getAnalysisRun,
  getBoardSnapshot,
  listBoardShares,
  materializeAnalysisRun,
  openBoard,
  publishAnalysisRun,
  revokeBoardShare,
  refreshAnalysisRun,
  searchBoard,
  startAnalysis,
  updateBoardViewContext,
  type AnalysisMaterialization,
  type AnalysisRun,
  type BoardEvent,
  type BoardGeometry,
  type BoardObject,
  type BoardSearchHit,
  type BoardShareGrant,
  type BoardSocketMessage,
  type BoardStyle,
} from "./boardApi";
import "./board.css";

export type BoardCamera = { x: number; y: number; zoom: number };
export type BoardFocusRequest =
  | {
      kind: "object";
      objectId: string;
      bbox?: BoardGeometry;
      token: string;
    }
  | {
      kind: "view_all";
      token: string;
    }
  | null;

export type BoardShareRequest =
  | {
      id: string;
      url: string;
      expiresAtMs: number;
      warning?: string;
    }
  | null;

const palette: Record<BoardStyle["color"], number> = {
  yellow: 0xffe58a,
  pink: 0xffb6cc,
  blue: 0xaad9ff,
  green: 0xb8efc0,
  orange: 0xffc78f,
  violet: 0xd8c2ff,
};

function normalizedObjects(items: BoardObject[]) {
  return new Map(
    items
      .filter((item) => item.deleted_at_ms == null)
      .map((item) => [item.id, item]),
  );
}

function applyEvent(current: Map<string, BoardObject>, event: BoardEvent) {
  const next = new Map(current);
  if (!event.after || event.after.deleted_at_ms != null) {
    next.delete(event.object_id);
  } else {
    next.set(event.object_id, event.after);
  }
  return next;
}

function worldToScreen(geometry: BoardGeometry, camera: BoardCamera) {
  return {
    left: camera.x + geometry.x * camera.zoom,
    top: camera.y + geometry.y * camera.zoom,
    width: geometry.width * camera.zoom,
    height: geometry.height * camera.zoom,
  };
}

export function StickyScene({
  host,
  objects,
  camera,
  selectedId,
  onRendererState,
}: {
  host: HTMLDivElement | null;
  objects: BoardObject[];
  camera: BoardCamera;
  selectedId: string | null;
  onRendererState: (value: "ready" | "lost" | "unavailable") => void;
}) {
  const appRef = useRef<Application | null>(null);
  const worldRef = useRef<Container | null>(null);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    if (!host) return;
    let disposed = false;
    const app = new Application();
    const onLost = (event: Event) => {
      event.preventDefault();
      onRendererState("lost");
    };
    const onRestored = () => onRendererState("ready");

    void app
      .init({
        resizeTo: host,
        preference: "webgl",
        antialias: true,
        backgroundAlpha: 0,
        autoDensity: true,
        resolution: Math.min(window.devicePixelRatio || 1, 2),
      })
      .then(() => {
        if (disposed) {
          app.destroy(true);
          return;
        }
        const canvas = app.canvas as HTMLCanvasElement;
        canvas.className = "board-webgl-canvas";
        canvas.addEventListener("webglcontextlost", onLost);
        canvas.addEventListener("webglcontextrestored", onRestored);
        host.appendChild(canvas);
        const world = new Container();
        app.stage.addChild(world);
        appRef.current = app;
        worldRef.current = world;
        setReady(true);
        onRendererState("ready");
      })
      .catch(() => {
        onRendererState("unavailable");
      });

    return () => {
      disposed = true;
      setReady(false);
      const canvas = app.canvas as HTMLCanvasElement | undefined;
      canvas?.removeEventListener("webglcontextlost", onLost);
      canvas?.removeEventListener("webglcontextrestored", onRestored);
      appRef.current = null;
      worldRef.current = null;
      try {
        app.destroy(true);
      } catch {
        // Context can already be gone.
      }
    };
  }, [host, onRendererState]);

  useEffect(() => {
    const world = worldRef.current;
    if (!ready || !world) return;

    world.removeChildren().forEach((child) => child.destroy({ children: true }));
    world.position.set(camera.x, camera.y);
    world.scale.set(camera.zoom);

    for (const object of objects) {
      const geometry = object.geometry;
      const width = geometry.width;
      const height = geometry.height;
      const group = new Container();
      group.position.set(geometry.x, geometry.y);
      const tailCode = object.id.charCodeAt(Math.max(0, object.id.length - 1));
      group.rotation = ((tailCode % 7) - 3) * 0.0025;

      const shadow = new Graphics()
        .roundRect(7, 10, width, height, 16)
        .fill({ color: 0x000000, alpha: 0.2 });
      group.addChild(shadow);

      const paper = new Graphics()
        .roundRect(0, 0, width, height, 14)
        .fill({ color: palette[object.style.color] ?? palette.yellow })
        .roundRect(1, 1, width - 2, Math.max(18, height * 0.16), 13)
        .fill({ color: 0xffffff, alpha: 0.16 });
      if (object.id === selectedId) {
        paper
          .roundRect(-3, -3, width + 6, height + 6, 18)
          .stroke({ color: 0xffffff, alpha: 0.86, width: 3 });
      }
      group.addChild(paper);

      const fold = new Graphics()
        .moveTo(width - 27, 1)
        .lineTo(width - 1, 1)
        .lineTo(width - 1, 27)
        .closePath()
        .fill({ color: 0xffffff, alpha: 0.23 });
      group.addChild(fold);

      const copy = new Text({
        text: object.text || "Новый стикер",
        style: {
          fontFamily: "Inter, system-ui, sans-serif",
          fontSize: 18,
          fontWeight: "600",
          fill: 0x172033,
          wordWrap: true,
          wordWrapWidth: Math.max(80, width - 34),
          breakWords: true,
          lineHeight: 23,
        },
      });
      copy.position.set(17, 22);
      group.addChild(copy);

      const meta = new Text({
        text: "r" + String(object.object_revision),
        style: {
          fontFamily: "Inter, system-ui, sans-serif",
          fontSize: 10,
          fontWeight: "700",
          fill: 0x172033,
        },
      });
      meta.alpha = 0.46;
      meta.position.set(16, Math.max(30, height - 24));
      group.addChild(meta);
      world.addChild(group);
    }
  }, [camera, objects, ready, selectedId]);

  return null;
}

export function BoardShell({
  visible,
  workspaceId,
  conversationId,
  projectId,
  clientInstanceId,
  canAnalyze,
  canManageShare,
  focusRequest,
  analysisRunId,
  shareRequest,
  onClose,
  onFocusFulfilled,
}: {
  visible: boolean;
  workspaceId: string;
  conversationId: string;
  projectId: string;
  clientInstanceId: string;
  canAnalyze: boolean;
  canManageShare: boolean;
  focusRequest?: BoardFocusRequest;
  analysisRunId?: string | null;
  shareRequest?: BoardShareRequest;
  onClose: () => void;
  onFocusFulfilled?: (token: string, ok: boolean) => void;
}) {
  const [host, setHost] = useState<HTMLDivElement | null>(null);
  const [boardId, setBoardId] = useState<string | null>(null);
  const [objects, setObjects] = useState<Map<string, BoardObject>>(new Map());
  const [seq, setSeq] = useState(0);
  const [camera, setCamera] = useState<BoardCamera>({ x: 160, y: 120, zoom: 1 });
  const cameraRef = useRef(camera);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [focusedId, setFocusedId] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [hits, setHits] = useState<BoardSearchHit[]>([]);
  const [status, setStatus] = useState("Подключение…");
  const [rendererState, setRendererState] = useState<
    "ready" | "lost" | "unavailable"
  >("ready");
  const [history, setHistory] = useState<BoardEvent[]>([]);
  const [analysisOpen, setAnalysisOpen] = useState(false);
  const [analysisRun, setAnalysisRun] = useState<AnalysisRun | null>(null);
  const [analysisModel, setAnalysisModel] = useState<AnalysisRun["model"]>("kimi_k3");
  const [analysisPurpose, setAnalysisPurpose] =
    useState<AnalysisRun["purpose"]>("edge_cases");
  const [analysisQuestion, setAnalysisQuestion] = useState(
    "Найди риски, пограничные случаи и конкретные рекомендации по выбранному материалу.",
  );
  const [analysisBusy, setAnalysisBusy] = useState(false);
  const [materialization, setMaterialization] =
    useState<AnalysisMaterialization | null>(null);
  const [materializationBusy, setMaterializationBusy] = useState(false);
  const [shareOpen, setShareOpen] = useState(false);
  const [shareActive, setShareActive] = useState<BoardShareGrant | null>(null);
  const [shareItems, setShareItems] = useState<BoardShareGrant[]>([]);
  const [shareBusy, setShareBusy] = useState(false);
  const [shareMessage, setShareMessage] = useState("");
  const socketRef = useRef<BoardSocket | null>(null);
  const fulfilledFocusRef = useRef<string | null>(null);
  const dragRef = useRef<{
    pointerId: number;
    startX: number;
    startY: number;
    camera: BoardCamera;
  } | null>(null);
  const objectsRef = useRef(objects);
  objectsRef.current = objects;
  cameraRef.current = camera;
  const reducedMotion = useMemo(
    () =>
      typeof window.matchMedia === "function" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches,
    [],
  );

  const handleSocket = useCallback((message: BoardSocketMessage) => {
    if (message.type === "snapshot") {
      setObjects(normalizedObjects(message.objects));
      setSeq(message.board.seq);
      setStatus("Синхронизировано");
      return;
    }
    if (message.type === "event") {
      setObjects((current) => applyEvent(current, message.event));
      setSeq((current) => Math.max(current, message.event.board_seq));
      setStatus("Сохранено");
      return;
    }
    if (message.type === "conflict") {
      setStatus("Конфликт: на сервере уже есть более свежая версия");
    }
    if (message.type === "error") {
      setStatus(message.message || "Ошибка синхронизации");
    }
  }, []);

  useEffect(() => {
    if (!visible) return;
    let cancelled = false;
    fulfilledFocusRef.current = null;

    void (async () => {
      try {
        setStatus("Подключение…");
        const board = await openBoard(workspaceId, projectId);
        if (cancelled) return;
        setBoardId(board.id);
        const snapshot = await getBoardSnapshot(workspaceId, board.id);
        if (cancelled) return;
        setObjects(normalizedObjects(snapshot.objects));
        setSeq(snapshot.board.seq);
        const socket = new BoardSocket(
          workspaceId,
          board.id,
          handleSocket,
          clientInstanceId,
        );
        socketRef.current = socket;
        await socket.connect();
        if (!cancelled) setStatus("Синхронизировано");
      } catch (error) {
        if (!cancelled) {
          setStatus(error instanceof Error ? error.message : "Доска недоступна");
        }
      }
    })();

    return () => {
      cancelled = true;
      socketRef.current?.close();
      socketRef.current = null;
      setBoardId(null);
    };
  }, [clientInstanceId, handleSocket, projectId, visible, workspaceId]);

  useEffect(() => {
    if (!visible || !analysisRunId) return;
    let cancelled = false;
    void getAnalysisRun(workspaceId, analysisRunId)
      .then((run) => {
        if (cancelled) return;
        setAnalysisRun(run);
        setAnalysisOpen(true);
      })
      .catch((error) => {
        if (!cancelled) {
          setStatus(error instanceof Error ? error.message : "Отчёт недоступен");
        }
      });
    return () => {
      cancelled = true;
    };
  }, [analysisRunId, visible, workspaceId]);

  useEffect(() => {
    if (!visible || !analysisRun || analysisRun.status !== "completed") {
      setMaterialization(null);
      return;
    }
    let cancelled = false;
    void getAnalysisMaterialization(workspaceId, analysisRun.id)
      .then((value) => {
        if (!cancelled) setMaterialization(value);
      })
      .catch((error) => {
        if (!cancelled) {
          setMaterialization({
            run_id: analysisRun.id,
            path: "docs/analysis/" + analysisRun.id + ".md",
            status: "failed",
            error_code: error instanceof Error ? error.message : "MATERIALIZATION_STATUS_FAILED",
          });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [analysisRun?.id, analysisRun?.status, visible, workspaceId]);

  useEffect(() => {
    if (!visible || !analysisRun) return;
    if (["completed", "failed", "cancelled", "blocked"].includes(analysisRun.status)) {
      return;
    }
    let cancelled = false;
    const poll = () => {
      void refreshAnalysisRun(workspaceId, analysisRun.id)
        .then((run) => {
          if (!cancelled) setAnalysisRun(run);
        })
        .catch((error) => {
          if (!cancelled) {
            setStatus(error instanceof Error ? error.message : "Не удалось обновить анализ");
          }
        });
    };
    const timer = window.setInterval(poll, 2000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [analysisRun?.id, analysisRun?.status, visible, workspaceId]);

  useEffect(() => {
    if (!visible || !shareRequest || !canManageShare) return;
    setShareActive({
      id: shareRequest.id,
      project_id: projectId,
      board_id: boardId ?? "",
      created_at_ms: Date.now(),
      expires_at_ms: shareRequest.expiresAtMs,
      url: shareRequest.url,
      warning: shareRequest.warning,
    });
    setShareOpen(true);
    setShareMessage("");
  }, [boardId, canManageShare, projectId, shareRequest, visible]);

  const refreshShares = useCallback(async () => {
    if (!canManageShare) return;
    const result = await listBoardShares(workspaceId, projectId);
    setShareItems(result.items);
  }, [canManageShare, projectId, workspaceId]);

  const focusGeometry = useCallback(
    (geometry: BoardGeometry, token?: string) => {
      if (!host) {
        if (token) onFocusFulfilled?.(token, false);
        return;
      }
      const rect = host.getBoundingClientRect();
      const targetZoom = Math.max(
        0.45,
        Math.min(
          2.2,
          Math.min(
            (rect.width * 0.68) / geometry.width,
            (rect.height * 0.68) / geometry.height,
          ),
        ),
      );
      const target = {
        zoom: targetZoom,
        x: rect.width / 2 - (geometry.x + geometry.width / 2) * targetZoom,
        y: rect.height / 2 - (geometry.y + geometry.height / 2) * targetZoom,
      };

      if (reducedMotion) {
        setCamera(target);
        if (token) {
          requestAnimationFrame(() => onFocusFulfilled?.(token, true));
        }
        return;
      }

      const start = cameraRef.current;
      const began = performance.now();
      const animate = (now: number) => {
        const progress = Math.min(1, (now - began) / 360);
        const eased = 1 - Math.pow(1 - progress, 3);
        setCamera({
          x: start.x + (target.x - start.x) * eased,
          y: start.y + (target.y - start.y) * eased,
          zoom: start.zoom + (target.zoom - start.zoom) * eased,
        });
        if (progress < 1) {
          requestAnimationFrame(animate);
        } else if (token) {
          onFocusFulfilled?.(token, true);
        }
      };
      requestAnimationFrame(animate);
    },
    [host, onFocusFulfilled, reducedMotion],
  );

  useEffect(() => {
    if (!visible || !focusRequest) return;
    if (fulfilledFocusRef.current === focusRequest.token) return;
    fulfilledFocusRef.current = focusRequest.token;
    if (focusRequest.kind === "view_all") {
      setFocusedId(null);
      const list = [...objectsRef.current.values()];
      if (list.length === 0) {
        onFocusFulfilled?.(focusRequest.token, true);
        return;
      }
      const left = Math.min(...list.map((item) => item.geometry.x));
      const top = Math.min(...list.map((item) => item.geometry.y));
      const right = Math.max(
        ...list.map((item) => item.geometry.x + item.geometry.width),
      );
      const bottom = Math.max(
        ...list.map((item) => item.geometry.y + item.geometry.height),
      );
      focusGeometry(
        {
          x: left,
          y: top,
          width: Math.max(1, right - left),
          height: Math.max(1, bottom - top),
          z: 0,
        },
        focusRequest.token,
      );
      return;
    }
    const current = objectsRef.current.get(focusRequest.objectId);
    const geometry = current?.geometry ?? focusRequest.bbox;
    if (!geometry) {
      onFocusFulfilled?.(focusRequest.token, false);
      return;
    }
    setSelectedId(focusRequest.objectId);
    setFocusedId(focusRequest.objectId);
    focusGeometry(geometry, focusRequest.token);
  }, [focusGeometry, focusRequest, onFocusFulfilled, visible]);

  const runSearch = useCallback(async () => {
    if (!boardId || !search.trim()) {
      setHits([]);
      return;
    }
    try {
      const result = await searchBoard(workspaceId, boardId, search.trim());
      setHits(result.items);
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Поиск недоступен");
    }
  }, [boardId, search, workspaceId]);

  useEffect(() => {
    const timer = window.setTimeout(() => void runSearch(), 180);
    return () => window.clearTimeout(timer);
  }, [runSearch]);

  const showHistory = async (objectId: string) => {
    if (!boardId) return;
    setSelectedId(objectId);
    try {
      const result = await boardHistory(workspaceId, boardId, objectId);
      setHistory(result.items);
    } catch {
      setHistory([]);
    }
  };

  const onViewportPointerDown = (event: PointerEvent<HTMLDivElement>) => {
    if ((event.target as HTMLElement).closest("[data-board-object]")) return;
    event.currentTarget.setPointerCapture(event.pointerId);
    dragRef.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      camera: cameraRef.current,
    };
  };

  const onPointerMove = (event: PointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    setCamera({
      ...drag.camera,
      x: drag.camera.x + event.clientX - drag.startX,
      y: drag.camera.y + event.clientY - drag.startY,
    });
  };

  const onPointerUp = (event: PointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    dragRef.current = null;
  };

  const onWheel = (event: WheelEvent<HTMLDivElement>) => {
    event.preventDefault();
    if (!host) return;
    const rect = host.getBoundingClientRect();
    const current = cameraRef.current;
    const screenX = event.clientX - rect.left;
    const screenY = event.clientY - rect.top;
    const worldX = (screenX - current.x) / current.zoom;
    const worldY = (screenY - current.y) / current.zoom;
    const factor = Math.exp(-event.deltaY * 0.0012);
    const zoom = Math.max(0.25, Math.min(3.2, current.zoom * factor));
    setCamera({
      zoom,
      x: screenX - worldX * zoom,
      y: screenY - worldY * zoom,
    });
  };

  const fitAll = () => {
    setFocusedId(null);
    if (!host || objects.size === 0) return;
    const list = [...objects.values()];
    const left = Math.min(...list.map((item) => item.geometry.x));
    const top = Math.min(...list.map((item) => item.geometry.y));
    const right = Math.max(
      ...list.map((item) => item.geometry.x + item.geometry.width),
    );
    const bottom = Math.max(
      ...list.map((item) => item.geometry.y + item.geometry.height),
    );
    focusGeometry({
      x: left,
      y: top,
      width: Math.max(1, right - left),
      height: Math.max(1, bottom - top),
      z: 0,
    });
  };

  const prepareShare = async () => {
    if (!canManageShare || shareBusy) return;
    setShareBusy(true);
    setShareMessage("");
    try {
      const grant = await createBoardShare(workspaceId, projectId);
      setShareActive(grant);
      setShareOpen(true);
      await refreshShares();
    } catch (error) {
      setShareMessage(error instanceof Error ? error.message : "Не удалось создать ссылку");
      setShareOpen(true);
    } finally {
      setShareBusy(false);
    }
  };

  const shareViaSystem = async () => {
    const rawUrl = shareActive?.url;
    if (!rawUrl) return;
    const url = new URL(rawUrl, window.location.origin).toString();
    if (typeof navigator.share !== "function") {
      try {
        await navigator.clipboard.writeText(url);
        setShareMessage("Ссылка скопирована.");
      } catch {
        setShareMessage("Не удалось открыть системное меню или скопировать ссылку.");
      }
      return;
    }
    try {
      await navigator.share({ title: "Доска проекта", url });
      setShareMessage("Системное меню завершено; приложение не подтверждает доставку получателю.");
    } catch (error) {
      const name =
        error && typeof error === "object" && "name" in error
          ? String((error as { name?: unknown }).name ?? "")
          : "";
      setShareMessage(
        name === "AbortError"
          ? "Поделиться отменено; доставка не выполнялась."
          : "Системное меню не открылось.",
      );
    }
  };

  const copyShareLink = async () => {
    const rawUrl = shareActive?.url;
    if (!rawUrl) return;
    try {
      await navigator.clipboard.writeText(
        new URL(rawUrl, window.location.origin).toString(),
      );
      setShareMessage("Ссылка скопирована.");
    } catch {
      setShareMessage("Не удалось скопировать ссылку.");
    }
  };

  const revokeShare = async (shareId: string) => {
    if (shareBusy) return;
    setShareBusy(true);
    try {
      await revokeBoardShare(workspaceId, shareId);
      if (shareActive?.id === shareId) {
        setShareActive((current) =>
          current ? { ...current, revoked_at_ms: Date.now() } : null,
        );
      }
      await refreshShares();
      setShareMessage("Ссылка отозвана; открытые гостевые подключения будут закрыты.");
    } catch (error) {
      setShareMessage(error instanceof Error ? error.message : "Не удалось отозвать ссылку");
    } finally {
      setShareBusy(false);
    }
  };

  const selected = selectedId ? objects.get(selectedId) ?? null : null;
  const analysisReferenceId =
    selected?.reference?.kind === "analysis_run" ? selected.reference.id : null;
  const objectList = useMemo(() => [...objects.values()], [objects]);
  const visibleObjectIds = useMemo(() => {
    if (!host || camera.zoom <= 0) return [] as string[];
    const left = -camera.x / camera.zoom;
    const top = -camera.y / camera.zoom;
    const right = left + host.clientWidth / camera.zoom;
    const bottom = top + host.clientHeight / camera.zoom;
    return objectList
      .filter((item) => {
        const box = item.geometry;
        return (
          box.x + box.width >= left &&
          box.x <= right &&
          box.y + box.height >= top &&
          box.y <= bottom
        );
      })
      .slice(0, 40)
      .map((item) => item.id);
  }, [camera, host, objectList]);

  useEffect(() => {
    if (!visible || !boardId || !conversationId || !host) return;
    let cancelled = false;
    const publish = () => {
      if (cancelled) return;
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
          width: host.clientWidth,
          height: host.clientHeight,
        },
        visible_object_ids: visibleObjectIds,
        selected_object_ids: selectedId ? [selectedId] : [],
        focused_object_id: focusedId,
      }).catch(() => {
        // View context is advisory and must never interrupt voice or board rendering.
      });
    };
    const timer = window.setTimeout(publish, 180);
    const heartbeat = window.setInterval(publish, 30_000);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
      window.clearInterval(heartbeat);
    };
  }, [
    boardId,
    camera,
    clientInstanceId,
    conversationId,
    focusedId,
    host,
    projectId,
    selectedId,
    seq,
    visible,
    visibleObjectIds,
    workspaceId,
  ]);
  const startSelectedAnalysis = async () => {
    if (!boardId || !selected || !canAnalyze || analysisBusy) return;
    const question = analysisQuestion.trim();
    if (!question) {
      setStatus("Опишите, что нужно проверить.");
      return;
    }
    setAnalysisBusy(true);
    try {
      const run = await startAnalysis(
        workspaceId,
        projectId,
        boardId,
        [selected.id],
        analysisModel,
        analysisPurpose,
        question,
      );
      setAnalysisRun(run);
      setAnalysisOpen(true);
      setStatus("Анализ запущен");
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Не удалось запустить анализ");
    } finally {
      setAnalysisBusy(false);
    }
  };

  const cancelCurrentAnalysis = async () => {
    if (!analysisRun || analysisBusy) return;
    setAnalysisBusy(true);
    try {
      setAnalysisRun(await cancelAnalysisRun(workspaceId, analysisRun.id));
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Не удалось отменить анализ");
    } finally {
      setAnalysisBusy(false);
    }
  };

  const materializeCurrentAnalysis = async (
    repositoryId?: number,
    allowPublic = false,
  ) => {
    if (!analysisRun || analysisRun.status !== "completed" || materializationBusy) return;
    setMaterializationBusy(true);
    try {
      const value = await materializeAnalysisRun(
        workspaceId,
        analysisRun.id,
        repositoryId,
        allowPublic,
      );
      setMaterialization(value);
      if (value.status === "synced") {
        setStatus(value.reused ? "GitHub уже содержит этот отчёт." : "Отчёт синхронизирован в GitHub.");
      } else if (value.status === "confirmation_required") {
        setStatus("Для публичного репозитория требуется отдельное подтверждение публикации.");
      } else if (value.status === "not_configured") {
        setStatus("GitHub document binding для отчётов не настроен.");
      }
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Не удалось синхронизировать отчёт");
    } finally {
      setMaterializationBusy(false);
    }
  };

  const publishCurrentAnalysis = async () => {
    if (!analysisRun || analysisRun.status !== "completed" || analysisBusy) return;
    setAnalysisBusy(true);
    try {
      await publishAnalysisRun(
        workspaceId,
        analysisRun.id,
        "obj_analysis_" + crypto.randomUUID(),
      );
      setStatus("Отчёт добавлен на доску");
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Не удалось добавить отчёт");
    } finally {
      setAnalysisBusy(false);
    }
  };


  return (
    <section
      className={"board-shell" + (visible ? " is-open" : "")}
      aria-hidden={!visible}
    >
      <header className="board-toolbar">
        <div className="board-title">
          <strong>Доска проекта</strong>
          <span>
            {status} · seq {seq}
          </span>
        </div>

        <div className="board-actions">
          <button onClick={fitAll}>Показать всё</button>
          {canManageShare && (
            <button onClick={() => void prepareShare()} disabled={shareBusy}>
              {shareBusy ? "Готовлю ссылку…" : "Поделиться"}
            </button>
          )}
          <button onClick={onClose}>Закрыть доску</button>
        </div>

        <div className="board-search-wrap">
          <input
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder="Найти стикер, цвет, автора…"
            aria-label="Поиск по доске"
          />
          {hits.length > 0 && (
            <div className="board-search-results">
              {hits.map((hit) => (
                <button
                  key={hit.object_id}
                  onClick={() => {
                    setHits([]);
                    setSelectedId(hit.object_id);
                    focusGeometry(hit.bbox);
                  }}
                >
                  <strong>{hit.title || "Без названия"}</strong>
                  <span>{hit.snippet}</span>
                </button>
              ))}
            </div>
          )}
        </div>
      </header>

      <div
        ref={setHost}
        className="board-viewport"
        onPointerDown={onViewportPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
        onWheel={onWheel}
      >
        <StickyScene
          host={host}
          objects={objectList}
          camera={camera}
          selectedId={selectedId}
          onRendererState={setRendererState}
        />

        {rendererState !== "ready" && (
          <div className="board-renderer-warning">
            {rendererState === "lost"
              ? "WebGL-контекст потерян. Жду восстановления…"
              : "WebGL недоступен на этом устройстве."}
          </div>
        )}

        <div className="board-hit-layer">
          {objectList.map((object) => {
            const rect = worldToScreen(object.geometry, camera);
            return (
              <button
                key={object.id}
                data-board-object
                className={
                  "board-object-hit" +
                  (selectedId === object.id ? " is-selected" : "")
                }
                style={{
                  left: rect.left,
                  top: rect.top,
                  width: rect.width,
                  height: rect.height,
                }}
                onClick={() => void showHistory(object.id)}
                aria-label={object.text || "Стикер"}
              />
            );
          })}
        </div>

      </div>

      {shareOpen && canManageShare && (
        <aside className="board-share-panel" aria-label="Поделиться доской">
          <div className="board-share-heading">
            <div>
              <strong>Живая ссылка · только просмотр</strong>
              <span>Срок — семь дней</span>
            </div>
            <button onClick={() => setShareOpen(false)}>Закрыть</button>
          </div>
          {shareActive?.url && !shareActive.revoked_at_ms && (
            <>
              <p className="board-share-warning">
                {shareActive.warning ||
                  "По ссылке доступно содержимое этой доски, включая дальнейшие изменения, до истечения срока или отзыва."}
              </p>
              <code className="board-share-url">
                {new URL(shareActive.url, window.location.origin).toString()}
              </code>
              <div className="board-share-actions">
                <button onClick={() => void shareViaSystem()}>
                  {typeof navigator.share === "function"
                    ? "Открыть системное «Поделиться»"
                    : "Скопировать ссылку"}
                </button>
                <button onClick={() => void copyShareLink()}>Копировать</button>
                <button
                  onClick={() => void revokeShare(shareActive.id)}
                  disabled={shareBusy}
                >
                  Отозвать
                </button>
              </div>
              <span className="board-share-expiry">
                Действует до {new Date(shareActive.expires_at_ms).toLocaleString()}
              </span>
            </>
          )}
          {!shareActive?.url && (
            <button onClick={() => void prepareShare()} disabled={shareBusy}>
              Создать новую ссылку
            </button>
          )}
          {shareMessage && <p className="board-share-message">{shareMessage}</p>}
          <div className="board-share-existing">
            <div>
              <strong>Выданные ссылки</strong>
              <button onClick={() => void refreshShares()}>Обновить</button>
            </div>
            {shareItems.length === 0 ? (
              <span>Список пуст или ещё не загружен.</span>
            ) : (
              shareItems.slice(0, 8).map((item) => (
                <div key={item.id} className="board-share-row">
                  <span>
                    до {new Date(item.expires_at_ms).toLocaleString()}
                    {item.revoked_at_ms ? " · отозвана" : ""}
                  </span>
                  {!item.revoked_at_ms && (
                    <button
                      onClick={() => void revokeShare(item.id)}
                      disabled={shareBusy}
                    >
                      Отозвать
                    </button>
                  )}
                </div>
              ))
            )}
          </div>
        </aside>
      )}

      {analysisOpen && canAnalyze && (
        <aside className="board-analysis-panel" aria-label="Сильный анализ">
          <div className="board-analysis-heading">
            <div>
              <strong>Сильный анализ</strong>
              <span>
                {analysisRun ? analysisRun.status : selected ? "выбран 1 объект" : "выберите объект"}
              </span>
            </div>
            <button onClick={() => setAnalysisOpen(false)}>Закрыть</button>
          </div>

          {!analysisRun && (
            <>
              <div className="board-analysis-fields">
                <label>
                  Модель
                  <select
                    value={analysisModel}
                    onChange={(event) =>
                      setAnalysisModel(event.target.value as AnalysisRun["model"])
                    }
                  >
                    <option value="kimi_k3">Kimi K3</option>
                    <option value="deepseek">DeepSeek</option>
                    <option value="council_pro">Консилиум · Kimi + DeepSeek · NVIDIA</option>
                  </select>
                </label>
                <label>
                  Режим
                  <select
                    value={analysisPurpose}
                    onChange={(event) =>
                      setAnalysisPurpose(event.target.value as AnalysisRun["purpose"])
                    }
                  >
                    <option value="edge_cases">Риски и edge cases</option>
                    <option value="requirements">Требования</option>
                    <option value="architecture">Архитектура</option>
                    <option value="ideas">Идеи</option>
                    <option value="code_review">Ревью</option>
                  </select>
                </label>
              </div>
              <textarea
                value={analysisQuestion}
                onChange={(event) => setAnalysisQuestion(event.target.value)}
                placeholder="Что проверить в выбранном объекте?"
              />
              <button
                onClick={() => void startSelectedAnalysis()}
                disabled={!selected || analysisBusy}
              >
                {analysisBusy ? "Запускаю…" : "Запустить анализ"}
              </button>
              <span className="board-analysis-note">
                Модель получает только замороженную версию выбранного объекта, без доступа к проектным файлам.
              </span>
              {analysisModel === "council_pro" && (
                <span className="board-analysis-note">
                  NVIDIA-консилиум запускается автоматически: Kimi K3 + DeepSeek, максимум два
                  параллельных NVIDIA inference-вызова на двух независимых credential/project slot.
                </span>
              )}
            </>
          )}

          {analysisRun && (
            <>
              <div className="board-analysis-meta">
                <span>{analysisRun.model} · {analysisRun.purpose}</span>
                {analysisRun.source_changed && (
                  <strong>Источник на доске изменился после запуска</strong>
                )}
                {analysisRun.error_code && <strong>{analysisRun.error_code}</strong>}
              </div>
              {analysisRun.status === "completed" && analysisRun.result_markdown ? (
                <pre className="board-analysis-report">{analysisRun.result_markdown}</pre>
              ) : (
                <p className="board-analysis-wait">
                  {analysisRun.status === "waiting_capacity"
                    ? "Два NVIDIA-слота заняты. Консилиум ждёт свободный слот…"
                    : analysisRun.status === "dispatch_unknown"
                      ? "Уточняю исход запуска без повторной отправки…"
                      : analysisRun.status === "failed"
                          ? "Анализ завершился ошибкой."
                          : analysisRun.status === "cancelled"
                            ? "Анализ отменён."
                            : "Анализ выполняется…"}
                </p>
              )}
              {analysisRun.status === "completed" && (
                <div className="board-analysis-materialization">
                  <strong>GitHub-копия отчёта</strong>
                  {!materialization && <span>Проверяю document binding…</span>}
                  {materialization?.status === "not_configured" && (
                    <span>
                      GitHub document binding не настроен. Внутренний Markdown уже сохранён и остаётся доступен.
                    </span>
                  )}
                  {materialization?.status === "ready" && (
                    <>
                      <span>
                        Отчёт ещё не синхронизирован. Canonical-версия остаётся в Projects Hub.
                      </span>
                      {(materialization.eligible_repository_ids?.length ?? 0) <= 1 ? (
                        <button
                          onClick={() =>
                            void materializeCurrentAnalysis(
                              materialization.eligible_repository_ids?.[0],
                            )
                          }
                          disabled={materializationBusy}
                        >
                          Синхронизировать в GitHub
                        </button>
                      ) : (
                        <div className="board-materialization-targets">
                          <span>Выберите разрешённый generated-artifacts репозиторий:</span>
                          {materialization.eligible_repository_ids?.map((repositoryId) => (
                            <button
                              key={repositoryId}
                              onClick={() => void materializeCurrentAnalysis(repositoryId)}
                              disabled={materializationBusy}
                            >
                              Репозиторий #{repositoryId}
                            </button>
                          ))}
                        </div>
                      )}
                    </>
                  )}
                  {materialization?.status === "confirmation_required" && (
                    <>
                      <span>
                        Целевой репозиторий публичный. Следующее действие опубликует полный текст отчёта в публичном GitHub.
                      </span>
                      <button
                        onClick={() =>
                          void materializeCurrentAnalysis(
                            materialization.repository_id,
                            true,
                          )
                        }
                        disabled={materializationBusy}
                      >
                        Опубликовать отчёт в публичный GitHub
                      </button>
                    </>
                  )}
                  {materialization?.status === "pending" && (
                    <span>GitHub-синхронизация выполняется…</span>
                  )}
                  {materialization?.status === "failed" && (
                    <>
                      <span>
                        GitHub-синхронизация не удалась ({materialization.error_code || "unknown"}).
                        Внутренний Markdown не потерян.
                      </span>
                      <button
                        onClick={() =>
                          void materializeCurrentAnalysis(materialization.repository_id)
                        }
                        disabled={materializationBusy}
                      >
                        Повторить синхронизацию
                      </button>
                    </>
                  )}
                  {materialization?.status === "synced" && (
                    <>
                      <span>
                        {materialization.full_name} · {materialization.path}
                        {materialization.reused ? " · без нового commit" : ""}
                      </span>
                      {materialization.commit_sha && (
                        <code>{materialization.commit_sha.slice(0, 12)}</code>
                      )}
                    </>
                  )}
                </div>
              )}
              <div className="board-analysis-actions">
                {!["completed", "failed", "cancelled", "blocked"].includes(analysisRun.status) && (
                  <button onClick={() => void cancelCurrentAnalysis()} disabled={analysisBusy}>
                    Отменить
                  </button>
                )}
                {analysisRun.status === "completed" && (
                  <>
                    <button onClick={() => void publishCurrentAnalysis()} disabled={analysisBusy}>
                      Добавить на доску
                    </button>
                    <a
                      href={analysisReportUrl(workspaceId, analysisRun.id)}
                      target="_blank"
                      rel="noreferrer"
                    >
                      Markdown
                    </a>
                  </>
                )}
                <button
                  onClick={() => {
                    setAnalysisRun(null);
                    setMaterialization(null);
                  }}
                >
                  Новый анализ
                </button>
              </div>
            </>
          )}
        </aside>
      )}

      {selected && (
        <aside className="board-inspector">
          <div>
            <strong>Выбрано</strong>
            <span>
              r{selected.object_revision} · {selected.style.color}
            </span>
          </div>
          {canAnalyze && selected.type !== "document_card" && (
            <button
              onClick={() => {
                setAnalysisOpen(true);
                setAnalysisRun(null);
              }}
            >
              Анализировать
            </button>
          )}
          {canAnalyze && analysisReferenceId && (
            <button
              onClick={() =>
                void getAnalysisRun(workspaceId, analysisReferenceId)
                  .then((run) => {
                    setAnalysisRun(run);
                    setAnalysisOpen(true);
                  })
                  .catch((error) =>
                    setStatus(error instanceof Error ? error.message : "Отчёт недоступен"),
                  )
              }
            >
              Открыть отчёт
            </button>
          )}
          <div className="board-history">
            {history.slice(0, 6).map((event) => (
              <span key={event.event_id}>
                #{event.board_seq} {event.operation} ·{" "}
                {new Date(event.server_time_ms).toLocaleTimeString()}
              </span>
            ))}
          </div>
        </aside>
      )}
    </section>
  );
}
