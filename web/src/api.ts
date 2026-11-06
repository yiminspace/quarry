import { PLATFORM_MODE, platformSnapshot, platformDb, platformEnv, platformJSON, resourceFor, resourceLabel, platformFetch } from "./platform";
import { connectionTargetId } from "./store/connStore";
export type CloudStatus = "connected" | "unavailable" | "signed_out" | "forbidden" | "restart_required";
export type ConnEnv = {
  env: string | null;
  key: string;
  engine: string;
  region: string | null;
  ssh: boolean;
  proxied: boolean;
  production?: boolean;
  source?: "cloud";
  display_env?: string | null;
  label?: string;
  resourceId?: string;
  targetId?: string;
  associatedTargetId?: string;
  providerLabel?: string;
  sourceStatus?: CloudStatus;
  workspaceLabel?: string;
  placement?: "remote" | "local";
};

export type VersionInfo = { name: string; version: string };

export type ConnItem = {
  db: string;
  label?: string;
  is_env_set: boolean;
  engine: string;
  envs: ConnEnv[];
};

export type ConnGroup = {
  group: string | null;
  label?: string;
  ws: string | null;
  items: ConnItem[];
};

export type ConnectionsResponse = {
  groups: ConnGroup[];
  workspace: string;
  workspaces: string[];
  cloudStatus?: CloudStatus;
};

export type RedisKeyMeta = {
  key: string;
  type: string;
  ttl: number;
};

export type TablesResponse =
  | { engine: "redis"; keys: RedisKeyMeta[]; capped: boolean; _cached?: boolean }
  | { engine: string; tables: string[]; capped: boolean; _cached?: boolean };

export type ColumnsResponse = {
  columns: string[];
  types: Record<string, string | null>;
};

export type HealthResponse = { ok: boolean | null; error?: string };

export type SavedQueryParam = {
  name: string;
  type: string | null;
  required: boolean;
  default: unknown;
};

export type SavedQuery = {
  ws?: string;
  queryId?: string;
  name: string;
  db: string;
  desc: string | null;
  sql: string;
  params: SavedQueryParam[];
  targetIds?: string[];
  ownerTargetIds?: string[];
};

export type QueryColumn = {
  name: string;
  type: string | null;
};

export type QueryResult = {
  columns: QueryColumn[];
  rows: Record<string, unknown>[];
  rowCount: number;
  truncated: boolean;
  elapsedMs: number;
  engine: string;
  sql: string;
  // issue #106: response-body size + whether it's an exact byte count or an
  // approximation (see core.py's QueryResult docstring for per-engine detail).
  downloadBytes: number;
  sizeIsEstimated: boolean;
};

export type QueryRequest = {
  db: string;
  env: string | null;
  sql: string;
  maxRows: number;
  offset?: number;
  requestId?: string;
  target?: string;
};

export type ConnTunnel = { host: string; user: string | null; port: number; key: string | null };

export type ConnInfo = {
  key: string;
  db: string;
  env: string | null;
  engine: string;
  url: string;
  host: string | null;
  port: number | null;
  database: string | null;
  databasePath?: string | null;
  group: string | null;
  region: string | null;
  notes: string | null;
  file: string;
  tunnel: ConnTunnel | null;
};

export type WorkspaceItem = {
  dir: string;
  display: string;
  exists: boolean;
  hasConnections: boolean;
  proxyEnabled: boolean;
};
export type ProxyDiscovered = { host: string; port: number; source: string };
export type WorkspacesResponse = {
  config: string;
  items: WorkspaceItem[];
  proxyDiscovered: ProxyDiscovered | null;
};

export type KeepAliveTunnelStatus = {
  connection: string;
  env: string | null;
  state: "up" | "reconnecting" | "down" | "blocked";
  localPort?: number | null;
  lastError?: string | null;
  updatedAt?: number;
};

export type KeepAliveResponse = {
  workspace: string;
  enabled: boolean;
  reconnect: boolean;
  keeper: { running: boolean; pid: number | null };
  tunnels: KeepAliveTunnelStatus[];
  updatedAt: number;
};

