import type { ConnItem, SavedQuery } from "./api";

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
 * (west2_matrix_runtime) or the logical name; env siblings share one bucket. */
export function groupQueriesByDb(queries: SavedQuery[], items: ConnItem[]): QuerySection[] {
  const keyToDb = new Map<string, string>();
  for (const item of items) {
    keyToDb.set(item.db, item.db);
    for (const env of item.envs) keyToDb.set(env.key, item.db);
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
