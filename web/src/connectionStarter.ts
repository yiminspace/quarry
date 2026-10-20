export const NEPTUNE_STARTER_SQL = "MATCH (n) RETURN n LIMIT 25";

type TabLike = {
  id: string;
  sql: string;
  db: string | null;
  env: string | null;
};

export type StarterPlan =
  | { action: "reuse" | "empty"; tabId: string; sql: string }
  | { action: "new"; sql: string }
  | { action: "none" };

export function connectionStarterPlan(
  tabs: TabLike[],
  activeId: string,
  db: string,
  env: string | null,
  engine: string,
): StarterPlan {
  if (engine !== "neptune") return { action: "none" };
  const existing = tabs.find(
    (tab) =>
      tab.db === db &&
      tab.env === env &&
      tab.sql.trim() === NEPTUNE_STARTER_SQL,
  );
  if (existing) return { action: "reuse", tabId: existing.id, sql: NEPTUNE_STARTER_SQL };
  const active = tabs.find((tab) => tab.id === activeId);
  if (active && active.sql.trim() === "") {
    return { action: "empty", tabId: active.id, sql: NEPTUNE_STARTER_SQL };
  }
  return { action: "new", sql: NEPTUNE_STARTER_SQL };
}
