import { t } from "./i18n";
import { NATIVE_MODE, nativePersonalStorage } from "./nativeRecords";
export { initializeNativeRecords } from "./nativeRecords";
import { consumeHostedHandoff, hostedConfig, hostedConfigurationError, loadHostedConfig, readHostedSession, saveHostedSession, type HostedSession } from "./hostedAuth";

/** Hosted workbench. Native Quarry uses its own same-origin API and storage. */
export const PLATFORM_MODE = typeof document !== "undefined" && document.querySelector('meta[name="quarry-mode"]')?.getAttribute("content") === "platform";
export const CLI_SESSION_MODE = PLATFORM_MODE && document.querySelector('meta[name="quarry-cli-session"]')?.getAttribute("content") === "true";
let cliExpiryTimer: ReturnType<typeof setTimeout> | undefined;
const cliSessionPath = /^\/quarry\/session\/[0-9a-f]{32}\/$/;
export type PlatformResource = { id: string; project: string; db: string; env: string | null; engine: string; production: boolean; placement: "remote" };
type Bootstrap = { principal: { id: string }; resources: Omit<PlatformResource, "placement">[] };
type PlatformState = {
  account: string | null;
  remote: "connected" | "disconnected";
  error: string | null;
  storageError: string | null;
  resources: PlatformResource[];
};
let state: PlatformState = { account: null, remote: "disconnected", error: null, storageError: null, resources: [] };
const listeners = new Set<() => void>();
export const platformSnapshot = () => state;
export const subscribePlatform = (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener); }; };
function publish(patch: Partial<PlatformState>) { state = { ...state, ...patch }; listeners.forEach((listener) => listener()); }

function currentSession(): HostedSession {
  const session = readHostedSession();
  if (!session || session.user.id !== state.account) throw new Error(t("platform_session_changed"));
  return session;
}
let refreshFlight: Promise<HostedSession> | undefined;
async function refreshSession(before: HostedSession): Promise<HostedSession> {
  refreshFlight ??= (async () => {
    const rotate = async () => {
      const latest = currentSession();
      if (latest.access_token !== before.access_token || latest.refresh_token !== before.refresh_token) return latest;
      const response = await fetch(hostedConfig().auth.refreshPath, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ refresh_token: before.refresh_token }), signal: AbortSignal.timeout(10000) });
      if (!response.ok) throw new Error(t("platform_session_expired"));
      const next = await response.json() as HostedSession;
      const now = currentSession();
      if (!next.access_token || !next.refresh_token || next.user?.id !== before.user.id) throw new Error(t("platform_identity_changed"));
      if (now.access_token !== before.access_token || now.refresh_token !== before.refresh_token) return now;
      saveHostedSession(next);
      return next;
    };
    const key = hostedConfig().auth.sessionKey;
    // Existing Console tabs use this lock for their rotating refresh token.
    const lock = key === "ylab.console.session.v1" ? "ylab.console.refresh.v1" : `quarry-refresh:${key}`;
    return navigator.locks?.request ? navigator.locks.request(lock, rotate) : rotate();
  })().finally(() => { refreshFlight = undefined; });
  return refreshFlight;
}
export async function platformFetch(path: string, init: RequestInit = {}): Promise<Response> {
  if (!path.startsWith("/api/") || path.startsWith("//")) throw new Error("Invalid platform API path");
  if (CLI_SESSION_MODE) {
    if (!cliSessionPath.test(location.pathname)) throw new Error(t("hosted_reopen_bcli"));
    const headers = new Headers(init.headers);
    if (init.body) headers.set("Content-Type", "application/json");
    const response = await fetch(location.pathname + path.slice(1), {
      ...init, headers, cache: "no-store", credentials: "same-origin",
      signal: init.signal ?? AbortSignal.timeout(path === "/api/platform" ? 2500 : 30000),
    });
    if (response.status === 401) {
      publish({ remote: "disconnected", error: t("hosted_reopen_bcli") });
      throw new Error(t("hosted_reopen_bcli"));
    }
    if (path === "/api/platform" && response.ok) {
      const remaining = Number(response.headers.get("x-quarry-session-remaining-ms"));
      if (remaining > 0 && remaining <= 30 * 60 * 1000) {
        clearTimeout(cliExpiryTimer);
        cliExpiryTimer = setTimeout(() => publish({ remote: "disconnected", error: t("hosted_reopen_bcli") }), remaining);
      }
    }
    return response;
  }
  const url = hostedConfig().apiBase + "/" + path.slice(5);
  const before = currentSession();
  const send = (session: HostedSession) => {
    const headers = new Headers(init.headers);
    headers.set("Authorization", `Bearer ${session.access_token}`);
    if (init.body) headers.set("Content-Type", "application/json");
    return fetch(url, { ...init, headers, cache: "no-store", credentials: "same-origin", signal: init.signal ?? AbortSignal.timeout(path === "/api/platform" ? 2500 : 30000) });
  };
  try {
    let response = await send(before);
    if (response.status === 401) response = await send(await refreshSession(before));
    currentSession(); // Do not publish another account's in-flight response.
    return response;
  } catch (error) {
    if (!init.signal?.aborted) publish({ remote: "disconnected" });
    throw error;
  }
}
export async function platformJSON<T>(path: string, body?: unknown): Promise<T> {
  const response = await platformFetch(path, body === undefined ? {} : { method: "POST", body: JSON.stringify(body) });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(typeof payload.error === "string" ? payload.error : payload.error?.message || payload.message || `请求失败 (${response.status})`);
  }
  return response.json() as Promise<T>;
}

