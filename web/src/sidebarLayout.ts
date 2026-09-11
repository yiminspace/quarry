import type { ConnGroup, ConnItem, SavedQuery } from "./api";
import { t } from "./i18n";

export function groupKey(ws: string | null, group: string | null): string {
  return `${ws || ""}::${group || t("other")}`;
}

export const ENGINE_ORDER = ["postgres", "mysql", "redis", "neptune"] as const;

/** Same-group connections, SQL family first; registration order inside each engine. */
export function itemsInEngineOrder(items: ConnItem[]): ConnItem[] {
  const buckets = new Map<string, ConnItem[]>();
  for (const item of items) {
    const engine = item.engine || "other";
    const list = buckets.get(engine);
    if (list) list.push(item);
    else buckets.set(engine, [item]);
  }
  const ordered: ConnItem[] = [];
  for (const engine of ENGINE_ORDER) {
    const grouped = buckets.get(engine);
    if (!grouped) continue;
    ordered.push(...grouped);
    buckets.delete(engine);
  }
  for (const grouped of buckets.values()) ordered.push(...grouped);
  return ordered;
}

export type QuerySection = { db: string; queries: SavedQuery[] };

/** Group saved queries by sidebar logical db. `@db` may be a connection key
 * (west2_matrix_runtime) or the logical name; env siblings share one bucket.
 * Exact connection keys win over another item's logical `db`, matching
 * `resolve_connection()` (direct key, no --env → that connection). */
export function groupQueriesByDb(queries: SavedQuery[], items: ConnItem[]): QuerySection[] {
  const keyToDb = new Map<string, string>();
  const exactKeys = new Set<string>();
  for (const item of items) {
    for (const env of item.envs) {
      exactKeys.add(env.key);
      keyToDb.set(env.key, item.db);
    }
  }
  for (const item of items) {
    if (!exactKeys.has(item.db)) keyToDb.set(item.db, item.db);
  }
  const buckets = new Map<string, SavedQuery[]>();
  for (const q of queries) {
    const db = keyToDb.get(q.db) ?? q.db;
    const list = buckets.get(db);
    if (list) list.push(q);
    else buckets.set(db, [q]);
  }
  const ordered: QuerySection[] = [];
  const seen = new Set<string>();
  for (const item of items) {
    const list = buckets.get(item.db);
    if (!list || seen.has(item.db)) continue;
    ordered.push({ db: item.db, queries: list });
    seen.add(item.db);
  }
  for (const [db, list] of buckets) {
    if (!seen.has(db)) ordered.push({ db, queries: list });
  }
  return ordered;
}

/** Keep file ownership independent of @db: a query may target a connection elsewhere. */
export function groupsWithQueries(groups: ConnGroup[], queries: SavedQuery[]) {
  const result = groups.map((g) => ({ ...g, queries: [] as SavedQuery[] }));
  for (const query of queries) {
    const owners = result.filter((g) => g.ws === query.ws);
    let owner = owners.find((g) => g.items.some((item) =>
      item.envs.some((env) => env.key === query.db))) ??
      owners.find((g) => g.items.some((item) => item.db === query.db)) ?? owners[0];
    if (!owner && !query.ws) {
      // Compatibility with an older server that doesn't send workspace metadata.
      owner = result.find((g) => g.items.some((item) =>
        item.db === query.db || item.envs.some((env) => env.key === query.db))) ?? result[0];
    }
    if (!owner) {
      owner = { ws: query.ws ?? null, group: query.ws?.split("/").pop() ?? null,
        items: [], queries: [] };
      result.push(owner);
    }
    owner.queries.push(query);
  }
  return result;
}
