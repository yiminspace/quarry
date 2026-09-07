export type QueryLinkPayload = {
  db: string;
  env: string | null;
  sql: string;
};

/** Address-bar focus (no `sql`) — restore selection, never auto-run. */
export type FocusPayload = {
  db: string | null;
  env: string | null;
  table: string | null;
};

function normalizeEnv(env: string | null): string | null {
  if (env === null) return null;
  return env.trim() === "" ? null : env;
}

export function encodeQueryLink(baseHref: string, payload: QueryLinkPayload): string {
  const url = new URL(baseHref);
  const params = new URLSearchParams(url.search);
  params.set("db", payload.db);
  params.set("sql", payload.sql);
  if (payload.env) params.set("env", payload.env);
  else params.delete("env");
  url.search = params.toString();
  return url.toString();
}

export function decodeQueryLink(search: string): QueryLinkPayload | null {
  const params = new URLSearchParams(search);
  const db = params.get("db");
  const sql = params.get("sql");
  if (!db || sql === null) return null;
  return { db, env: normalizeEnv(params.get("env")), sql };
}

export function encodeFocusSearch(payload: FocusPayload): string {
  const params = new URLSearchParams();
  if (payload.db) params.set("db", payload.db);
  if (payload.env) params.set("env", payload.env);
  if (payload.table) params.set("table", payload.table);
  const q = params.toString();
  return q ? `?${q}` : "";
}

export function decodeFocus(search: string): { db: string; env: string | null; table: string | null } | null {
  const params = new URLSearchParams(search);
  if (params.has("sql")) return null;
  const db = params.get("db");
  if (!db) return null;
  const table = params.get("table");
  return { db, env: normalizeEnv(params.get("env")), table: table && table.trim() ? table : null };
}

export function focusTitle(payload: FocusPayload): string {
  if (!payload.db) return "Quarry";
  const conn = payload.env ? `${payload.db}@${payload.env}` : payload.db;
  return payload.table ? `${conn} · ${payload.table}` : conn;
}
