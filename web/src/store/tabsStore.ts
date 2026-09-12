import { create } from "zustand";
import type { QueryResult, SavedQuery } from "../api";
import { t } from "../i18n";
import { useConnStore } from "./connStore";

export type TabId = string;

/** A single editor tab: its own SQL draft and the connection it targets.
 * `title` is a user-set rename; `null` falls back to the automatic
 * `db@env` / first-SQL-words title (see `tabTitle`). */
export type Tab = {
  id: TabId;
  title: string | null;
  sql: string;
  db: string | null;
  env: string | null;
  workspace?: string | null;
  visited?: number;
  savedQueryId?: string;
};

/** Workspace is part of a tab's identity, including when a removed workspace
 * is later replaced by another with the same logical database name. */
export function workspaceFor(db: string | null): string | null {
  const state = useConnStore.getState();
  return state.groups.find((g) => g.items.some((item) => item.db === db))?.ws ?? null;
}

export function sameTabGroup(a: Tab, b: Tab): boolean {
  return a.db === b.db && a.env === b.env && (a.workspace ?? null) === (b.workspace ?? null);
}

export function mostRecent(tabs: Tab[]): Tab | undefined {
  return tabs.reduce<Tab | undefined>((last, tab) =>
    !last || (tab.visited ?? 0) >= (last.visited ?? 0) ? tab : last, undefined);
}

function visit(tabs: Tab[], id: TabId): Tab[] {
  const next = Math.max(0, ...tabs.map((tab) => tab.visited ?? 0)) + 1;
  return tabs.map((tab) => tab.id === id ? { ...tab, visited: next } : tab);
}

/** Per-tab result snapshot, tagged with the connection that PRODUCED the
 * result (`queryDb`/`queryEnv`) — not necessarily the tab's current one. A
 * tab re-pointed to another connection must never have its grid repainted
 * with a mismatched result. `querySql` is the exact SQL that produced the
 * page (set by Run only) — it is what makes "load more" possible. */
export type TabResultSnapshot = {
  result: QueryResult | null;
  queryDb: string | null;
  queryEnv: string | null;
  querySql: string | null;
};

// Exactly the legacy GUI's storage keys AND value formats (qy_tabs is a flat
// array of {id:'t3',sql,db,env,title?}; qy_ati an index; qy_tabres an
// index-aligned array of {db,env,res}) — an existing user's tabs, results and
// active tab carry over unchanged, and the browser suite's storage-shape
// assertions apply as-is.
const TABS_KEY = "qy_tabs";
const ATI_KEY = "qy_ati";
const TABRES_KEY = "qy_tabres";
const EMPTY_GROUPS_KEY = "qy_empty_tab_groups";

function tabGroupKey(tab: Pick<Tab, "workspace" | "db" | "env">): string {
  return JSON.stringify([tab.workspace ?? null, tab.db, tab.env]);
}

function readEmptyGroups(): string[] {
  try {
    const groups = JSON.parse(localStorage.getItem(EMPTY_GROUPS_KEY) || "[]");
    return Array.isArray(groups) ? groups.filter((key): key is string => typeof key === "string") : [];
  } catch { return []; }
}

function persistEmptyGroups(groups: string[]): void {
  try { localStorage.setItem(EMPTY_GROUPS_KEY, JSON.stringify(groups)); } catch { /* storage unavailable */ }
}
// Even older single-state keys, migrated from when qy_tabs was never written.
const LEGACY_UI_KEY = "qy_ui";
const LEGACY_RESULT_KEY = "qy_result";

/** Keep synchronous localStorage work bounded. Large results remain fully
 * available in memory for the current session, but are not restored on reload.
 * downloadBytes is already computed by every current backend engine, so this
 * decision does not stringify the result merely to measure it. */
export const MAX_PERSISTED_RESULT_BYTES = 512 * 1024;
const MAX_PERSISTED_RESULTS_BYTES = 1536 * 1024;

export function isResultSnapshotPersistable(snapshot: TabResultSnapshot): boolean {
  const bytes = snapshot.result?.downloadBytes;
  return typeof bytes !== "number" || !Number.isFinite(bytes) || bytes <= MAX_PERSISTED_RESULT_BYTES;
}

