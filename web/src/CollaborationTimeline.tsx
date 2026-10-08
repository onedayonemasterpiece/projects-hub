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
  mode?: "activity" | "note";
  noteId?: string;
};

function relatedNoteId(event: CollaborationEvent): string | null {
  if (event.object_kind === "note") return event.object_id;
  if (event.object_kind === "reply") return event.parent_object_id;
  if (event.object_kind === "analysis") return event.parent_object_id;
  return null;
}

function activityLabel(event: CollaborationEvent): string {
  if (event.kind === "note_chatgpt_analyzed") return "ChatGPT завершил анализ заметки";
  if (event.kind === "continuation_completed") return "Завершено продолжение аналитики";
  if (event.kind === "note_replied") return "Участник ответил на заметку";
  return "Участник добавил заметку";
}

export default function CollaborationTimeline({
  workspaceId, actorId, refreshKey, mode = "activity", noteId,
}: Props) {
  const [events, setEvents] = useState<CollaborationEvent[]>([]);
  const [opened, setOpened] = useState<ProjectNote | null>(null);
  const [replies, setReplies] = useState<ProjectReply[]>([]);
  const [manualReply, setManualReply] = useState(false);
  const [replyText, setReplyText] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  const openNote = useCallback(async (id: string) => {
    setBusy(true);
    setNotice(null);
    try {
      const [note, thread] = await Promise.all([
        getProjectNote(workspaceId, id),
        getProjectNoteReplies(workspaceId, id),
      ]);
      setOpened(note);
      setReplies(thread.items);
      setManualReply(false);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "Не удалось открыть заметку.");
    } finally {
      setBusy(false);
    }
  }, [workspaceId]);

  useEffect(() => {
    if (mode !== "activity") return;
    let cancelled = false;
    const load = async () => {
      try {
        const result = await getCollaborationTimeline(workspaceId);
        if (!cancelled) setEvents(result.items);
      } catch {
        // A read-only auxiliary view must not interrupt Mira's conversation.
      }
    };
    void load();
    const timer = window.setInterval(load, 30000);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [workspaceId, mode, refreshKey]);

  useEffect(() => {
    if (mode !== "note") return;
    setOpened(null);
    if (noteId) void openNote(noteId);
  }, [mode, noteId, openNote]);

  // The explicit team view is a concise outcome feed, not every prior self-note,
  // internal question, or historical test event in the durable event log.
  const meaningful = useMemo(() =>
    events.filter(event =>
      event.kind === "note_chatgpt_analyzed"
      || (event.kind === "continuation_completed" && event.addressed_to_actor_id === actorId)
      || (["note_created", "note_replied"].includes(event.kind) && event.actor_id !== actorId)
    ).slice(-30),
    [events, actorId],
  );

  const sendReply = useCallback(async () => {
    if (!opened || !replyText.trim()) return;
    setBusy(true);
    setNotice(null);
    try {
      await postProjectNoteReply(workspaceId, opened.id, replyText.trim());
      setReplyText("");
      setManualReply(false);
      const next = await getProjectNoteReplies(workspaceId, opened.id);
      setReplies(next.items);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "Не удалось отправить ответ.");
    } finally {
      setBusy(false);
    }
  }, [opened, replyText, workspaceId]);

  return (
    <div className="collaboration-inline" aria-label="Совместная работа">
      {mode === "activity" && (
        <>
          {meaningful.length === 0 && (
            <p className="empty-copy">Нет новых заметок участников или завершённых результатов аналитики.</p>
          )}
          {meaningful.map(event => (
            <article className="collaboration-widget" key={event.id}>
              <div className="collaboration-widget-head">
                <span>{event.project_name}</span>
                <small>{activityLabel(event)}</small>
              </div>
              <strong>{event.summary}</strong>
              {relatedNoteId(event) && (
                <button className="mini-action" disabled={busy}
                  onClick={() => void openNote(relatedNoteId(event)!)}>
                  Открыть заметку
                </button>
              )}
            </article>
          ))}
        </>
      )}
      {opened && (
        <article className="collaboration-thread">
          <div className="collaboration-widget-head">
            <span>{opened.author.display_name}</span>
            <small>{opened.repository.full_name} · {opened.repository.path}</small>
          </div>
          <h3>{opened.title}</h3>
          <p>{opened.body}</p>
          {opened.chatgpt_analysis && (
            <details className="collaboration-chatgpt-analysis">
              <summary>Анализ ChatGPT · {opened.chatgpt_analysis.generated_at_utc.slice(0, 16)}</summary>
              <p className="question-context">Аналитические предложения, не принятые автоматически решения. Редакция: {opened.chatgpt_analysis.source_sha.slice(0, 12)}.</p>
              <div style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
                {opened.chatgpt_analysis.markdown}
              </div>
            </details>
          )}
          <div className="collaboration-replies">
            {replies.map(item => (
              <div className="collaboration-reply" key={item.id}>
                <strong>{item.author.display_name}</strong>
                <p>{item.body}</p>
              </div>
            ))}
          </div>
          <small>Можно ответить Мире голосом, назвав заметку.</small>
          <button className="quiet-button" onClick={() => setManualReply(value => !value)}>
            {manualReply ? "Скрыть ввод" : "Ответить текстом"}
          </button>
          {manualReply && (
            <div className="collaboration-reply-box">
              <textarea value={replyText}
                onChange={event => setReplyText(event.target.value)}
                placeholder="Необязательный ответ текстом…" maxLength={12000} />
              <button className="mini-action" onClick={() => void sendReply()}
                disabled={busy || !replyText.trim()}>Отправить ответ</button>
            </div>
          )}
          {mode === "activity" && (
            <button className="quiet-button" onClick={() => setOpened(null)}>
              Вернуться к новостям
            </button>
          )}
        </article>
      )}
      {mode === "note" && !opened && !notice && <p className="empty-copy">Загружаю заметку…</p>}
      {notice && <span className="message-delivery-note">{notice}</span>}
    </div>
  );
}
