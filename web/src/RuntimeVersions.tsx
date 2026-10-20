import { useEffect, useMemo, useState } from "react";

type Health = {
  version: string | null;
  release_sha: string | null;
  live_sdk: { version: string | null; source_sha: string | null };
};
const value = (x: unknown): string | null => typeof x === "string" && /^[0-9A-Za-z._+-]{1,64}$/.test(x) ? x : null;
const sha = (x: unknown): string | null => typeof x === "string" && /^[0-9a-f]{40}$/.test(x) ? x : null;
const shown = (x: string | null) => x ?? "не сообщена";

export default function RuntimeVersions({ nativeVersion }: { nativeVersion: string | null }) {
  const native = useMemo(() => {
    const q = new URLSearchParams(window.location.search);
    return {
      version: value(nativeVersion), code: value(q.get("native_code")),
      source: sha(q.get("native_sha")), sdk: value(q.get("native_sdk")),
      sdkSource: sha(q.get("native_sdk_sha")),
    };
  }, [nativeVersion]);
  const [health, setHealth] = useState<Health | null>(null);
  const [status, setStatus] = useState("Проверяю работающий сервер…");
  useEffect(() => {
    let active = true;
    let controller: AbortController | null = null;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const refresh = async () => {
      controller?.abort();
      if (timer !== null) clearTimeout(timer);
      const request = new AbortController();
      controller = request;
      timer = setTimeout(() => request.abort(), 7000);
      try {
        const response = await fetch("/healthz", { cache: "no-store", signal: request.signal });
        if (!response.ok) throw new Error("server unavailable");
        const data = await response.json();
        if (!active || controller !== request) return;
        setHealth({ version: value(data.version), release_sha: sha(data.release_sha),
          live_sdk: { version: value(data.live_sdk?.version), source_sha: sha(data.live_sdk?.source_sha) } });
        setStatus("Версия работающего сервера подтверждена");
      } catch {
        if (!active || controller !== request) return;
        setHealth(null);
        setStatus("Сервер недоступен — его версия сейчас не подтверждена");
      } finally {
        if (controller === request && timer !== null) clearTimeout(timer);
      }
    };
    const visible = () => { if (document.visibilityState === "visible") void refresh(); };
    void refresh();
    window.addEventListener("online", visible);
    document.addEventListener("visibilitychange", visible);
    return () => { active = false; controller?.abort(); if (timer !== null) clearTimeout(timer);
      window.removeEventListener("online", visible); document.removeEventListener("visibilitychange", visible); };
  }, []);
  return <details aria-label="Версии приложения и сервера" style={{ padding: "12px 0", overflowWrap: "anywhere" }}>
    <summary>Версии: {native.version ? `приложение ${native.version}` : "веб-клиент"} · сервер {shown(health?.version ?? null)}</summary>
    <p>{native.version ? `Android ${native.version}, сборка ${shown(native.code)}` : "Открыт веб-клиент, не Android-приложение"}</p>
    {native.version && <p>SDK приложения: {shown(native.sdk)}<br />Исходный код приложения: {shown(native.source)}<br />Исходный код SDK: {shown(native.sdkSource)}</p>}
    <p>Бэкенд: {shown(health?.version ?? null)}<br />Исходный код бэкенда: {shown(health?.release_sha ?? null)}<br />SDK бэкенда: {shown(health?.live_sdk.version ?? null)}<br />Исходный код SDK: {shown(health?.live_sdk.source_sha ?? null)}</p>
    <small role="status">{status}. Это запущенные компоненты, не версия доступного обновления.</small>
  </details>;
}