// Tab ids are 't<n>' with a monotonically increasing counter seeded past
// every persisted id, so ids are never reused across reloads (an in-flight
// request routed by id must not land on an unrelated new tab).
let TID = 0;

function newId(): TabId {
  return `t${++TID}`;
}

type StoredTab = { id?: string; sql?: string; db?: string | null; env?: string | null; title?: string | null; workspace?: string | null; visited?: number; savedQueryId?: string };
type StoredResult = { db?: string | null; env?: string | null; res?: (QueryResult & { _sql?: string }) | null } | null;

function blankTab(seed?: { db?: string | null; env?: string | null }): Tab {
  return { id: newId(), title: null, sql: "", db: seed?.db ?? null, env: seed?.env ?? null, workspace: workspaceFor(seed?.db ?? null) };
}

function readStoredTabs(): StoredTab[] {
  try {
    const parsed = JSON.parse(localStorage.getItem(TABS_KEY) || "null");
    if (Array.isArray(parsed)) return parsed;
  } catch {
    // corrupt value — fall through to the single-state key
  }
  try {
    const ui = JSON.parse(localStorage.getItem(LEGACY_UI_KEY) || "null") as StoredTab | null;
    return [{ sql: ui?.sql ?? "", db: ui?.db ?? null, env: ui?.env ?? null }];
  } catch {
    return [{ sql: "", db: null, env: null }];
  }
}

/** Restore each tab's persisted result, but ONLY when its producing
 * connection still matches that tab's current db/env — both fields, not just
 * db. Falls back to the pre-tabres single-result key for the active tab. */
function readStoredResults(tabs: Tab[], ati: number): Record<TabId, TabResultSnapshot> {
  const out: Record<TabId, TabResultSnapshot> = {};
  const accept = (entry: StoredResult, tab: Tab): void => {
    if (!entry?.res) return;
    if (entry.db !== tab.db || (entry.env ?? null) !== (tab.env ?? null)) return;
    const { _sql, ...result } = entry.res;
    out[tab.id] = {
      result: result as QueryResult,
      queryDb: entry.db ?? null,
      queryEnv: entry.env ?? null,
      querySql: _sql ?? null,
    };
  };
  try {
    const parsed = JSON.parse(localStorage.getItem(TABRES_KEY) || "null");
    if (Array.isArray(parsed)) {
      tabs.forEach((tab, i) => accept(parsed[i] as StoredResult, tab));
      return out;
    }
  } catch {
    // corrupt value — fall through to the single-result key
  }
  try {
    const single = JSON.parse(localStorage.getItem(LEGACY_RESULT_KEY) || "null") as StoredResult;
    const tab = tabs[ati];
    if (tab) accept(single, tab);
  } catch {
    // corrupt value — start with no restored results
  }
  return out;
}

function readInitial(): { tabs: Tab[]; activeId: TabId; results: Record<TabId, TabResultSnapshot> } {
  const stored = readStoredTabs();
  for (const tb of stored) {
    const n = Number(String(tb.id || "").slice(1));
    if (n > TID) TID = n;
  }
  const tabs: Tab[] = stored.map((tb) => ({
    id: tb.id || newId(),
    title: tb.title ?? null,
    sql: tb.sql ?? "",
    db: tb.db ?? null,
    env: tb.env ?? null,
    workspace: tb.workspace,
    visited: Number.isFinite(tb.visited) ? tb.visited : 0,
    savedQueryId: typeof tb.savedQueryId === "string" ? tb.savedQueryId : undefined,
  }));
  const ati = Math.min(Math.max(Number(localStorage.getItem(ATI_KEY) || 0) || 0, -1), tabs.length - 1);
  return { tabs, activeId: tabs[ati]?.id ?? "", results: readStoredResults(tabs, ati) };
}

