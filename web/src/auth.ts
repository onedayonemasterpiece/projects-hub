import { createClient, type SupabaseClient } from "@supabase/supabase-js";
import {
  exchangePublicAuth,
  type AuthConfig,
  type Bootstrap,
} from "./api";

type YandexAuthConfig = Extract<AuthConfig, { mode: "yandex_pkce" }>;

const CALLBACK_KEYS = [
  "code",
  "error",
  "error_code",
  "error_description",
  "state",
  "sb",
];
const PKCE_COOKIE_PREFIX = "ph_pkce_";
const CALLBACK_TIMEOUT_MS = 20_000;
const SESSION_TIMEOUT_MS = 8_000;

function isVerifierKey(key: string): boolean {
  return key.endsWith("-code-verifier");
}

function cookieName(key: string): string {
  return (
    PKCE_COOKIE_PREFIX +
    encodeURIComponent(key).replaceAll("%", "_")
  );
}

function setVerifierCookie(key: string, value: string): void {
  if (!isVerifierKey(key)) return;
  document.cookie =
    cookieName(key) +
    "=" +
    encodeURIComponent(value) +
    "; Max-Age=900; Path=/; SameSite=Lax; Secure";
}

function getVerifierCookie(key: string): string | null {
  if (!isVerifierKey(key)) return null;
  const prefix = cookieName(key) + "=";
  const item = document.cookie
    .split("; ")
    .find(part => part.startsWith(prefix));
  if (!item) return null;
  try {
    return decodeURIComponent(item.slice(prefix.length));
  } catch {
    return null;
  }
}

function removeVerifierCookie(key: string): void {
  if (!isVerifierKey(key)) return;
  document.cookie =
    cookieName(key) +
    "=; Max-Age=0; Path=/; SameSite=Lax; Secure";
}

const authStorage = {
  getItem(key: string): string | null {
    try {
      const value = window.localStorage.getItem(key);
      if (value !== null) return value;
    } catch {
      // Continue to the short-lived PKCE cookie fallback.
    }
    return getVerifierCookie(key);
  },
  setItem(key: string, value: string): void {
    try {
      window.localStorage.setItem(key, value);
    } catch {
      // The cookie fallback is enough for the verifier round-trip.
    }
    setVerifierCookie(key, value);
  },
  removeItem(key: string): void {
    try {
      window.localStorage.removeItem(key);
    } catch {
      // Best effort local cleanup.
    }
    removeVerifierCookie(key);
  },
};

function purgeVerifierCookies(): void {
  for (const item of document.cookie.split(";")) {
    const name = item.split("=", 1)[0]?.trim() ?? "";
    if (!name.startsWith(PKCE_COOKIE_PREFIX)) continue;
    document.cookie =
      name +
      "=; Max-Age=0; Path=/; SameSite=Lax; Secure";
  }
}

function withTimeout<T>(
  promise: PromiseLike<T>,
  timeoutMs: number,
  message: string,
): Promise<T> {
  let timer = 0;
  const timeout = new Promise<never>((_resolve, reject) => {
    timer = window.setTimeout(
      () => reject(new Error(message)),
      timeoutMs,
    );
  });
  return Promise.race([Promise.resolve(promise), timeout])
    .finally(() => window.clearTimeout(timer));
}

function cleanCallbackUrl(value = window.location.href): string {
  const url = new URL(value);
  for (const key of CALLBACK_KEYS) {
    url.searchParams.delete(key);
  }
  url.hash = "";
  return url.toString();
}

function cleanCallbackHistory(): void {
  const clean = cleanCallbackUrl();
  if (clean !== window.location.href) {
    window.history.replaceState(
      window.history.state,
      "",
      clean,
    );
  }
}

function createAuthClient(
  config: YandexAuthConfig,
): SupabaseClient {
  return createClient(
    config.supabase_url,
    config.publishable_key,
    {
      auth: {
        persistSession: true,
        autoRefreshToken: true,
        detectSessionInUrl: false,
        flowType: "pkce",
        storage: authStorage,
        storageKey: "projects-hub-yandex-auth-v1",
      },
    },
  );
}

async function exchangeAndForget(
  client: SupabaseClient,
  accessToken: string,
): Promise<Bootstrap> {
  const bootstrap = await exchangePublicAuth(accessToken);
  try {
    await client.auth.signOut({ scope: "local" });
  } catch {
    // Projects Hub HttpOnly session is already authoritative.
  }
  purgeVerifierCookies();
  return bootstrap;
}

export async function finishPublicAuth(
  config: YandexAuthConfig,
): Promise<Bootstrap | null> {
  const params = new URL(window.location.href).searchParams;
  const providerError =
    params.get("error_description") ||
    params.get("error_code") ||
    params.get("error");
  if (providerError) {
    cleanCallbackHistory();
    throw new Error(
      "Вход через Яндекс не завершён. Попробуйте ещё раз.",
    );
  }

  const code = params.get("code");
  if (!code) return null;

  const client = createAuthClient(config);
  try {
    const { data, error } = await withTimeout(
      client.auth.exchangeCodeForSession(code),
      CALLBACK_TIMEOUT_MS,
      "auth_callback_timeout",
    );
    if (error || !data.session?.access_token) {
      throw error ?? new Error("auth_callback_no_session");
    }
    if (data.session.refresh_token) {
      await client.auth.setSession({
        access_token: data.session.access_token,
        refresh_token: data.session.refresh_token,
      });
    }
    cleanCallbackHistory();
    return await exchangeAndForget(
      client,
      data.session.access_token,
    );
  } catch (error) {
    cleanCallbackHistory();
    const message =
      error instanceof Error ? error.message : String(error);
    if (message === "auth_callback_timeout") {
      throw new Error(
        "Вход через Яндекс не завершён: браузер не получил ответ. Попробуйте ещё раз.",
      );
    }
    if (/code verifier|flow state|auth code|invalid|pkce/i.test(message)) {
      throw new Error(
        "Сессия входа через Яндекс устарела. Запустите вход ещё раз с этой страницы.",
      );
    }
    throw new Error(
      "Не удалось завершить вход через Яндекс.",
    );
  }
}

export async function recoverPublicAuth(
  config: YandexAuthConfig,
): Promise<Bootstrap | null> {
  const client = createAuthClient(config);
  try {
    const { data } = await withTimeout(
      client.auth.getSession(),
      SESSION_TIMEOUT_MS,
      "auth_session_timeout",
    );
    if (!data.session?.access_token) return null;
    return await exchangeAndForget(
      client,
      data.session.access_token,
    );
  } catch {
    return null;
  }
}

export async function startPublicAuth(
  config: YandexAuthConfig,
): Promise<void> {
  const client = createAuthClient(config);
  const redirectTo = cleanCallbackUrl(config.redirect_url);
  const { error } = await client.auth.signInWithOAuth({
    provider: config.provider as any,
    options: {
      redirectTo,
      skipBrowserRedirect: false,
    },
  });
  if (error) {
    throw new Error(
      "Не удалось открыть вход через Яндекс.",
    );
  }
}
