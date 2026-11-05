/** Runtime configuration is public; credentials never belong in this document. */
export type HostedSession = { access_token: string; refresh_token: string; expires_in: number; user: { id: string } };
export type HostedConfig = {
  auth: { mode: "builtin" | "external"; sessionKey: string; loginUrl?: string; refreshPath: string; logoutPath?: string };
  apiBase: string;
};
const CACHE_KEY = "qy_hosted_config:v1";
const LEGACY: HostedConfig = { auth: { mode: "external", sessionKey: "ylab.console.session.v1", loginUrl: "/iam/?return=/quarry/", refreshPath: "/api/iam/auth/refresh" }, apiBase: "/api/db" };
let config = LEGACY;
let configurationError: string | null = null;
export const hostedConfig = () => config;
export const hostedConfigurationError = () => configurationError;

function sameOriginPath(value: unknown): string {
  if (typeof value !== "string" || !value.startsWith("/") || value.startsWith("//") || Array.from(value).some((char) => char === "\\" || char.charCodeAt(0) <= 32) || new URL(value, location.origin).origin !== location.origin) throw new Error("Invalid hosted authentication path");
  return value;
}
function validate(value: unknown): HostedConfig {
  const candidate = value as HostedConfig;
  if (!["builtin", "external"].includes(candidate?.auth?.mode) || typeof candidate.auth.sessionKey !== "string" || !candidate.auth.sessionKey || candidate.auth.sessionKey.length > 200) throw new Error("Invalid hosted authentication configuration");
  const apiBase = sameOriginPath(candidate.apiBase);
  if (apiBase.includes("?") || apiBase.includes("#") || apiBase.endsWith("/")) throw new Error("Invalid hosted API path");
  return { apiBase, auth: {
    mode: candidate.auth.mode, sessionKey: candidate.auth.sessionKey,
    refreshPath: sameOriginPath(candidate.auth.refreshPath),
    ...(candidate.auth.loginUrl ? { loginUrl: sameOriginPath(candidate.auth.loginUrl) } : {}),
    ...(candidate.auth.logoutPath ? { logoutPath: sameOriginPath(candidate.auth.logoutPath) } : {}),
  } };
}
export async function loadHostedConfig(): Promise<void> {
  let response: Response | undefined;
  try {
    response = await fetch("/api/hosted/config", { cache: "no-store", signal: AbortSignal.timeout(2500) });
    // SDK hosts predating the official server retain their existing adapter.
    if (response.status === 404) {
      const cached = localStorage.getItem(CACHE_KEY);
      config = cached ? validate(JSON.parse(cached)) : LEGACY;
      return;
    }
    if (!response.ok) throw new Error("Hosted service unavailable");
    config = validate(await response.json());
    try { localStorage.setItem(CACHE_KEY, JSON.stringify(config)); } catch { /* Offline editing remains optional. */ }
  } catch (error) {
    // A malformed live document fails closed; cached settings are for outages.
    if (response?.ok) { configurationError = String((error as Error).message); return; }
    try {
      const cached = localStorage.getItem(CACHE_KEY);
      if (cached) { config = validate(JSON.parse(cached)); return; }
    } catch { /* An invalid cache cannot change the authentication destination. */ }
    // Offline legacy SDK pages have no runtime document to cache.
    if (!response && readHostedSession()) return;
    configurationError = String((error as Error).message);
  }
}
export function readHostedSession(): HostedSession | null {
  try {
    const value = JSON.parse(localStorage.getItem(config.auth.sessionKey) || "null") as HostedSession | null;
    return value?.user?.id && typeof value.access_token === "string" && !!value.access_token && typeof value.refresh_token === "string" && !!value.refresh_token ? value : null;
  } catch { return null; }
}
export function saveHostedSession(value: unknown): HostedSession {
  const session = value as HostedSession;
  if (!session?.user?.id || typeof session.access_token !== "string" || !session.access_token || typeof session.refresh_token !== "string" || !session.refresh_token) throw new Error("Invalid sign-in response");
  localStorage.setItem(config.auth.sessionKey, JSON.stringify(session));
  return session;
}
export async function signIn(username: string, password: string): Promise<void> {
  if (config.auth.mode !== "builtin") throw new Error("Use the platform sign-in page");
  const response = await fetch("/api/hosted/auth/login", { method: "POST", cache: "no-store", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ username, password }), signal: AbortSignal.timeout(10000) });
  if (!response.ok) throw new Error("Sign-in failed");
  saveHostedSession(await response.json());
  location.reload();
}
export async function consumeHostedHandoff(code: string): Promise<void> {
  if (config.auth.mode !== "builtin") throw new Error("Unexpected hosted sign-in handoff");
  const before = readHostedSession();
  const response = await fetch("/api/hosted/auth/handoff/consume", { method: "POST", cache: "no-store", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ code }), signal: AbortSignal.timeout(10000) });
  if (!response.ok) throw new Error("Expired hosted sign-in handoff");
  const next = await response.json() as HostedSession;
  if (before && next.user?.id !== before.user.id) throw new Error("The signed-in identity changed");
  saveHostedSession(next);
}
export async function signOut(): Promise<void> {
  const session = readHostedSession();
  const path = config.auth.logoutPath;
  if (session && path) {
    const response = await fetch(path, { method: "POST", cache: "no-store", headers: { "Content-Type": "application/json", Authorization: `Bearer ${session.access_token}` }, body: JSON.stringify({ refresh_token: session.refresh_token }), signal: AbortSignal.timeout(10000) });
    if (!response.ok) throw new Error("Sign-out failed");
  }
  localStorage.removeItem(config.auth.sessionKey);
  location.reload();
}