function persistTabs(tabs: Tab[], activeId: TabId): void {
  try {
    localStorage.setItem(
      TABS_KEY,
      JSON.stringify(tabs.map((tb) => ({ id: tb.id, sql: tb.sql, db: tb.db, env: tb.env, title: tb.title, workspace: tb.workspace, visited: tb.visited, savedQueryId: tb.savedQueryId }))),
    );
    localStorage.setItem(ATI_KEY, String(tabs.findIndex((t) => t.id === activeId)));
  } catch {
    // storage full/unavailable — tabs just won't survive a reload
  }
}

/** Persist bounded tab results index-aligned with qy_tabs and tagged with the
 * producing connection. Active-tab-first packing keeps both serialization
 * work and localStorage usage below a deterministic budget. */
function persistResults(tabs: Tab[], activeId: TabId, results: Record<TabId, TabResultSnapshot>): void {
  const pack = (tab: Tab): StoredResult => {
    const snap = results[tab.id];
    if (!snap?.result) return null;
    const res = snap.querySql ? { ...snap.result, _sql: snap.querySql } : snap.result;
    return { db: snap.queryDb, env: snap.queryEnv, res };
  };
  const packed: StoredResult[] = tabs.map(() => null);
  let remaining = MAX_PERSISTED_RESULTS_BYTES;
  const activeIndex = tabs.findIndex((tab) => tab.id === activeId);
  const order = [activeIndex, ...tabs.map((_, i) => i)].filter(
    (i, pos, all) => i >= 0 && all.indexOf(i) === pos,
  );
  for (const i of order) {
    const snap = results[tabs[i].id];
    if (!snap?.result || !isResultSnapshotPersistable(snap)) continue;
    const bytes = snap.result.downloadBytes;
    if (typeof bytes === "number" && Number.isFinite(bytes)) {
      if (bytes > remaining) continue;
      remaining -= bytes;
    } else {
      // A legacy restored result has no size metadata but necessarily already
      // fit localStorage once. Keep at most one such unknown-size payload.
      if (remaining === 0) continue;
      remaining = 0;
    }
    packed[i] = pack(tabs[i]);
  }
  const serialized = JSON.stringify(packed);
  try {
    localStorage.setItem(TABRES_KEY, serialized);
  } catch {
    try {
      localStorage.removeItem(TABRES_KEY);
    } catch {
      // storage completely unavailable — results just won't survive a reload
    }
  }
}

// A bare identifier, or one quoted the way the table-click preview SQL
// quotes mixed-case/reserved names — `"double"` (Postgres, `""` escapes) or
// `` `backtick` `` (MySQL, ``` `` ``` escapes). See `quoteIdent` in
// tablePreview.ts, the producer of that preview SQL.
const IDENT = String.raw`"(?:[^"]|"")+"|\`(?:[^\`]|\`\`)+\`|[a-zA-Z_]\w*`;
const MAIN_TABLE_RE = new RegExp(String.raw`\b(?:from|update|into)\s+(?:(${IDENT})\.)?(${IDENT})`, "i");

function unquoteIdent(raw: string): string {
  if (raw.startsWith('"')) return raw.slice(1, -1).replaceAll('""', '"');
  if (raw.startsWith("`")) return raw.slice(1, -1).replaceAll("``", "`");
  return raw;
}

/** Extracts the single main table an SQL statement targets — the identifier
 * following `FROM`/`UPDATE`/`INTO` (covers SELECT, DELETE FROM, UPDATE and
 * INSERT INTO), quoted or not, with an optional schema prefix. Returns null
 * when no such keyword is found, or the statement joins multiple tables,
 * since there is then no single table to summarize a title by (callers fall
 * back to raw SQL words in that case). */
