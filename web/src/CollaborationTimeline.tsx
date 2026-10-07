import { useCallback, useEffect, useMemo, useState } from "react";
import {
  getCollaborationBrief,
  getCollaborationTimeline,
  markCollaborationBriefSeen,
  setCollaborationGeneralNews,
  getProjectNote,
  getProjectNoteReplies,
  postProjectNoteReply,
  type CollaborationBrief,
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
  const [brief, setBrief] = useState<CollaborationBrief | null>(null);
  const [showGeneral, setShowGeneral] = useState(false);
  const [opened, setOpened] = useState<ProjectNote | null>(null);
  const [replies, setReplies] = useState<ProjectReply[]>([]);
  const [replyText, setReplyText] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    const [value, nextBrief] = await Promise.all([
      getCollaborationTimeline(workspaceId),
      getCollaborationBrief(workspaceId),
    ]);
    setEvents(value.items);
    setBrief(nextBrief);
  }, [workspaceId]);

  useEffect(() => {
    let cancelled = false;
    const run = async () => {
      try {
        const [value, nextBrief] = await Promise.all([
          getCollaborationTimeline(workspaceId),
          getCollaborationBrief(workspaceId),
        ]);
        if (!cancelled) {
          setEvents(value.items);
          setBrief(nextBrief);
        }
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

  const markPersonalSeen = useCallback(async () => {
    if (!brief?.personal_through_id) return;
    await markCollaborationBriefSeen(
      workspaceId,
      brief.personal_through_id,
      undefined,
    );
    await refresh();
  }, [brief?.personal_through_id, refresh, workspaceId]);

  const revealGeneral = useCallback(async () => {
    setShowGeneral(true);
    if (brief?.general_through_id) {
      await markCollaborationBriefSeen(
        workspaceId,
        undefined,
        brief.general_through_id,
      );
      await refresh();
    }
  }, [brief?.general_through_id, refresh, workspaceId]);

  const setGeneralNews = useCallback(async (enabled: boolean) => {
    await setCollaborationGeneralNews(workspaceId, enabled);
    setShowGeneral(false);
    await refresh();
  }, [refresh, workspaceId]);

  if (!events.length) return null;

  return (
    <div className="collaboration-inline" aria-label="Совместная работа">
      {brief && brief.personal_unread_count > 0 && (
        <div className="collaboration-attention">
          <strong>Для вас · {brief.personal_unread_count}</strong>
          <button className="mini-action" onClick={() => void markPersonalSeen()}>
            Просмотрено
          </button>
        </div>
      )}
      {visible.map(event => (
        <article
          className={"collaboration-widget " + (
            event.addressed_to_actor_id === actorId ? "addressed" : ""
          )}
          key={event.id}
        >
          <div className="collaboration-widget-head">
            <span>{event.project_name}</span>
            <small>{event.kind === "note_chatgpt_analyzed" ? "Глубокий анализ ChatGPT" : event.kind === "note_replied" ? "Ответ на заметку" : "Проектная заметка"}</small>
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
      {!showGeneral && brief?.general_news_enabled && general.length > 0 && (
        <button className="collaboration-general-toggle" onClick={() => void revealGeneral()}>
          Общие новости · {brief.general_count || general.length}
        </button>
      )}
      {showGeneral && general.length > 0 && (
        <div className="collaboration-general-controls">
          <button className="collaboration-general-toggle" onClick={() => setShowGeneral(false)}>
            Скрыть общие новости
          </button>
          <button className="quiet-button" onClick={() => void setGeneralNews(false)}>
            Не показывать общие новости
          </button>
        </div>
      )}
      {brief && !brief.general_news_enabled && (
        <div className="collaboration-general-controls">
          <small>Общие новости скрыты. Лично адресованное остаётся включено.</small>
          <button className="quiet-button" onClick={() => void setGeneralNews(true)}>
            Включить общие новости
          </button>
        </div>
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
              <summary>Глубокий анализ ChatGPT · {opened.chatgpt_analysis.generated_at_utc.slice(0, 16)}</summary>
              <p className="question-context">Отдельная аналитика, не принятое решение. Версия исходника: {opened.chatgpt_analysis.source_sha.slice(0, 12)}.</p>
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
