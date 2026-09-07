/** Quote mixed-case / reserved identifiers (legacy `qid`). */
export function quoteIdent(name: string, engine: string): string {
  if (/^[a-z_][a-z0-9_$]*$/.test(name)) return name;
  if (engine === "mysql") return `\`${name.replaceAll("`", "``")}\``;
  return `"${name.replaceAll('"', '""')}"`;
}

/** Sidebar table-click SQL: first page only — the toolbar max-rows cap
 * (and "load more") bound the result. Do not bake a LIMIT into the text. */
export function previewSql(table: string, engine: string): string {
  const target = engine !== "mysql" && table.includes(".") ? table : quoteIdent(table, engine);
  return `select * from ${target}`;
}

export type TabLike = { id: string; sql: string; db: string | null; env: string | null };

export function findPreviewTab(
  tabs: TabLike[],
  db: string,
  env: string | null,
  table: string,
  engine: string,
): TabLike | undefined {
  const preview = previewSql(table, engine);
  return tabs.find((t) => t.db === db && (t.env ?? null) === env && t.sql.trim() === preview);
}

export type TableClickPlan =
  | { action: "reuse"; tabId: string }
  | { action: "empty"; tabId: string }
  | { action: "same"; tabId: string }
  | { action: "new" };

/** Clicking a table never overwrites a tab that already has SQL: reuse the
 * same-table preview if it exists, otherwise the empty active tab, otherwise
 * open a new one. */
export function tableClickPlan(
  tabs: TabLike[],
  activeId: string,
  db: string,
  env: string | null,
  table: string,
  engine: string,
): TableClickPlan {
  const existing = findPreviewTab(tabs, db, env, table, engine);
  if (existing) return { action: "reuse", tabId: existing.id };
  const active = tabs.find((t) => t.id === activeId);
  if (active && active.sql.trim() === "") return { action: "empty", tabId: active.id };
  return { action: "new" };
}

/** Opening a focus URL (`?db=&env=&table=`) must never rebind a tab that
 * already has SQL. Reuse a matching preview, an empty tab, or the same
 * connection when no table was requested; otherwise open a new tab. */
export function focusRestorePlan(
  tabs: TabLike[],
  activeId: string,
  db: string,
  env: string | null,
  table: string | null,
  engine: string,
): TableClickPlan {
  if (table) return tableClickPlan(tabs, activeId, db, env, table, engine);
  const active = tabs.find((t) => t.id === activeId);
  if (active && active.sql.trim() === "") return { action: "empty", tabId: active.id };
  if (active && active.db === db && (active.env ?? null) === env) return { action: "same", tabId: active.id };
  return { action: "new" };
}