export function parseMainTable(sql: string): string | null {
  const cleaned = sql.replace(/--.*$/gm, "").replace(/\/\*[\s\S]*?\*\//g, "");
  if (/\bjoin\b/i.test(cleaned)) return null;
  const m = MAIN_TABLE_RE.exec(cleaned);
  if (!m) return null;
  const table = unquoteIdent(m[2]);
  return m[1] ? `${unquoteIdent(m[1])}.${table}` : table;
}

/** A user rename always wins. Else, a non-empty SQL body is what
 * distinguishes tabs on the same connection: prefer the main table it
 * targets (`parseMainTable`), falling back to its first two words when no
 * single table can be parsed out (multi-table JOIN, non-DML statements…).
 * An empty SQL body falls back to `db@env`, then the localized "new query". */
export function tabTitle(tab: Tab): string {
  if (tab.title) return tab.title;
  const sql = tab.sql.trim();
  if (sql) return parseMainTable(sql) ?? sql.split(/\s+/).slice(0, 2).join(" ");
  if (tab.db) return tab.db + (tab.env ? `@${tab.env}` : "");
  return t("new_query");
}

export type TabsState = {
  tabs: Tab[];
  /** Empty string means this connection has no open editor. */
  activeId: TabId;
  emptyGroups: string[];
  /** Keyed by tab id (stable across reorder/close); persisted index-aligned. */
  results: Record<TabId, TabResultSnapshot>;
  claimWorkspaces: () => void;
  selectGroup: (db: string, env: string | null) => void;
  addTab: (seed?: { db?: string | null; env?: string | null }) => void;
  openSavedTab: (query: SavedQuery, db: string, env: string | null) => boolean;
  switchTab: (id: TabId) => void;
  closeTab: (id: TabId) => void;
  renameTab: (id: TabId, title: string | null) => void;
  reorderTab: (fromId: TabId, toId: TabId) => void;
  updateActiveTab: (patch: Partial<Pick<Tab, "sql" | "db" | "env">>) => void;
  updateTab: (id: TabId, patch: Partial<Pick<Tab, "sql" | "db" | "env">>) => void;
  setTabResult: (id: TabId, snapshot: TabResultSnapshot | null) => void;
};

export const useTabsStore = create<TabsState>((set, get) => {
  const initial = readInitial();
  const saveTabs = (tabs: Tab[], activeId: TabId): void => {
    persistTabs(tabs, activeId);
  };
  const saveTopology = (tabs: Tab[], activeId: TabId, results: Record<TabId, TabResultSnapshot>): void => {
    persistTabs(tabs, activeId);
    persistResults(tabs, activeId, results);
  };
  return {
    tabs: initial.tabs,
    activeId: initial.activeId,
    emptyGroups: readEmptyGroups(),
    results: initial.results,

    claimWorkspaces: () => {
      const s = get();
      const tabs = s.tabs.map((tab) => tab.workspace === undefined
        ? { ...tab, workspace: workspaceFor(tab.db) } : tab);
      saveTabs(tabs, s.activeId);
      set({ tabs });
    },

    selectGroup: (db, env) => {
      const s = get();
      const target = { db, env, workspace: workspaceFor(db) } as Tab;
      const existing = mostRecent(s.tabs.filter((tab) => sameTabGroup(tab, target)));
      if (existing) { get().switchTab(existing.id); return; }
      if (s.emptyGroups.includes(tabGroupKey(target))) {
        saveTabs(s.tabs, "");
        set({ activeId: "" });
        return;
      }
      const active = s.tabs.find((tab) => tab.id === s.activeId);
      // Only the initial unbound, empty editor can acquire a connection.
      if (active && !active.db && !active.sql.trim() && !s.results[active.id]?.result) {
        get().updateActiveTab({ db, env });
      } else get().addTab({ db, env });
    },

    addTab: (seed) => {
      const s = get();
      const active = s.tabs.find((t) => t.id === s.activeId);
      const current = useConnStore.getState().current;
      const tab = blankTab(seed ?? { db: active?.db ?? current?.db ?? null, env: active?.env ?? current?.env ?? null });
      const tabs = visit([...s.tabs, tab], tab.id);
      const emptyGroups = s.emptyGroups.filter((key) => key !== tabGroupKey(tab));
      persistEmptyGroups(emptyGroups);
      saveTabs(tabs, tab.id);
      set({ tabs, activeId: tab.id, emptyGroups });
    },

    openSavedTab: (query, db, env) => {
      const s = get();
      const identity = query.queryId ?? JSON.stringify([query.ws ?? null, query.name, query.db]);
      const existing = s.tabs.find((tab) => tab.savedQueryId === identity && tab.db === db &&
        tab.env === env && tab.workspace === workspaceFor(db));
      if (existing) { get().switchTab(existing.id); return false; }
      const tab = { ...blankTab({ db, env }), sql: query.sql,
        title: (query.desc || query.name).split(/[，,；;。\n]/)[0], savedQueryId: identity };
      const tabs = visit([...s.tabs, tab], tab.id);
      const emptyGroups = s.emptyGroups.filter((key) => key !== tabGroupKey(tab));
      persistEmptyGroups(emptyGroups);
      saveTabs(tabs, tab.id);
      set({ tabs, activeId: tab.id, emptyGroups });
      return true;
    },

    switchTab: (id) => {
      const s = get();
      if (!s.tabs.some((t) => t.id === id) || id === s.activeId) return;
      const tabs = visit(s.tabs, id);
      saveTabs(tabs, id);
      set({ tabs, activeId: id });
    },

    closeTab: (id) => {
      const s = get();
      const idx = s.tabs.findIndex((t) => t.id === id);
      if (idx === -1) return;
      const dying = s.tabs[idx];
      let tabs = s.tabs.filter((t) => t.id !== id);
      let activeId = s.activeId;
      const siblings = tabs.filter((tab) => sameTabGroup(tab, dying));
      const emptyGroups = siblings.length ? s.emptyGroups : [...new Set([...s.emptyGroups, tabGroupKey(dying)])];
      if (activeId === id) {
        activeId = mostRecent(siblings)?.id ?? "";
        if (activeId) tabs = visit(tabs, activeId);
      }
      persistEmptyGroups(emptyGroups);
      const results = { ...s.results };
      delete results[id];
      saveTopology(tabs, activeId, results);
      set({ tabs, activeId, results, emptyGroups });
    },

    renameTab: (id, title) => {
      const s = get();
      const tabs = s.tabs.map((t) => (t.id === id ? { ...t, title } : t));
      saveTabs(tabs, s.activeId);
      set({ tabs });
    },

    reorderTab: (fromId, toId) => {
      const s = get();
      if (fromId === toId) return;
      const from = s.tabs.findIndex((t) => t.id === fromId);
      const to = s.tabs.findIndex((t) => t.id === toId);
      if (from === -1 || to === -1 || !sameTabGroup(s.tabs[from], s.tabs[to])) return;
      const tabs = [...s.tabs];
      const [moved] = tabs.splice(from, 1);
      tabs.splice(to, 0, moved);
      saveTopology(tabs, s.activeId, s.results);
      set({ tabs });
    },

    updateActiveTab: (patch) => {
      get().updateTab(get().activeId, patch);
    },

    updateTab: (id, patch) => {
      const s = get();
      // Saved query text is immutable. Every editor/history/format write passes here.
      if (s.tabs.find((tab) => tab.id === id)?.savedQueryId && "sql" in patch) {
        patch = { ...patch };
        delete patch.sql;
      }
      let tabs = s.tabs.map((t) => (t.id === id ? { ...t, ...patch, ...("db" in patch ? { workspace: workspaceFor(patch.db ?? null) } : {}) } : t));
      if (id === s.activeId && ("db" in patch || "env" in patch)) tabs = visit(tabs, id);
      // SQL input is the hot path: never reserialize unchanged result payloads
      // on every keystroke. A low-frequency connection/env re-point still
      // rewrites the index-aligned qy_tabres shape so its producer tags and
      // explicit null entries keep the established reload contract.
      if ("db" in patch || "env" in patch) saveTopology(tabs, s.activeId, s.results);
      else saveTabs(tabs, s.activeId);
      set({ tabs });
    },

    setTabResult: (id, snapshot) => {
      const s = get();
      if (!s.tabs.some((t) => t.id === id)) return; // tab closed while in flight
      const results = { ...s.results };
      if (snapshot) results[id] = snapshot;
      else delete results[id];
      persistResults(s.tabs, s.activeId, results);
      set({ results });
    },
  };
});