export type LocalUpResponse = {
  key: string;
  created: boolean;
  engine: string;
  state: string;
  port: number;
  synced_from?: string;
  sync_error?: string;
};

export type LocalSyncResponse = { from: string; prev?: string | null; [key: string]: unknown };

export type UpdateInfo = { current: string; latest: string | null; available: boolean; managed?: boolean };

export type ChangelogVersion = { version: string; date: string; entries: string[] };

async function getJSON<T>(path: string, signal?: AbortSignal): Promise<T> {
  if (PLATFORM_MODE) return platformGet<T>(path);
  const url = new URL(path, location.origin);
  const db = url.searchParams.get("db");
  const target = db && connectionTargetId(db, url.searchParams.get("env") || null);
  if (target?.startsWith("local:")) url.searchParams.set("target", target);
  const res = await fetch(url.pathname + url.search, { signal });
  if (!res.ok) {
    const body = await res.json().catch(() => ({ error: res.statusText }));
    throw new Error(body.error || `${path} -> ${res.status}`);
  }
  return res.json() as Promise<T>;
}

async function postJSON<T>(path: string, body: unknown): Promise<T> {
  if (PLATFORM_MODE) {
    const source = body as Record<string, unknown>;
    if (typeof source.db !== "string") throw new Error("此操作在托管工作台中不可用。");
    const resource = resourceFor(source.db, typeof source.env === "string" ? source.env : null);
    return platformJSON<T>(path, { ...source, db: undefined, env: undefined, resourceId: resource.id, requestId: source.requestId || crypto.randomUUID() });
  }
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const payload = await res.json().catch(() => ({ error: res.statusText }));
    throw new Error(payload.error || `${path} -> ${res.status}`);
  }
  return res.json() as Promise<T>;
}

export function fetchVersion(): Promise<VersionInfo> {
  return getJSON("/api/version");
}

export function fetchConnections(): Promise<ConnectionsResponse> {
  return getJSON("/api/connections");
}

export function fetchTables(
  db: string,
  env: string | null,
  opts?: { fresh?: boolean },
): Promise<TablesResponse> {
  const qs = new URLSearchParams({ db, env: env ?? "" });
  if (opts?.fresh) qs.set("fresh", "1");
  return getJSON(`/api/tables?${qs}`, AbortSignal.timeout(30000));
}

export function fetchColumns(
  db: string,
  env: string | null,
  table: string,
): Promise<ColumnsResponse> {
  const qs = new URLSearchParams({ db, env: env ?? "", table });
  return getJSON(`/api/columns?${qs}`);
}

const cancelledPlatformRequests = new Set<string>();
export async function runQuery(req: QueryRequest): Promise<QueryResult> {
  const request = PLATFORM_MODE ? { ...req, requestId: req.requestId || crypto.randomUUID() }
    : { ...req, target: req.target ?? connectionTargetId(req.db, req.env) };
  const result = await postJSON<QueryResult>("/api/query", request);
  if (PLATFORM_MODE && request.requestId && cancelledPlatformRequests.delete(request.requestId)) throw new Error("查询已取消。");
  return result;
}
export async function cancelPlatformQuery(db: string, env: string | null, requestId: string): Promise<void> {
  if (PLATFORM_MODE) {
    await platformJSON("/api/cancel", { requestId });
    cancelledPlatformRequests.add(requestId);
  } else {
    await postJSON("/api/cancel", { db, env, requestId });
  }
}

export function fetchHealth(
  db: string,
  env: string | null,
  opts?: { fresh?: boolean; cachedOnly?: boolean },
): Promise<HealthResponse> {
  const qs = new URLSearchParams({ db, env: env ?? "" });
  if (opts?.fresh) qs.set("fresh", "1");
  if (opts?.cachedOnly) qs.set("cached", "1");
  return getJSON(`/api/health?${qs}`, AbortSignal.timeout(10000));
}

export function fetchQueries(): Promise<SavedQuery[]> {
  return getJSON("/api/queries");
}

