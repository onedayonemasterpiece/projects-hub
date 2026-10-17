import { useEffect, useState } from "react";
import { api } from "./api";
import { analysisResultId, safeResultLink } from "./analysisResultTarget.js";
import "./analysis-result.css";

type Result = {
  id: string;
  result_markdown: string;
  result: { summary?: string; artifact_url?: string } | null;
  delivery_links?: {
    source_note_url?: string | null;
    source_notes?: Array<{ url: string; snapshot_url?: string; path?: string }>;
    slack_result_url?: string | null;
    slack_thread_url?: string | null;
  };
};

function currentTarget() {
  return analysisResultId(new URLSearchParams(window.location.search).get("analysis_result"));
}

export default function AnalysisResult({ workspaceId }: { workspaceId: string }) {
  const [id, setId] = useState(currentTarget);
  const [result, setResult] = useState<Result | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    const open = (event: Event) => {
      const next = analysisResultId((event as CustomEvent).detail);
      if (next) setId(next);
    };
    const navigate = () => setId(currentTarget());
    window.addEventListener("projects-hub-analysis-result", open);
    window.addEventListener("popstate", navigate);
    return () => {
      window.removeEventListener("projects-hub-analysis-result", open);
      window.removeEventListener("popstate", navigate);
    };
  }, []);
  useEffect(() => {
    let cancelled = false;
    setResult(null);
    setError(null);
    if (id) {
      const query = new URLSearchParams({ workspace_id: workspaceId });
      void api<Result>(`/api/collaboration/analyses/${encodeURIComponent(id)}?${query}`)
        .then(value => { if (!cancelled) setResult(value); })
        .catch(() => { if (!cancelled) setError("Результат недоступен. Проверьте вход и доступ к проекту."); });
    }
    return () => { cancelled = true; };
  }, [id, workspaceId]);
  if (!id) return null;
  const close = () => {
    setId(null);
    const url = new URL(window.location.href);
    url.searchParams.delete("analysis_result");
    window.history.replaceState({}, "", url.pathname + url.search + url.hash);
  };
  const links = result?.delivery_links;
  const sources = links?.source_notes?.length ? links.source_notes
    : links?.source_note_url ? [{ url: links.source_note_url }] : [];
  const link = (value: unknown, label: string) => {
    const href = safeResultLink(value);
    return href ? <a href={href} target="_blank" rel="noopener noreferrer">{label}</a> : null;
  };
  return <section className="analysis-result" role="dialog" aria-label="Результат аналитики">
    <header><strong>Результат аналитики</strong><button onClick={close}>Закрыть</button></header>
    {error ? <p role="alert">{error}</p> : !result ? <p role="status">Загружаю результат…</p> : <>
      <p>{result.result?.summary || "Анализ готов. Можно попросить Миру кратко озвучить результат."}</p>
      <nav aria-label="Источники результата">
        {link(result.result?.artifact_url, "Полный разбор в GitHub")}
        {sources.map((source, index) => <span key={source.url}>
          {link(source.url, `Исходная заметка${sources.length > 1 ? ` ${index + 1}` : ""} (.md)`)}
          {link(source.snapshot_url, "Снимок на момент анализа")}
        </span>)}
        {link(links?.slack_result_url, "Разбор в Slack")}
        {link(links?.slack_thread_url, "Тред в Slack")}
      </nav>
      <details><summary>Прочитать результат здесь</summary><pre>{result.result_markdown || "Текст результата пока недоступен."}</pre></details>
    </>}
  </section>;
}
