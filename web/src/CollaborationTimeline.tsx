import { useCallback, useEffect, useMemo, useState } from "react";
import {
  getCollaborationTimeline,
  getProjectNote,
  getProjectNoteReplies,
  postProjectNoteReply,
  type CollaborationEvent,
  type ProjectNote,
  type ProjectReply,
} from "./api";

type Props = {
  workspaceId: string;
  actorId: string;
  refreshKey: number;
};

function noteIdFor(event: CollaborationEvent) {
  if (event.object_kind === "note") return event.object_id;
  if (event.object_kind === "reply") return event.parent_object_id;
  return null;
}

export default function CollaborationTimeline({ workspaceId, actorId, refreshKey }: Props) {
  const [events, setEvents] = useState<CollaborationEvent[]>([]);
  const [showGeneral, setShowGeneral] = useState(false);
  const [opened, setOpened] = useState<ProjectNote | null>(null);
  const [replies, setReplies] = useState<ProjectReply[]>([]);
  const [replyText, setReplyText] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    const value = await getCollaborationTimeline(workspaceId);
    setEvents(value.items);
  }, [workspaceId]);

  useEffect(() => {
    let cancelled = false;
    const run = async () => {
      try {
        const value = await getCollaborationTimeline(workspaceId);
        if (!cancelled) setEvents(value.items);
      } catch {
        // Main conversation remains usable when collaboration refresh is unavailable.
      }
    };
    void run();
    const timer = window.setInterval(run, 5000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [workspaceId, refreshKey]);

  const personal = useMemo(
    () => events.filter(item =>
      item.actor_id === actorId || item.addressed_to_actor_id === actorId
    ),
    [actorId, events],
  );
  const general = useMemo(
    () => events.filter(item =>
      item.actor_id !== actorId && item.addressed_to_actor_id == null
    ),
    [actorId, events],
  );
  const visible = showGeneral ? [...personal, ...general] : personal;
  visible.sort((a, b) => a.id - b.id);

  const openEvent = useCallback(async (event: CollaborationEvent) => {
    const noteId = noteIdFor(event);
    if (!noteId) return;
    setBusy(true);
    setNotice(null);
    try {
      const [note, thread] = await Promise.all([
        getProjectNote(workspaceId, noteId),
        getProjectNoteReplies(workspaceId, noteId),
      ]);
      setOpened(note);
      setReplies(thread.items);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "Не удалось открыть заметку.");
    } finally {
      setBusy(false);
    }
  }, [workspaceId]);

  const sendReply = useCallback(async () => {
    if (!opened || !replyText.trim()) return;
    setBusy(true);
    setNotice(null);
    try {
      await postProjectNoteReply(workspaceId, opened.id, replyText.trim());
      setReplyText("");
      const thread = await getProjectNoteReplies(workspaceId, opened.id);
      setReplies(thread.items);
      await refresh();
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "Не удалось отправить ответ.");
    } finally {
      setBusy(false);
    }
  }, [opened, refresh, replyText, workspaceId]);

  if (!events.length) return null;

  return (
    <div className="collaboration-inline" aria-label="Совместная работа">
      {visible.map(event => (
        <article
          className={"collaboration-widget " + (
            event.addressed_to_actor_id === actorId ? "addressed" : ""
          )}
          key={event.id}
        >
          <div className="collaboration-widget-head">
            <span>{event.project_name}</span>
            <small>{event.kind === "note_replied" ? "Ответ на заметку" : "Проектная заметка"}</small>
          </div>
          <strong>{event.summary}</strong>
          {noteIdFor(event) && (
            <button
              className="mini-action"
              disabled={busy}
              onClick={() => void openEvent(event)}
            >
              Открыть
            </button>
          )}
        </article>
      ))}
      {!showGeneral && general.length > 0 && (
        <button className="collaboration-general-toggle" onClick={() => setShowGeneral(true)}>
          Общие новости · {general.length}
        </button>
      )}
      {showGeneral && general.length > 0 && (
        <button className="collaboration-general-toggle" onClick={() => setShowGeneral(false)}>
          Скрыть общие новости
        </button>
      )}
      {opened && (
        <article className="collaboration-thread">
          <div className="collaboration-widget-head">
            <span>{opened.author.display_name}</span>
            <small>{opened.repository.full_name} · {opened.repository.path}</small>
          </div>
          <h3>{opened.title}</h3>
          <p>{opened.body}</p>
          <div className="collaboration-replies">
            {replies.map(item => (
              <div className="collaboration-reply" key={item.id}>
                <strong>{item.author.display_name}</strong>
                <p>{item.body}</p>
              </div>
            ))}
          </div>
          <div className="collaboration-reply-box">
            <textarea
              value={replyText}
              onChange={event => setReplyText(event.target.value)}
              placeholder="Ответить в обсуждении…"
              maxLength={12000}
            />
            <button className="mini-action" onClick={() => void sendReply()} disabled={busy || !replyText.trim()}>
              Ответить
            </button>
          </div>
          {notice && <span className="message-delivery-note">{notice}</span>}
          <button className="quiet-button" onClick={() => setOpened(null)}>Закрыть</button>
        </article>
      )}
    </div>
  );
}