export function fetchInspect(db: string, env: string | null, key: string): Promise<QueryResult> {
  const qs = new URLSearchParams({ db, env: env ?? "", key });
  return getJSON(`/api/inspect?${qs}`);
}

export function runSaved(
  name: string,
  env: string | null,
  params: Record<string, string>,
  maxRows: number,
  queryId?: string,
  requestId?: string,
  target?: string,
  db?: string,
): Promise<QueryResult & { db: string; env: string | null }> {
  return postJSON("/api/run", { name, env, params, maxRows, queryId, requestId, target, db });
}

export function fetchConnInfo(
  db: string,
  env: string | null,
  opts?: { reveal?: boolean },
): Promise<ConnInfo> {
  const qs = new URLSearchParams({ db, env: env ?? "" });
  if (opts?.reveal) qs.set("reveal", "1");
  return getJSON(`/api/conninfo?${qs}`);
}

export function fetchWorkspaces(): Promise<WorkspacesResponse> {
  return getJSON("/api/workspaces");
}

export function fetchKeepAlive(): Promise<KeepAliveResponse> {
  return getJSON("/api/keepalive");
}

export function keepAliveUp(): Promise<KeepAliveResponse> {
  return postJSON("/api/keepalive/up", {});
}

export function keepAliveDown(): Promise<KeepAliveResponse> {
  return postJSON("/api/keepalive/down", {});
}

export function addWorkspace(dir: string): Promise<WorkspacesResponse> {
  return postJSON("/api/workspaces/add", { dir });
}

export function removeWorkspace(dir: string): Promise<WorkspacesResponse> {
  return postJSON("/api/workspaces/remove", { dir });
}

export function localUp(db: string): Promise<LocalUpResponse> {
  return postJSON("/api/local/up", { db });
}

export function localSync(db: string, from?: string): Promise<LocalSyncResponse> {
  return postJSON("/api/local/sync", { db, from });
}

export function fetchUpdate(): Promise<UpdateInfo> {
  return getJSON("/api/update");
}

export function fetchChangelog(): Promise<ChangelogVersion[]> {
  return getJSON("/api/changelog");
}

export function fetchRecordLink(db: string, env: string | null, sql: string, row: Record<string, unknown>): Promise<{ sql?: string; table?: string; reason?: string }> {
  return postJSON("/api/record-link", { db, env, sql, row, target: connectionTargetId(db, env) });
}

/** Adapt the standalone protocol to explicit, authorized platform targets. */
async function platformGet<T>(path: string): Promise<T> {
  const url = new URL(path, location.origin);
  if (url.pathname === "/api/connections") {
    const groups: ConnGroup[] = [];
    for (const resource of platformSnapshot().resources) {
      let group = groups.find((item) => item.group === resource.project);
      if (!group) { group = { group: resource.project, ws: resource.project, items: [] }; groups.push(group); }
      const db = platformDb(resource);
      let item = group.items.find((item) => item.db === db);
      if (!item) { item = { db, engine: resource.engine, is_env_set: true, envs: [] }; group.items.push(item); }
      item.envs.push({ env: platformEnv(resource), key: resource.id, resourceId: resource.id, label: resourceLabel(resource), placement: resource.placement, engine: resource.engine, production: resource.production, region: null, ssh: false, proxied: false });
    }
    return { groups, workspace: location.origin, workspaces: groups.map((group) => group.ws!) } as T;
  }
  if (url.pathname === "/api/queries") return [] as T;
  if (url.pathname === "/api/keepalive") return { workspace: "", enabled: false, reconnect: false, keeper: { running: false, pid: null }, tunnels: [] } as T;
  const db = url.searchParams.get("db");
  if (!db) throw new Error("此操作在托管工作台中不可用。");
  const resource = resourceFor(db, url.searchParams.get("env"));
  url.searchParams.delete("db"); url.searchParams.delete("env"); url.searchParams.set("resourceId", resource.id);
  const response = await platformFetch(url.pathname + url.search, {});
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(typeof payload.error === "string" ? payload.error : payload.error?.message || `请求失败 (${response.status})`);
  }
  return response.json() as Promise<T>;
}
