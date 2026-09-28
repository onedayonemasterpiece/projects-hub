import { createClient, type SupabaseClient } from "@supabase/supabase-js";
import {
  exchangePublicAuth,
  type AuthConfig,
  type Bootstrap,
} from "./api";

type YandexAuthConfig = Extract<AuthConfig, { mode: "yandex_pkce" }>;

const CALLBACK_KEYS = ["code", "error", "error_code", "error_description", "state"];

function createAuthClient(config: YandexAuthConfig): SupabaseClient {
  return createClient(config.supabase_url, config.publishable_key, {
    auth: {
      persistSession: true,
      autoRefreshToken: false,
      detectSessionInUrl: false,
      flowType: "pkce",
    },
  });
}

function cleanCallbackUrl(): void {
  const url = new URL(window.location.href);
  let changed = false;
  for (const key of CALLBACK_KEYS) {
    if (url.searchParams.has(key)) {
      url.searchParams.delete(key);
      changed = true;
    }
  }
  url.hash = "";
  if (changed) {
    window.history.replaceState(window.history.state, "", url.toString());
  }
}

export async function finishPublicAuth(
  config: YandexAuthConfig,
): Promise<Bootstrap | null> {
  const params = new URL(window.location.href).searchParams;
  const providerError =
    params.get("error_description") || params.get("error_code") || params.get("error");
  if (providerError) {
    cleanCallbackUrl();
    throw new Error("Вход через Яндекс не завершён. Попробуйте ещё раз.");
  }
  const code = params.get("code");
  if (!code) return null;

  const client = createAuthClient(config);
  const { data, error } = await client.auth.exchangeCodeForSession(code);
  cleanCallbackUrl();
  if (error || !data.session?.access_token) {
    throw new Error("Не удалось завершить вход через Яндекс.");
  }

  const result = await exchangePublicAuth(data.session.access_token);
  try {
    await client.auth.signOut({ scope: "local" });
  } catch {
    // Projects Hub server session is already authoritative. Best-effort local cleanup.
  }
  return result;
}

export async function startPublicAuth(config: YandexAuthConfig): Promise<void> {
  const client = createAuthClient(config);
  const { error } = await client.auth.signInWithOAuth({
    provider: config.provider as any,
    options: {
      redirectTo: config.redirect_url,
      skipBrowserRedirect: false,
    },
  });
  if (error) {
    throw new Error("Не удалось открыть вход через Яндекс.");
  }
}
