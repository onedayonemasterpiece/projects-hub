export type ThemePreference = { theme: "light" | "dark"; revision: number };
export type NativeApplication = "applied" | "failed" | "unsupported" | "not_required";
export type ThemeApplication = ThemePreference & { web_status: "applied"; native_status: NativeApplication };
export type ThemeBinding = { version: 1; actor_id: string; conversation_id: string; session_id: string; command_id: string };
export function validPreference(value: unknown): value is ThemePreference;
export function createThemePreferences(options: {
  render: (value: ThemePreference, reset?: boolean) => Promise<{ web: boolean; native?: NativeApplication }>;
  acknowledge: (binding: ThemeBinding, value: ThemeApplication) => Promise<unknown>;
  onStatus?: (message: string) => void;
}): {
  reset(actor?: string | null): Promise<void>;
  apply(value: unknown, binding?: unknown, isCurrent?: () => boolean): Promise<boolean>;
  readonly actor: string | null;
  readonly preference: ThemePreference;
};
export function renderTheme(value: ThemePreference, reset?: boolean): Promise<{ web: boolean; native: NativeApplication }>;
