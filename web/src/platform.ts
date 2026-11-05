import { t } from "./i18n";
import { consumeHostedHandoff, hostedConfig, hostedConfigurationError, loadHostedConfig, readHostedSession, saveHostedSession, type HostedSession } from "./hostedAuth";
/** Hosted adapter. Standalone Quarry never reads platform identity or pairing. */
export const PLATFORM_MODE = typeof document !== "undefined" && document.querySelector('meta[name="quarry-mode"]')?.getAttribute("content") === "platform";
type Session = HostedSession;
export type PlatformResource = { id: string; project: string; db: string; env: string | null; engine: string; production: boolean; sourceResourceId?: string; placement?: "remote" | "local" };
type Bootstrap = { principal: { id: string }; resources: PlatformResource[]; capabilities?: Record<string, boolean> };
type WorkbenchDocument = { values: Record<string, string> };
type Pairing = { url: string; token: string; expiresAt?: string | number };
export type PlatformState = {
  account: string | null; remote: "connected" | "disconnected"; local: "connected" | "disconnected";
  persistence: "local" | "recovery" | "saving" | "conflict"; error: string | null; resources: PlatformResource[];
};
let state: PlatformState = { account: null, remote: "disconnected", local: "disconnected", persistence: "recovery", error: null, resources: [] };
const listeners = new Set<() => void>();
export const platformSnapshot = () => state;
export const subscribePlatform = (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener); }; };
function publish(patch: Partial<PlatformState>) { state = { ...state, ...patch }; listeners.forEach((listener) => listener()); }
function changed() { window.dispatchEvent(new Event("quarry-platform-changed")); }
const readSession = readHostedSession;
function currentSession(): Session {
  const session = readSession();
  if (!session || session.user.id !== state.account) throw new Error(t("platform_session_changed"));
  return session;
}
let refreshFlight: Promise<Session> | undefined;
async function refreshSession(before: Session): Promise<Session> {
  refreshFlight ??= (async () => {
    const rotate = async () => {
      const latest = currentSession();
      if (latest.access_token !== before.access_token || latest.refresh_token !== before.refresh_token) return latest;
      const response = await fetch(hostedConfig().auth.refreshPath, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ refresh_token: before.refresh_token }), signal: AbortSignal.timeout(10000) });
      if (!response.ok) throw new Error(t("platform_session_expired"));
      const next = await response.json() as Session;
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
function sameOriginPath(path: string): string {
  if (!path.startsWith("/api/") || path.startsWith("//")) throw new Error("Invalid platform API path");
  return hostedConfig().apiBase + "/" + path.slice(5);
}
async function authenticatedFetch(url: string, init: RequestInit = {}, local = false, pairingRequest = false): Promise<Response> {
  const before = currentSession();
  const send = (session: Session) => {
    const headers = new Headers(init.headers);
    headers.set("Authorization", `Bearer ${session.access_token}`);
    if (init.body) headers.set("Content-Type", "application/json");
    if (local && !pairingRequest) {
      if (!pairing?.token) throw new Error(t("platform_pair_first"));
      headers.set("X-Quarry-Companion-Token", pairing.token);
      headers.set("X-Quarry-User-Id", session.user.id);
    }
    return fetch(url, { ...init, headers, cache: "no-store", credentials: local ? "omit" : "same-origin", signal: init.signal ?? AbortSignal.timeout(url.endsWith("/api/platform") || url.includes("/api/personal") ? 2500 : 30000) });
  };
  let response = await send(before);
  const requiresLiveIAM = !local || pairingRequest || url.endsWith("/api/schema/import") || url.endsWith("/api/assistant/chat");
  if (response.status === 401 && requiresLiveIAM) response = await send(await refreshSession(before));
  currentSession(); // Do not publish another account's in-flight response.
  return response;
}
export async function platformJSON<T>(path: string, body?: unknown, local = false): Promise<T> {
  const response = await platformFetch(path, body === undefined ? {} : { method: "POST", body: JSON.stringify(body) }, local);
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(typeof payload.error === "string" ? payload.error : payload.error?.message || payload.message || `请求失败 (${response.status})`);
  }
  return response.json() as Promise<T>;
}
export async function platformFetch(path: string, init: RequestInit = {}, local = false): Promise<Response> {
  const cloudRequiredLocal = local && ["/api/assistant/chat", "/api/schema/import"].includes(path);
  try {
    const response = await authenticatedFetch(local ? companionURL() + path : sameOriginPath(path), init, local);
    if (local && !response.ok && [401,403].includes(response.status)) {
      const payload = await response.clone().json().catch(() => ({}));
      const code = payload.error?.code || payload.code;
      const message = payload.error?.message || payload.message || (typeof payload.error === "string" ? payload.error : "");
      const invalidPairing = ["pairing_required", "pairing_expired", "owner_mismatch"].includes(code) || /Companion owner|paired platform origin|loopback host|paired to another IAM user/.test(message);
      if (!cloudRequiredLocal || invalidPairing) publish({ local: "disconnected", persistence: "recovery" });
    }
    return response;
  } catch (error) {
    if (!cloudRequiredLocal) publish(local ? { local: "disconnected", persistence: "recovery" } : { remote: "disconnected" });
    throw error;
  }
}
function scopeKey(suffix: string): string { return `qy_platform:${JSON.stringify([location.origin, state.account])}:${suffix}`; }
let pairing: Pairing | null = null;
let values: Record<string, string> = {};
let dirty = false;
let revision = 0;
let timer: ReturnType<typeof setTimeout> | undefined;
let saving: Promise<void> | undefined;
function companionURL() { if (!pairing) throw new Error(t("platform_pair_first")); return pairing.url; }
export function validateCompanionURL(value: string): string {
  const url = new URL(value);
  if (url.protocol !== "http:" || !["127.0.0.1", "[::1]", "localhost"].includes(url.hostname) || url.username || url.password || url.search || url.hash || url.pathname !== "/") throw new Error(t("platform_bad_local_url"));
  return url.origin;
}
function saveRecovery() {
  if (!state.account) return;
  // Full records stay in memory/SQLite. A browser recovery copy has its own budget.
  const recovery = { ...values };
  let document = JSON.stringify({ values: recovery, dirty, revision });
  if (document.length > 2_000_000) {
    delete recovery.qy_tabres;
    try { recovery.qy_pending_query_history = JSON.stringify((JSON.parse(recovery.qy_pending_query_history || "[]") as PersonalQueryEntry[]).map((entry) => ({ ...entry, result: undefined }))); } catch { /* invalid pending history */ }
    document = JSON.stringify({ values: recovery, dirty, revision });
  }
  if (document.length > 2_000_000) {
    delete recovery.qy_assistant_conversations;
    delete recovery.qy_hist;
    document = JSON.stringify({ values: recovery, dirty, revision });
    publish({ error: t("platform_recovery_full") });
  }
  if (document.length > 2_000_000) {
    publish({ error: t("platform_draft_large"), persistence: "recovery" });
    return;
  }
  try { localStorage.setItem(scopeKey("recovery"), document); }
  catch { publish({ error: t("platform_browser_full"), persistence: "recovery" }); }
}
function scheduleSave() {
  if (!PLATFORM_MODE || !state.account) return;
  dirty = true;
  saveRecovery();
  if (state.local !== "connected" || state.persistence === "conflict") { if (state.persistence !== "conflict") publish({ persistence: "recovery" }); return; }
  publish({ persistence: "saving" });
  clearTimeout(timer);
  timer = setTimeout(() => { void flushPersonal(); }, 350);
}
export async function flushPersonal(): Promise<void> {
  if (saving) { await saving; if (dirty && state.local === "connected" && state.persistence !== "conflict") return flushPersonal(); return; }
  if (!dirty || state.local !== "connected" || state.persistence === "conflict") return;
  saving = (async () => {
    try {
      await flushQueryHistory();
      const copy = JSON.stringify(values);
      const response = await platformFetch("/api/personal", { method: "POST", body: JSON.stringify({ kind: "workbench", key: "workspace", value: { values: JSON.parse(copy) }, revision }) }, true);
      if (response.status === 409) { publish({ persistence: "conflict", error: t("platform_save_conflict") }); return; }
      if (!response.ok) throw new Error(`本机保存失败 (${response.status})`);
      const result = await response.json() as { revision: number };
      revision = result.revision;
      dirty = JSON.stringify(values) !== copy;
      saveRecovery();
      publish({ persistence: dirty ? "saving" : "local" });
    } catch (error) { publish({ local: "disconnected", persistence: "recovery", error: String((error as Error).message) }); }
  })().finally(() => { saving = undefined; });
  await saving;
  if (dirty && state.local === "connected" && platformSnapshot().persistence !== "conflict") scheduleSave();
}
export const personalStorage = {
  getItem(key: string): string | null { return PLATFORM_MODE ? values[key] ?? null : localStorage.getItem(key); },
  setItem(key: string, value: string): void { if (!PLATFORM_MODE) localStorage.setItem(key, value); else { values[key] = value; scheduleSave(); } },
  removeItem(key: string): void { if (!PLATFORM_MODE) localStorage.removeItem(key); else { delete values[key]; scheduleSave(); } },
};
async function hydratePersonal(): Promise<void> {
  const result = await platformJSON<{ value: WorkbenchDocument | null; revision: number }>("/api/personal?kind=workbench&key=workspace", undefined, true);
  if (dirty && result.revision !== revision) {
    publish({ persistence: "conflict", error: t("platform_both_changed") });
  } else if (!dirty && result.value?.values) { values = result.value.values; revision = result.revision; saveRecovery(); publish({ persistence: "local" }); }
  else { revision = result.revision; publish({ persistence: dirty ? "saving" : "local" }); await flushPersonal(); }
}
export async function reconnectRemote(): Promise<void> {
  try {
    const data = await platformJSON<Bootstrap>("/api/platform");
    if (data.principal.id !== state.account) throw new Error(t("platform_remote_identity"));
    publish({ remote: "connected", error: null, resources: [...data.resources.map((resource) => ({ ...resource, placement: "remote" as const })), ...state.resources.filter((resource) => resource.placement === "local")] });
    changed();
  } catch (error) { publish({ remote: "disconnected", error: String((error as Error).message) }); throw error; }
}
export async function reconnectLocal(hydrate = false): Promise<void> {
  try {
    const data = await platformJSON<Bootstrap>("/api/platform", undefined, true);
    if (data.principal.id !== state.account) throw new Error(t("platform_local_identity"));
    publish({ local: "connected", error: null, resources: [...state.resources.filter((resource) => resource.placement !== "local"), ...data.resources.map((resource) => ({ ...resource, placement: "local" as const }))] });
    if (hydrate) await hydratePersonal(); else await flushPersonal();
    changed();
  } catch (error) { publish({ local: "disconnected", persistence: "recovery", error: String((error as Error).message) }); throw error; }
}
export async function pairCompanion(url: string, code: string): Promise<void> {
  const origin = validateCompanionURL(url);
  const response = await authenticatedFetch(origin + "/api/pair", { method: "POST", body: JSON.stringify({ code }) }, true, true);
  if (!response.ok) throw new Error(t("platform_pair_failed"));
  const result = await response.json() as { token: string; expiresAt?: string | number };
  if (!result.token) throw new Error(t("platform_pair_incomplete"));
  pairing = { url: origin, ...result };
  localStorage.setItem(scopeKey("pairing"), JSON.stringify(pairing));
  await reconnectLocal(false);
  // Existing page edits take precedence. New machine records become available on explicit reload.
  await flushPersonal();
}
export async function unpairCompanion(): Promise<void> {
  await flushPersonal();
  await platformJSON("/api/unpair", {}, true);
  pairing = null;
  localStorage.removeItem(scopeKey("pairing"));
  publish({ local: "disconnected", persistence: "recovery" });
  changed();
}
export function resourceFor(db: string, env: string | null): PlatformResource {
  const resource = state.resources.find((item) => platformDb(item) === db && platformEnv(item) === env);
  if (!resource) throw new Error(t("platform_target_unavailable"));
  return resource;
}
export const platformDb = (resource: PlatformResource) => `${resource.project}/${resource.db}`;
export const platformEnv = (resource: PlatformResource) => `${resource.placement === "local" ? "local" : "remote"}:${resource.id}`;
export const resourceLabel = (resource: PlatformResource) => resource.placement === "local" ? `Local · ${resource.env || t("platform_computer")}` : resource.env || t("platform_default");
export function displayEnvironment(db: string, env: string | null): string {
  if (!PLATFORM_MODE) return env || "default";
  try { return resourceLabel(resourceFor(db, env)); } catch { return env?.startsWith("local:") ? t("platform_local_missing") : t("platform_remote_missing"); }
}
export async function initializePlatform(): Promise<void> {
  if (!PLATFORM_MODE) return;
  // One-use launch credentials stay in the fragment, never in HTTP requests or history.
  const launch = new URLSearchParams(location.hash.slice(1));
  const localURL = launch.get("quarry_local");
  const localCode = launch.get("quarry_code");
  const handoff = launch.get("quarry_handoff");
  if (localURL !== null || localCode !== null || handoff !== null) {
    launch.delete("quarry_local"); launch.delete("quarry_code"); launch.delete("quarry_handoff");
    history.replaceState(null, "", location.pathname + location.search + (launch.size ? `#${launch}` : ""));
  }
  await loadHostedConfig();
  if (hostedConfigurationError()) return;
  if (handoff) {
    try { await consumeHostedHandoff(handoff); }
    catch { publish({ error: t("hosted_launch_failed") }); }
  }
  const session = readSession();
  publish({ account: session?.user.id ?? null });
  window.addEventListener("storage", (event) => {
    if (event.key === hostedConfig().auth.sessionKey && readSession()?.user.id !== state.account) window.location.reload();
  });
  if (!state.account) return;
  try {
    const recovery = JSON.parse(localStorage.getItem(scopeKey("recovery")) || "null") as { values?: Record<string,string>; dirty?: boolean; revision?: number } | null;
    values = recovery?.values ?? {}; dirty = recovery?.dirty ?? false; revision = recovery?.revision ?? 0;
    const saved = JSON.parse(localStorage.getItem(scopeKey("pairing")) || "null") as Pairing | null;
    if (saved?.token && saved.url) pairing = { ...saved, url: validateCompanionURL(saved.url) };
  } catch { /* malformed recovery cannot load another account */ }
  await Promise.allSettled([reconnectRemote(), pairing ? reconnectLocal(true) : Promise.resolve()]);
  if (localURL && localCode) {
    try { await pairCompanion(localURL, localCode); await reconnectLocal(true); }
    catch (error) { publish({ error: String((error as Error).message) }); }
  }
  if (state.local === "connected") await loadQueryHistory().catch(() => {});

  let reconnecting = false;
  const reconnect = async (refresh = false) => {
    if (reconnecting || readSession()?.user.id !== state.account) return;
    reconnecting = true;
    try {
      await Promise.allSettled([
        refresh || state.remote === "disconnected" ? reconnectRemote() : Promise.resolve(),
        pairing && (refresh || state.local === "disconnected") ? reconnectLocal(state.local === "disconnected") : Promise.resolve(),
      ]);
    } finally { reconnecting = false; }
  };
  window.addEventListener("online", () => { void reconnect(true); });
  window.setInterval(() => { void reconnect(); }, 5000);

  window.addEventListener("pagehide", () => { saveRecovery(); });
}
export function recoveryDownload(): void {
  const blob = new Blob([JSON.stringify({ account: state.account, values }, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob); const anchor = document.createElement("a"); anchor.href = url; anchor.download = "quarry-personal-recovery.json"; anchor.click(); URL.revokeObjectURL(url);
}

/** Explicit recovery choice; never overwrite newer SQLite records automatically. */
export async function loadPersonalFromCompanion(): Promise<void> {
  if (state.local !== "connected") throw new Error(t("platform_local_first"));
  const result = await platformJSON<{ value: WorkbenchDocument | null; revision: number }>("/api/personal?kind=workbench&key=workspace", undefined, true).catch((error) => {
    publish({ error: String((error as Error).message) });
    throw error;
  });
  values = result.value?.values ?? {};
  revision = result.revision;
  dirty = false;
  saveRecovery();
  window.location.reload();
}


type PersonalQueryEntry = { id: string; resourceId: string; sql: string; result?: unknown; createdAt: number; target: { project: string; db: string; env: string | null; placement?: string } };
const PENDING_HISTORY = "qy_pending_query_history";
export function recordPlatformQuery(resource: PlatformResource, sql: string, result: unknown, requestId: string = crypto.randomUUID()): void {
  let entries: PersonalQueryEntry[] = [];
  try { entries = JSON.parse(values[PENDING_HISTORY] || "[]"); } catch { /* damaged recovery */ }
  const serialized = JSON.stringify(result);
  entries.push({ id: requestId, resourceId: resource.id, sql, result: serialized.length <= 262144 ? result : undefined, createdAt: Date.now(), target: { project: resource.project, db: resource.db, env: resource.env, placement: resource.placement } });
  let total = 0;
  let bounded = entries.slice(-1000).reverse().map((entry) => {
    const bytes = JSON.stringify(entry.result ?? null).length;
    total += bytes;
    return total <= 1_500_000 ? entry : { ...entry, result: undefined };
  });
  let metadataBytes = bounded.reduce((sum, entry) => sum + JSON.stringify({ ...entry, result: undefined }).length, 0);
  let removed = entries.length - bounded.length;
  while (metadataBytes > 1_000_000 && bounded.length > 1) {
    const entry = bounded.pop()!;
    metadataBytes -= JSON.stringify({ ...entry, result: undefined }).length;
    removed++;
  }
  if (removed) publish({ error: t("platform_history_full") });
  bounded = bounded.reverse();
  values[PENDING_HISTORY] = JSON.stringify(bounded);
  scheduleSave();
}
async function loadQueryHistory(): Promise<void> {
  const stored = await platformJSON<{ value: PersonalQueryEntry[] | null; revision: number }>("/api/personal?kind=query-history&key=entries", undefined, true);
  const history: { sql: string; db: string | null; env: string | null; ts: number }[] = [];
  for (const entry of stored.value || []) {
    const resource = state.resources.find((item) => item.id === entry.resourceId && (!entry.target?.placement || item.placement === entry.target.placement));
    history.push({ sql: entry.sql, db: resource ? platformDb(resource) : null, env: resource ? platformEnv(resource) : null, ts: entry.createdAt });
  }
  let existing: typeof history = [];
  try { existing = JSON.parse(values.qy_hist || "[]"); } catch { /* absent */ }
  const seen = new Set<string>();
  values.qy_hist = JSON.stringify([...history, ...existing].sort((a, b) => b.ts - a.ts).filter((entry) => { const key = JSON.stringify([entry.sql, entry.ts]); if (seen.has(key)) return false; seen.add(key); return true; }).slice(0, 1000));
}
async function flushQueryHistory(): Promise<void> {
  const pending = JSON.parse(values[PENDING_HISTORY] || "[]") as PersonalQueryEntry[];
  if (!pending.length) return;
  const stored = await platformJSON<{ value: PersonalQueryEntry[] | null; revision: number }>("/api/personal?kind=query-history&key=entries", undefined, true);
  const entries = [...(stored.value || [])];
  for (const entry of pending) if (!entries.some((value) => value.id === entry.id)) entries.push(entry);
  entries.sort((a, b) => b.createdAt - a.createdAt);
  let total = 0;
  const bounded = entries.slice(0, 1000).map((entry) => { total += JSON.stringify(entry.result ?? null).length; return total <= 1_500_000 ? entry : { ...entry, result: undefined }; });
  await platformJSON("/api/personal", { kind: "query-history", key: "entries", value: bounded, revision: stored.revision }, true);
  const ids = new Set(pending.map((entry) => entry.id));
  values[PENDING_HISTORY] = JSON.stringify((JSON.parse(values[PENDING_HISTORY] || "[]") as PersonalQueryEntry[]).filter((entry) => !ids.has(entry.id)));
}