// Retain the existing account-scoped browser document so hosted upgrades keep drafts.
function scopeKey(suffix: string): string { return `qy_platform:${JSON.stringify(CLI_SESSION_MODE ? [location.origin, state.account, state.resources[0]?.id] : [location.origin, state.account])}:${suffix}`; }
let values: Record<string, string> = {};
const BROWSER_DOCUMENT_LIMIT = 2_000_000;
function saveBrowserRecords(): void {
  if (!state.account || (CLI_SESSION_MODE ? state.resources.length !== 1 : readHostedSession()?.user.id !== state.account)) return;
  const copy = { ...values };
  // Query rows stay in the current page. Persist the editor and SQL history only.
  delete copy.qy_tabres;
  delete copy.qy_result;
  let document = JSON.stringify({ values: copy });
  let storageError: string | null = null;
  if (document.length > BROWSER_DOCUMENT_LIMIT) {
    delete copy.qy_assistant_conversations;
    delete copy.qy_hist;
    document = JSON.stringify({ values: copy });
    storageError = t("platform_recovery_full");
  }
  if (document.length > BROWSER_DOCUMENT_LIMIT) {
    publish({ storageError: t("platform_draft_large") });
    return;
  }
  try {
    localStorage.setItem(scopeKey("recovery"), document);
    if (storageError !== state.storageError) publish({ storageError });
  } catch { publish({ storageError: t("platform_browser_full") }); }
}
export const personalStorage = NATIVE_MODE ? nativePersonalStorage : {
  getItem(key: string): string | null { return PLATFORM_MODE ? values[key] ?? null : localStorage.getItem(key); },
  setItem(key: string, value: string): void { if (!PLATFORM_MODE) localStorage.setItem(key, value); else { values[key] = value; saveBrowserRecords(); } },
  removeItem(key: string): void { if (!PLATFORM_MODE) localStorage.removeItem(key); else { delete values[key]; saveBrowserRecords(); } },
};
function hydrateBrowserRecords(): void {
  try {
    localStorage.removeItem(scopeKey("pairing"));
    const document = JSON.parse(localStorage.getItem(scopeKey("recovery")) || "null");
    if (document?.values && typeof document.values === "object") {
      values = Object.fromEntries(Object.entries(document.values).filter(([key, value]) => !["qy_pending_query_history", "qy_tabres", "qy_result"].includes(key) && typeof value === "string")) as Record<string, string>;
    }
  } catch { /* Damaged or inaccessible browser storage cannot load another account. */ }
}

