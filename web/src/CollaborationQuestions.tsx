import { useCallback, useEffect, useMemo, useState } from "react";
import {
  answerCollaborationQuestions,
  answerSingleCollaborationQuestion,
  getCollaborationQuestions,
  type CollaborationQuestion,
} from "./api";

type Props = {
  workspaceId: string;
  refreshKey: number;
  onChanged?: () => void;
};

export default function CollaborationQuestions({ workspaceId, refreshKey, onChanged }: Props) {
  const [questions, setQuestions] = useState<CollaborationQuestion[]>([]);
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [busyId, setBusyId] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(async () => {
    const result = await getCollaborationQuestions(workspaceId);
    setQuestions(result.items);
  }, [workspaceId]);

  useEffect(() => {
    let cancelled = false;
    const run = async () => {
      try {
        const result = await getCollaborationQuestions(workspaceId);
        if (!cancelled) setQuestions(result.items);
      } catch {
        // Collaboration widgets fail open; the main Live conversation stays usable.
      }
    };
    void run();
    const timer = window.setInterval(run, 5000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [load, refreshKey, workspaceId]);

  const active = useMemo(
    () => questions.filter(item => item.state === "open" || item.state === "deferred"),
    [questions],
  );

  const respond = useCallback(async (
    question: CollaborationQuestion,
    disposition: "answer" | "unknown" | "skip" | "later",
  ) => {
    const body = (answers[question.id] ?? "").trim();
    if (disposition === "answer" && !body) {
      setNotice("Введите ответ на этот вопрос.");
      return;
    }
    setBusyId(question.id);
    setNotice(null);
    try {
      if (question.source_kind === "owner_development") {
        await answerSingleCollaborationQuestion(
          workspaceId,
          question.id,
          disposition,
          body,
        );
      } else {
        await answerCollaborationQuestions(
          workspaceId,
          question.analysis_id,
          [{
            question_id: question.id,
            disposition,
            body,
          }],
        );
      }
      setAnswers(previous => ({ ...previous, [question.id]: "" }));
      await load();
      onChanged?.();
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "Не удалось сохранить ответ.");
    } finally {
      setBusyId(null);
    }
  }, [answers, load, onChanged, workspaceId]);

  if (!active.length) return null;

  return (
    <div className="collaboration-questions" aria-label="Вопросы для вас">
      {active.map(question => (
        <article
          className={"collaboration-question " + (question.blocking ? "blocking" : "nonblocking")}
          key={question.id}
        >
          <div className="collaboration-widget-head">
            <span>
              {question.source_kind === "owner_development"
                ? "Нужно решение владельца"
                : question.blocking ? "Нужен ответ" : "Можно уточнить позже"}
            </span>
            <small>{question.addressed_role}</small>
          </div>
          {question.shared_context && <p className="question-context">{question.shared_context}</p>}
          <strong>{question.prompt}</strong>
          {question.state === "deferred" && (
            <small>Отложено — вопрос остаётся незакрытым.</small>
          )}
          <textarea
            value={answers[question.id] ?? ""}
            onChange={event => setAnswers(previous => ({
              ...previous,
              [question.id]: event.target.value,
            }))}
            placeholder="Ответ на этот вопрос…"
            maxLength={12000}
          />
          <div className="question-actions">
            <button
              className="mini-action"
              disabled={busyId === question.id}
              onClick={() => void respond(question, "answer")}
            >
              Ответить
            </button>
            <button
              className="mini-action"
              disabled={busyId === question.id}
              onClick={() => void respond(question, "unknown")}
            >
              Не знаю
            </button>
            <button
              className="mini-action"
              disabled={busyId === question.id}
              onClick={() => void respond(question, "skip")}
            >
              Пропустить
            </button>
            <button
              className="mini-action"
              disabled={busyId === question.id}
              onClick={() => void respond(question, "later")}
            >
              Позже
            </button>
          </div>
        </article>
      ))}
      {notice && <span className="message-delivery-note">{notice}</span>}
    </div>
  );
}