async function reconnectRemote(): Promise<void> {
  try {
    const data = await platformJSON<Bootstrap>("/api/platform");
    if (data.principal.id !== state.account) throw new Error(t("platform_remote_identity"));
    publish({ remote: "connected", error: null, resources: data.resources.map((resource) => ({ ...resource, placement: "remote" })) });
    window.dispatchEvent(new Event("quarry-platform-changed"));
  } catch (error) {
    publish({ remote: "disconnected", error: String((error as Error).message) });
  }
}
export function resourceFor(db: string, env: string | null): PlatformResource {
  const resource = state.resources.find((item) => platformDb(item) === db && platformEnv(item) === env);
  if (!resource) throw new Error(t("platform_target_unavailable"));
  return resource;
}
export const platformDb = (resource: PlatformResource) => `${resource.project}/${resource.db}`;
export const platformEnv = (resource: PlatformResource) => `remote:${resource.id}`;
export const resourceLabel = (resource: PlatformResource) => resource.env || t("platform_default");
export function displayEnvironment(db: string, env: string | null): string {
  if (!PLATFORM_MODE) return env || "default";
  try { return resourceLabel(resourceFor(db, env)); } catch { return t("platform_remote_missing"); }
}
export async function initializePlatform(): Promise<void> {
  if (!PLATFORM_MODE) return;
  const launch = new URLSearchParams(location.hash.slice(1));
  const handoff = launch.get("quarry_handoff");
  const legacyLocalLaunch = launch.has("quarry_local") || launch.has("quarry_code");
  if (handoff !== null || legacyLocalLaunch) {
    launch.delete("quarry_local"); launch.delete("quarry_code"); launch.delete("quarry_handoff");
    history.replaceState(null, "", location.pathname + location.search + (launch.size ? `#${launch}` : ""));
  }
  if (CLI_SESSION_MODE) {
    try {
      if (handoff && location.pathname === "/quarry/") {
        const response = await fetch("/quarry/browser-ticket/consume", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ code: handoff }), cache: "no-store",
          credentials: "same-origin", signal: AbortSignal.timeout(10000),
        });
        if (!response.ok) throw new Error(t("hosted_reopen_bcli"));
        const { path } = await response.json();
        if (typeof path !== "string" || !cliSessionPath.test(path)) throw new Error(t("hosted_reopen_bcli"));
        location.replace(path);
        return;
      }
      if (!cliSessionPath.test(location.pathname)) return;
      const data = await platformJSON<Bootstrap>("/api/platform");
      if (data.resources.length !== 1) throw new Error(t("hosted_reopen_bcli"));
      publish({ account: data.principal.id, remote: "connected", resources: data.resources.map((resource) => ({ ...resource, placement: "remote" })) });
      hydrateBrowserRecords();
    } catch { publish({ error: t("hosted_reopen_bcli") }); }
    return;
  }
  await loadHostedConfig();
  if (hostedConfigurationError()) return;
  if (handoff) {
    try { await consumeHostedHandoff(handoff); }
    catch { publish({ error: t("hosted_launch_failed") }); }
  }
  const session = readHostedSession();
  publish({ account: session?.user.id ?? null });
  window.addEventListener("storage", (event) => {
    if ((event.key === null || event.key === hostedConfig().auth.sessionKey) && readHostedSession()?.user.id !== state.account) window.location.reload();
  });
  if (!state.account) return;
  hydrateBrowserRecords();
  await reconnectRemote();

  let reconnecting = false;
  const reconnect = async (refresh = false) => {
    if (reconnecting || readHostedSession()?.user.id !== state.account || (!refresh && state.remote === "connected")) return;
    reconnecting = true;
    try { await reconnectRemote(); }
    finally { reconnecting = false; }
  };
  window.addEventListener("online", () => { void reconnect(true); });
  window.setInterval(() => { void reconnect(); }, 5000);
}
export function recoveryDownload(): void {
  const blob = new Blob([JSON.stringify({ account: state.account, values }, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob); const anchor = document.createElement("a"); anchor.href = url; anchor.download = "quarry-personal-recovery.json"; anchor.click(); URL.revokeObjectURL(url);
}
