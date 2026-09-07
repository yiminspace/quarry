import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  fetchConnections,
  fetchHealth,
  fetchInspect,
  fetchKeepAlive,
  fetchQueries,
  fetchTables,
  runQuery,
  runSaved,
  type ConnItem,
  type QueryColumn,
  type QueryResult,
  type SavedQuery,
  type TablesResponse,
} from "./api";
import { cellOpensInspector, cellPreview, cellText } from "./cellValue";
import { copy } from "./clip";
import ConnInfoModal from "./ConnInfoModal";
import { t } from "./i18n";
import { CellModal, ExplainModal, HistoryModal, ParamModal, RowDetailModal } from "./Modals";
import { anyModalOpen } from "./modalStack";
import { decodeFocus, decodeQueryLink, encodeFocusSearch, encodeQueryLink, focusTitle, type QueryLinkPayload } from "./queryLink";
import { previewSql, tableClickPlan } from "./tablePreview";
import Sidebar, { defaultEnvFor, type PanelData } from "./Sidebar";
import SqlEditor from "./SqlEditor";
import { useConnStore } from "./store/connStore";
import {
  isResultSnapshotPersistable,
  parseMainTable,
  useTabsStore,
  type Tab,
  type TabId,
  type TabResultSnapshot,
} from "./store/tabsStore";
import { toast } from "./store/toastStore";
import { MAX_ROWS_OPTIONS, useUiStore } from "./store/uiStore";
import TabBar from "./TabBar";
import { useSqlHistory } from "./useSqlHistory";

type Row = Record<string, unknown>;
type SortState = { i: number; dir: 1 | -1 } | null;
type SelectedCell = { ri: number; ci: number } | null;
type ModalState =
  | { type: "cell"; value: unknown }
  | { type: "row"; row: Row; columns: QueryColumn[] }
  | { type: "explain"; plan: string; db: string; env: string | null }
  | null;

/** Snapshot of the connection a request was fired against — used to route the
 * response back to its issuing tab and drop it if that tab was re-pointed to
 * another connection while the request was in flight. */
type ReqCtx = { tabId: TabId; seq: number; db: string; env: string | null };

const EMPTY_PANEL: PanelData = {
  loading: false,
  error: null,
  engine: "",
  tables: null,
  keys: null,
  capped: false,
};

/** Cell type classes for coloring — the legacy `cellClass` rules verbatim. */
function cellClass(v: unknown): string {
  if (v === null || v === undefined) return "null";
  if (typeof v === "number") return "num";
  if (typeof v === "object") return "json";
  const s = String(v);
  if (/^[0-9a-f]{8}-[0-9a-f]{4}-/i.test(s)) return "uuid";
  if (/^\d{4}-\d{2}-\d{2}[ T]\d{2}:/.test(s)) return "ts";
  if (s === "true" || s === "false") return "bool";
  if (/^-?\d+\.?\d*$/.test(s)) return "num";
  return "";
}

/** Human-readable byte size, auto-scaled — mirrors core.py's format_bytes
 * (issue #104/#106) so the GUI's status bar matches the CLI's stderr summary. */
function formatBytes(n: number): string {
  if (n < 1024) return `${Math.trunc(n)} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

/** Numeric-aware sort — the legacy comparator ('10' > '9', nulls last). */
function sortRowsBy(rows: Row[], col: string, dir: 1 | -1): Row[] {
  const numish = (v: unknown): boolean =>
    typeof v === "number" || (typeof v === "string" && v.trim() !== "" && !isNaN(Number(v)));
  return rows.slice().sort((a, b) => {
    const x = a[col];
    const y = b[col];
    if (x === null || x === undefined) return 1;
    if (y === null || y === undefined) return -1;
    if (numish(x) && numish(y)) return (Number(x) - Number(y)) * dir;
    return String(x).localeCompare(String(y)) * dir;
  });
}

function toCSV(columns: QueryColumn[], rows: Row[]): string {
  const cols = columns.map((c) => c.name);
  const esc = (v: unknown): string => {
    const s = cellText(v);
    if (s === null) return "";
    return /[",\n]/.test(s) ? `"${s.replaceAll('"', '""')}"` : s;
  };
  return [cols.join(","), ...rows.map((r) => cols.map((c) => esc(r[c])).join(","))].join("\n");
}

function download(name: string, text: string, type: string): void {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([text], { type }));
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 0);
}

/** Everything under the header bar: sidebar + resizer + the query section
 * (qhead, tabs, editor, toolbar, grid, status) — the legacy `<main>`.
 * Owns connection selection, the per-tab request guards (latest-wins,
 * origin-tab routing, re-point drops) and every result-view interaction. */
export default function ResultWorkbench() {
  const groups = useConnStore((s) => s.groups);
  const loaded = useConnStore((s) => s.loaded);
  const current = useConnStore((s) => s.current);
  const currentTable = useConnStore((s) => s.currentTable);
  const reloadToken = useConnStore((s) => s.reloadToken);
  const maxRows = useUiStore((s) => s.maxRows);
  const setMaxRows = useUiStore((s) => s.setMaxRows);
  const sidebarWidth = useUiStore((s) => s.sidebarWidth);
  const setSidebarWidth = useUiStore((s) => s.setSidebarWidth);

  const tabs = useTabsStore((s) => s.tabs);
  const activeTabId = useTabsStore((s) => s.activeId);
  const results = useTabsStore((s) => s.results);
  const updateActiveTab = useTabsStore((s) => s.updateActiveTab);
  const updateTab = useTabsStore((s) => s.updateTab);
  const setTabResult = useTabsStore((s) => s.setTabResult);

  const activeTab = useMemo(
    () => tabs.find((tb) => tb.id === activeTabId) ?? tabs[0],
    [tabs, activeTabId],
  );
  const sql = activeTab.sql;
  const setSql = useCallback(
    (v: string): void => updateActiveTab({ sql: v }),
    [updateActiveTab],
  );
  const sqlRef = useRef(sql);
  sqlRef.current = sql;

  const [savedQueries, setSavedQueries] = useState<SavedQuery[]>([]);
  const [panel, setPanel] = useState<PanelData>(EMPTY_PANEL);
  const [panelOpen, setPanelOpen] = useState(true);
  const [filter, setFilter] = useState("");
  const [sortState, setSortState] = useState<SortState>(null);
  const [selectedCell, setSelectedCell] = useState<SelectedCell>(null);
  const [columnWidths, setColumnWidths] = useState<Record<number, number>>({});
  const [modal, setModal] = useState<ModalState>(null);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [paramModal, setParamModal] = useState<SavedQuery | null>(null);
  const [connInfoOpen, setConnInfoOpen] = useState(false);
  const [explainBusy, setExplainBusy] = useState(false);
  const [loadMoreBusy, setLoadMoreBusy] = useState(false);
  const [sessionReady, setSessionReady] = useState(false);

  // Per-tab in-flight bookkeeping: a request snapshots its issuing tab and a
  // sequence number, so a later request on the same tab always wins and a
  // response only ever lands on — or shows an error on — the tab that fired it.
  const reqSeqRef = useRef<Record<TabId, number>>({});
  const [pendingByTab, setPendingByTab] = useState<Record<TabId, boolean>>({});
  const [errorByTab, setErrorByTab] = useState<Record<TabId, string | null>>({});
  const loading = pendingByTab[activeTabId] ?? false;
  const gridError = errorByTab[activeTabId] ?? null;

  const activeSnapshot: TabResultSnapshot | undefined = results[activeTabId];
  const result = activeSnapshot?.result ?? null;
  const queryDb = activeSnapshot?.queryDb ?? null;
  const queryEnv = activeSnapshot?.queryEnv ?? null;
  const querySql = activeSnapshot?.querySql ?? null;

  const { history, pushHist, keepDraft, navigateHistory } = useSqlHistory();

  // key of the connection whose table panel is currently shown; used to drop
  // stale /api/tables responses after the user moved on.
  const panelKeyRef = useRef<string | null>(null);
  const tablesSeqRef = useRef(0);

  const findItem = useCallback(
    (db: string): ConnItem | undefined =>
      groups.flatMap((g) => g.items).find((it) => it.db === db),
    [groups],
  );

  /* ---- table/key list: session-cache instant paint + SWR refresh ---- */

  const applyPanel = useCallback((data: TablesResponse): void => {
    setPanel({
      loading: false,
      error: null,
      engine: data.engine,
      tables: "tables" in data ? data.tables : null,
      keys: "keys" in data ? data.keys : null,
      capped: !!data.capped,
    });
  }, []);

  const loadTables = useCallback(
    (db: string, env: string | null, fresh: boolean): void => {
      const key = `${db}@${env || ""}`;
      const seq = ++tablesSeqRef.current;
      panelKeyRef.current = key;
      const { tcache, putTcache, setHealth } = useConnStore.getState();
      const cached = tcache[key];
      if (!fresh && cached) applyPanel(cached);
      else setPanel({ ...EMPTY_PANEL, loading: true });
      const stillCurrent = (): boolean =>
        panelKeyRef.current === key && tablesSeqRef.current === seq;
      fetchTables(db, env, { fresh })
        .then((data) => {
          putTcache(key, data);
          setHealth(db, true);
          if (stillCurrent()) applyPanel(data);
          if (data._cached) {
            // SWR: served from the backend cache — refresh in the background
            fetchTables(db, env, { fresh: true })
              .then((fd) => {
                putTcache(key, fd);
                if (panelKeyRef.current === key) applyPanel(fd);
              })
              .catch(() => {});
          }
        })
        .catch((e) => {
          const msg = String((e as Error).message ?? e);
          setHealth(db, false, msg);
          if (!cached && stillCurrent()) {
            setPanel({ ...EMPTY_PANEL, error: msg });
          }
        });
    },
    [applyPanel],
  );

  /* ---- connection selection (the legacy selectDb) ---- */

  const selectDb = useCallback(
    (db: string, env: string | null, opts?: { viaPill?: boolean; force?: boolean }): void => {
      const state = useConnStore.getState();
      const cur = state.current;
      // re-click on the active connection (no env change) toggles its panel
      if (cur?.db === db && env === null && !opts?.viaPill && !opts?.force) {
        setPanelOpen((v) => !v);
        return;
      }
      const item = findItem(db);
      if (!item) return;
      if (cur?.db !== db) {
        state.setCurrentTable(null);
        setFilter("");
      }
      const multi = item.envs.length > 1;
      const realEnv = multi
        ? (env ?? defaultEnvFor(item))
        : (env ?? item.envs[0]?.env ?? null);
      state.setCurrent({
        db,
        env: realEnv,
        engine: item.engine,
        isRedis: item.engine === "redis",
      });
      setPanelOpen(true);
      updateActiveTab({ db, env: realEnv });
      loadTables(db, realEnv, false);
      if (opts?.viaPill && sqlRef.current.trim() && item.engine !== "redis") {
        // env switch re-runs the current SQL — but never auto-run on prod
        if ((realEnv || "").toLowerCase() === "prod") toast(t("prod_no_autorun"), false);
        else void run({ db, env: realEnv });
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [findItem, loadTables, updateActiveTab],
  );

  /** No / vanished connection: unbind the tab — never silently rebind. */
  const unbindActiveTab = useCallback((): void => {
    const state = useConnStore.getState();
    updateActiveTab({ db: null, env: null });
    state.setCurrent(null);
    state.setCurrentTable(null);
    setPanel(EMPTY_PANEL);
    panelKeyRef.current = null;
  }, [updateActiveTab]);

  /* ---- initial load + workspace-change reload (the legacy loadSide) ---- */

  useEffect(() => {
    let cancelled = false;
    fetchConnections()
      .then((data) => {
        if (cancelled) return;
        const conn = useConnStore.getState();
        conn.setConnMeta(data.workspace, data.workspaces, data.groups);
        // paint health dots instantly from the backend cache (no probing)
        for (const g of data.groups) {
          for (const item of g.items) {
            const env = item.envs.find((e) => e.env === "dev")?.env ?? item.envs[0]?.env ?? "";
            fetchHealth(item.db, env, { cachedOnly: true })
              .then((d) => {
                if (!cancelled && (d.ok === true || d.ok === false))
                  conn.setHealth(item.db, d.ok, d.error);
              })
              .catch(() => {});
          }
        }
      })
      .catch(() => useConnStore.getState().setConnMeta("", [], []));
    fetchQueries()
      .then((qs) => !cancelled && setSavedQueries(qs))
      .catch(() => !cancelled && setSavedQueries([]));
    return () => {
      cancelled = true;
    };
  }, [reloadToken]);

  useEffect(() => {
    let cancelled = false;
    const refresh = (): void => {
      fetchKeepAlive()
        .then((ka) => !cancelled && useConnStore.getState().setKeepAlive(ka))
        .catch(() => {});
    };
    refresh();
    const timer = window.setInterval(refresh, 2000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, []);

  // Restore / re-validate the active tab's connection whenever the tree
  // (re)loads: select it if it still resolves, unbind it otherwise — the
  // active tab's connection can vanish mid-session (workspace removal).
  const loadSeq = useConnStore((s) => s.loadSeq);
  const restoredRef = useRef<number>(-1);
  const pendingSelectRef = useRef<{ db: string; env: string | null } | null>(null);
  const linkHandledRef = useRef(false);
  const deepLinkRef = useRef<QueryLinkPayload | null>(decodeQueryLink(window.location.search));
  useEffect(() => {
    if (!loaded) return;
    // run once per completed connections load (NOT per reloadToken bump —
    // the token changes before the refetched tree lands, and re-validating
    // against the stale tree would miss a just-removed workspace)
    if (restoredRef.current === loadSeq) return;
    restoredRef.current = loadSeq;
    // a just-created local env takes precedence over restoring the tab's
    // previous connection (the legacy `loadSide(); selectDb(db,'local')`)
    const pending = pendingSelectRef.current;
    pendingSelectRef.current = null;
    if (pending && findItem(pending.db)) {
      selectDb(pending.db, pending.env, { force: true });
      return;
    }
    const tab = useTabsStore.getState().tabs.find((tb) => tb.id === useTabsStore.getState().activeId);
    if (tab?.db && findItem(tab.db)) selectDb(tab.db, tab.env ?? null, { force: true });
    else if (tab?.db) unbindActiveTab();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loaded, loadSeq]);

  /* ---- per-tab request guards ---- */

  const startReq = (tabId: TabId, target: { db: string; env: string | null }): ReqCtx => {
    const seq = (reqSeqRef.current[tabId] ?? 0) + 1;
    reqSeqRef.current[tabId] = seq;
    setPendingByTab((prev) => ({ ...prev, [tabId]: true }));
    setErrorByTab((prev) => ({ ...prev, [tabId]: null }));
    return { tabId, seq, db: target.db, env: target.env };
  };
  const isCurrentReq = (ctx: ReqCtx): boolean => reqSeqRef.current[ctx.tabId] === ctx.seq;
  const endReq = (ctx: ReqCtx): void => {
    if (!isCurrentReq(ctx)) return;
    setPendingByTab((prev) => ({ ...prev, [ctx.tabId]: false }));
  };
  const reqFailed = (ctx: ReqCtx, e: unknown): void => {
    if (!isCurrentReq(ctx)) return;
    const tab = useTabsStore.getState().tabs.find((tb) => tb.id === ctx.tabId);
    if (!tab) return; // tab closed while in flight
    if (tab.db !== ctx.db || (tab.env ?? null) !== (ctx.env ?? null)) return;
    setErrorByTab((prev) => ({ ...prev, [ctx.tabId]: String((e as Error)?.message ?? e) }));
  };
  /** Applies a fresh result to its origin tab — but only if that request is
   * still the latest for the tab, the tab still exists, and the tab's current
   * connection still equals the one the request was fired against (a re-point
   * mid-flight must drop the response, never mislabel it). */
  const applyTabResult = (
    ctx: ReqCtx,
    build: () => TabResultSnapshot,
    opts?: { resetSort?: boolean },
  ): void => {
    if (!isCurrentReq(ctx)) return;
    const state = useTabsStore.getState();
    const tab = state.tabs.find((tb) => tb.id === ctx.tabId);
    if (!tab) return; // tab closed while in flight
    if (tab.db !== ctx.db || (tab.env ?? null) !== (ctx.env ?? null)) return;
    setTabResult(ctx.tabId, build());
    if (opts?.resetSort !== false && state.activeId === ctx.tabId) {
      setSortState(null);
      setSelectedCell(null);
      setColumnWidths({});
    }
  };

  /* ---- actions ---- */

  const run = async (
    overrideTarget?: { db: string; env: string | null },
    overrideSql?: string,
  ): Promise<void> => {
    const state = useConnStore.getState();
    const target = overrideTarget ?? (state.current ? { db: state.current.db, env: state.current.env } : null);
    // an explicit SQL is passed by call sites that just called setSql() — the
    // store update hasn't re-rendered into sqlRef yet
    const currentSql = (overrideSql ?? sqlRef.current).trim();
    if (!target || !currentSql) return;
    pushHist(currentSql, target.db, target.env);
    const ctx = startReq(useTabsStore.getState().activeId, target);
    try {
      const data = await runQuery({ db: target.db, env: target.env, sql: currentSql, maxRows });
      applyTabResult(ctx, () => ({
        result: data,
        queryDb: target.db,
        queryEnv: target.env,
        querySql: currentSql,
      }));
    } catch (e) {
      reqFailed(ctx, e);
    } finally {
      endReq(ctx);
    }
  };

  const normalizeEnvForItem = useCallback(
    (item: ConnItem, env: string | null): { env: string | null; ok: boolean } => {
      const envs = item.envs.map((e) => e.env ?? null);
      if (!item.envs.length) return { env: null, ok: false };
      if (item.envs.length === 1) {
        const only = item.envs[0]?.env ?? null;
        if (env !== null && env !== only) return { env: only, ok: false };
        return { env: only, ok: true };
      }
      if (env === null) return { env: defaultEnvFor(item), ok: true };
      if (!envs.includes(env)) return { env: defaultEnvFor(item), ok: false };
      return { env, ok: true };
    },
    [],
  );

  useEffect(() => {
    if (!loaded || linkHandledRef.current) return;
    linkHandledRef.current = true;
    const payload = deepLinkRef.current;
    if (!payload) {
      const focus = decodeFocus(window.location.search);
      if (focus) {
        const item = findItem(focus.db);
        if (item) {
          selectDb(focus.db, focus.env, { force: true });
          if (focus.table) {
            const tab = useTabsStore.getState().tabs.find((tb) => tb.id === useTabsStore.getState().activeId);
            if (tab && !tab.sql.trim()) {
              useTabsStore.getState().updateActiveTab({ sql: previewSql(focus.table, item.engine) });
              useConnStore.getState().setCurrentTable(focus.table);
            }
          }
        }
      }
      setSessionReady(true);
      return;
    }
    const tabsState = useTabsStore.getState();
    const existing = tabsState.tabs.find(
      (tb) =>
        tb.db === payload.db &&
        (tb.env ?? null) === (payload.env ?? null) &&
        tb.sql === payload.sql,
    );
    if (existing) {
      tabsState.switchTab(existing.id);
    } else {
      tabsState.addTab({ db: payload.db, env: payload.env });
      useTabsStore.getState().updateActiveTab({
        db: payload.db,
        env: payload.env,
        sql: payload.sql,
      });
    }

    try {
      const item = findItem(payload.db);
      if (!item) {
        const state = useConnStore.getState();
        state.setCurrent(null);
        state.setCurrentTable(null);
        panelKeyRef.current = null;
        setPanel(EMPTY_PANEL);
        toast(t("share_link_db_missing"), false);
        return;
      }

      const normalized = normalizeEnvForItem(item, payload.env);
      selectDb(payload.db, normalized.env, { force: true });
      if (!normalized.ok) {
        toast(t("share_link_env_missing"), false);
        return;
      }
      const currentSql = payload.sql.trim();
      if (!currentSql) return;
      pushHist(currentSql, payload.db, normalized.env);
      const ctx = startReq(useTabsStore.getState().activeId, {
        db: payload.db,
        env: normalized.env,
      });
      runQuery({
        db: payload.db,
        env: normalized.env,
        sql: currentSql,
        maxRows,
      })
        .then((data) => {
          applyTabResult(ctx, () => ({
            result: data,
            queryDb: payload.db,
            queryEnv: normalized.env,
            querySql: currentSql,
          }));
        })
        .catch((e) => reqFailed(ctx, e))
        .finally(() => endReq(ctx));
    } finally {
      setSessionReady(true);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loaded]);

  // The current-table highlight only holds while the editor still shows the
  // generated preview; editing it away clears the highlight.
  useEffect(() => {
    if (!currentTable || !current) return;
    if (sql.trim() !== previewSql(currentTable, current.engine)) {
      useConnStore.getState().setCurrentTable(null);
    }
  }, [sql, currentTable, current]);

  useEffect(() => {
    if (!sessionReady) return;
    const db = current?.db ?? null;
    const env = current?.env ?? null;
    const table = currentTable ?? parseMainTable(sql);
    const search = encodeFocusSearch({ db, env, table });
    const url = new URL(window.location.href);
    if (url.search !== search) {
      window.history.replaceState(null, "", `${url.pathname}${search}${url.hash}`);
    }
    document.title = focusTitle({ db, env, table });
  }, [sessionReady, current, currentTable, sql]);

  const handleTableClick = (table: string, altKey: boolean): void => {
    const cur = useConnStore.getState().current;
    if (!cur) return;
    const next = previewSql(table, cur.engine);
    if (altKey) {
      keepDraft(sqlRef.current, next, cur.db, cur.env);
      setSql(next);
      return;
    }
    const tabsState = useTabsStore.getState();
    const plan = tableClickPlan(tabsState.tabs, tabsState.activeId, cur.db, cur.env, table, cur.engine);
    if (plan.action === "reuse") {
      const tab = tabsState.tabs.find((tb) => tb.id === plan.tabId);
      if (tab && tab.id !== tabsState.activeId) {
        tabsState.switchTab(tab.id);
        revalidateTab(tab);
      }
      useConnStore.getState().setCurrentTable(table);
      if (!useTabsStore.getState().results[plan.tabId]?.result) {
        void run({ db: cur.db, env: cur.env }, next);
      }
      return;
    }
    if (plan.action === "new") {
      tabsState.addTab({ db: cur.db, env: cur.env });
    }
    useTabsStore.getState().updateActiveTab({ db: cur.db, env: cur.env, sql: next });
    useConnStore.getState().setCurrentTable(table);
    void run({ db: cur.db, env: cur.env }, next);
  };

  const handleInspectKey = async (key: string): Promise<void> => {
    const cur = useConnStore.getState().current;
    if (!cur) return;
    const placeholder = `# ${key}`;
    keepDraft(sqlRef.current, placeholder, cur.db, cur.env);
    setSql(placeholder);
    const ctx = startReq(useTabsStore.getState().activeId, { db: cur.db, env: cur.env });
    try {
      const data = await fetchInspect(cur.db, cur.env, key);
      applyTabResult(ctx, () => ({
        result: data,
        queryDb: cur.db,
        queryEnv: cur.env,
        querySql: null,
      }));
    } catch (e) {
      reqFailed(ctx, e);
    } finally {
      endReq(ctx);
    }
  };

  const openSaved = (name: string): void => {
    const q = savedQueries.find((x) => x.name === name);
    if (!q) return;
    const cur = useConnStore.getState().current;
    const nv = q.sql || `-- ${name}`;
    keepDraft(sqlRef.current, nv, cur?.db ?? null, cur?.env ?? null);
    setSql(nv);
    if (!q.params.length) {
      void runSavedQuery(name, {});
      return;
    }
    setParamModal(q);
  };

  // A saved query runs on its OWN connection (`@db` in the query file), which
  // may differ from the tab's. The response reports the producing connection;
  // the tab is re-pointed to it so the result is tagged/persisted/restored
  // under the connection that actually produced it. Unlike run(), a
  // mid-flight re-point of the issuing tab must NOT drop the response — the
  // saved query itself decides the tab's new connection.
  const runSavedQuery = async (name: string, params: Record<string, string>): Promise<void> => {
    const meta = savedQueries.find((x) => x.name === name);
    const cur = useConnStore.getState().current;
    const tabId = useTabsStore.getState().activeId;
    const ctx = startReq(tabId, { db: cur?.db ?? "", env: cur?.env ?? null });
    try {
      const data = await runSaved(name, cur?.env ?? null, params, maxRows);
      if (!isCurrentReq(ctx)) return;
      const state = useTabsStore.getState();
      const tab = state.tabs.find((tb) => tb.id === tabId);
      if (!tab) return; // tab closed while in flight
      const { db: resDb, env: resEnv, ...clean } = data;
      const db = resDb ?? meta?.db ?? tab.db;
      const env = resEnv ?? null;
      updateTab(tabId, { db, env });
      setTabResult(tabId, {
        result: clean as QueryResult,
        queryDb: db,
        queryEnv: env,
        querySql: null,
      });
      if (state.activeId === tabId) {
        setSortState(null);
        setSelectedCell(null);
        setColumnWidths({});
        useConnStore.getState().setCurrentTable(null);
        if (db && (cur?.db !== db || (cur?.env ?? null) !== (env ?? null)) && findItem(db)) {
          selectDb(db, env, { force: true });
        }
        setSql(clean.sql);
      }
    } catch (e) {
      reqFailed(ctx, e);
    } finally {
      endReq(ctx);
    }
  };

  /** Light SQL formatter: collapse whitespace, uppercase major keywords,
   * newline before major clauses (the legacy Format button, verbatim). */
  const formatSql = (): void => {
    let s = sqlRef.current;
    s = s.replace(/\s+/g, " ").replace(/\s*,\s*/g, ", ").trim();
    s = s.replace(
      /\b(select|from|where|order by|group by|having|limit|offset|left join|right join|inner join|join|on|and|or|union|values|insert into|update|set|delete from)\b/gi,
      (m) => m.toUpperCase(),
    );
    s = s.replace(
      /\b(FROM|WHERE|ORDER BY|GROUP BY|HAVING|LIMIT|OFFSET|LEFT JOIN|RIGHT JOIN|INNER JOIN|JOIN|UNION)\b/g,
      "\n$1",
    );
    setSql(s);
  };

  /** EXPLAIN the current SQL. Single-column plans (postgres) open in a modal;
   * tabular plans (mysql) render in the grid. The modal is suppressed if its
   * tab was switched away or re-pointed while the plan was in flight. */
  const runExplain = async (): Promise<void> => {
    const cur = useConnStore.getState().current;
    if (!cur || cur.isRedis) {
      toast(cur?.isRedis ? t("no_plan_redis") : t("pick_conn"), false);
      return;
    }
    const trimmed = sqlRef.current.trim();
    if (!trimmed) return;
    setExplainBusy(true);
    const ctx = startReq(useTabsStore.getState().activeId, { db: cur.db, env: cur.env });
    try {
      const explainSql = `EXPLAIN ${trimmed.replace(/^\s*explain\s+/i, "")}`;
      const data = await runQuery({ db: cur.db, env: cur.env, sql: explainSql, maxRows });
      if (data.columns.length > 1) {
        applyTabResult(ctx, () => ({
          result: data,
          queryDb: cur.db,
          queryEnv: cur.env,
          querySql: null,
        }));
        return;
      }
      if (!isCurrentReq(ctx)) return;
      const state = useTabsStore.getState();
      const tab = state.tabs.find((tb) => tb.id === ctx.tabId);
      if (!tab || state.activeId !== ctx.tabId) return; // switched away mid-flight
      if (tab.db !== ctx.db || (tab.env ?? null) !== (ctx.env ?? null)) return; // re-pointed
      const col = data.columns[0]?.name;
      const plan = col ? data.rows.map((r) => String(r[col] ?? "")).join("\n") : t("empty_plan");
      setModal({ type: "explain", plan, db: ctx.db, env: ctx.env });
    } catch (e) {
      toast(String((e as Error).message ?? e), false);
    } finally {
      endReq(ctx);
      setExplainBusy(false);
    }
  };

  /* ---- tab switch / close (arriving at a tab re-validates its result) ---- */

  const revalidateTab = useCallback(
    (tab: Tab | undefined): void => {
      if (!tab) return;
      const snap = useTabsStore.getState().results[tab.id];
      if (snap?.result && (snap.queryDb !== tab.db || (snap.queryEnv ?? null) !== (tab.env ?? null))) {
        setTabResult(tab.id, null);
      }
      if (tab.db && findItem(tab.db)) selectDb(tab.db, tab.env ?? null, { force: true });
      else if (tab.db) unbindActiveTab();
      else {
        const state = useConnStore.getState();
        state.setCurrent(null);
        state.setCurrentTable(null);
        setPanel(EMPTY_PANEL);
        panelKeyRef.current = null;
      }
    },
    [findItem, selectDb, setTabResult, unbindActiveTab],
  );

  const handleTabSwitch = useCallback(
    (tab: Tab): void => {
      useTabsStore.getState().switchTab(tab.id);
      revalidateTab(tab);
    },
    [revalidateTab],
  );

  const handleTabClose = useCallback(
    (tab: Tab): void => {
      // Closing a tab must never silently lose hand-written SQL.
      const dying = tab.sql.trim();
      if (dying) pushHist(dying, tab.db, tab.env);
      const store = useTabsStore.getState();
      const wasActive = store.activeId === tab.id;
      store.closeTab(tab.id);
      if (wasActive) {
        const next = useTabsStore.getState();
        revalidateTab(next.tabs.find((tb) => tb.id === next.activeId));
      }
    },
    [pushHist, revalidateTab],
  );

  // Cmd/Ctrl+Shift+W closes the active tab (real Cmd/Ctrl+W can't be
  // intercepted — it closes the browser tab itself); disabled while renaming
  // and when it is the only tab left.
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent): void => {
      if (!(e.metaKey || e.ctrlKey) || !e.shiftKey) return;
      if (e.key !== "w" && e.key !== "W") return;
      if (document.querySelector(".vg-tab.renaming")) return;
      const state = useTabsStore.getState();
      if (state.tabs.length <= 1) return;
      e.preventDefault();
      const dying = state.tabs.find((tb) => tb.id === state.activeId);
      if (dying) handleTabClose(dying);
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [handleTabClose]);

  // Grid view state never carries over between tabs.
  useEffect(() => {
    setSortState(null);
    setSelectedCell(null);
    setColumnWidths({});
  }, [activeTabId]);

  /* ---- grid derivations ---- */

  const shownRows = useMemo(() => {
    const rows = result?.rows ?? [];
    if (!sortState || !result) return rows;
    const col = result.columns[sortState.i];
    return col ? sortRowsBy(rows, col.name, sortState.dir) : rows;
  }, [result, sortState]);

  const canLoadMore = !!(
    result &&
    result.truncated &&
    querySql &&
    current &&
    current.db === queryDb &&
    (current.env ?? null) === (queryEnv ?? null) &&
    (result.engine === "postgres" || result.engine === "mysql")
  );

  const onSort = (i: number): void => {
    setSortState((prev) => {
      if (!prev || prev.i !== i) return { i, dir: 1 };
      if (prev.dir === 1) return { i, dir: -1 };
      return null; // 3rd click restores the original order
    });
  };

  const onLoadMore = async (): Promise<void> => {
    if (!canLoadMore || !result || !querySql || !queryDb) return;
    const tabId = useTabsStore.getState().activeId;
    const ctx = startReq(tabId, { db: queryDb, env: queryEnv });
    const prevRows = result.rows;
    const prevElapsed = result.elapsedMs;
    const prevDownloadBytes = result.downloadBytes ?? 0;
    setLoadMoreBusy(true);
    try {
      const data = await runQuery({
        db: queryDb,
        env: queryEnv,
        sql: querySql,
        maxRows,
        offset: prevRows.length,
      });
      applyTabResult(
        ctx,
        () => {
          const merged = [...prevRows, ...data.rows];
          return {
            result: {
              ...data,
              rows: merged,
              rowCount: merged.length,
              elapsedMs: prevElapsed + data.elapsedMs,
              downloadBytes: prevDownloadBytes + (data.downloadBytes ?? 0),
            },
            queryDb,
            queryEnv,
            querySql,
          };
        },
        // an active sort re-applies itself to the combined rows via shownRows
        { resetSort: false },
      );
    } catch (e) {
      reqFailed(ctx, e);
    } finally {
      endReq(ctx);
      setLoadMoreBusy(false);
    }
  };

  const openCell = (v: unknown): void => {
    setModal({ type: "cell", value: v });
  };

  const onCellDblClick = (v: unknown): void => {
    if (cellOpensInspector(v)) openCell(v);
    else copy(cellText(v) ?? "");
  };

  // Grid keyboard navigation: arrows move the selected cell, Enter inspects,
  // Cmd/Ctrl+C copies — only while not typing and no modal is open.
  const gridNavRef = useRef({ shownRows, selectedCell, result });
  gridNavRef.current = { shownRows, selectedCell, result };
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent): void => {
      const { shownRows: rows, selectedCell: sel, result: res } = gridNavRef.current;
      if (!sel || !res) return;
      const typing = /INPUT|TEXTAREA/.test((document.activeElement as HTMLElement)?.tagName || "");
      if ((e.metaKey || e.ctrlKey) && e.key === "c" && !typing && !String(getSelection())) {
        const v = cellText(rows[sel.ri]?.[res.columns[sel.ci]?.name]) ?? "";
        copy(v);
        return;
      }
      if (typing || e.metaKey || e.ctrlKey || e.altKey || anyModalOpen()) return;
      const move = (dr: number, dc: number): void => {
        e.preventDefault();
        const ri = Math.min(Math.max(sel.ri + dr, 0), rows.length - 1);
        const ci = Math.min(Math.max(sel.ci + dc, 0), res.columns.length - 1);
        setSelectedCell({ ri, ci });
        document
          .querySelector(`#grid tbody tr:nth-child(${ri + 1}) td:nth-child(${ci + 2})`)
          ?.scrollIntoView({ block: "nearest", inline: "nearest" });
      };
      if (e.key === "ArrowDown") move(1, 0);
      else if (e.key === "ArrowUp") move(-1, 0);
      else if (e.key === "ArrowLeft") move(0, -1);
      else if (e.key === "ArrowRight") move(0, 1);
      else if (e.key === "Enter") {
        e.preventDefault();
        const v = cellText(rows[sel.ri]?.[res.columns[sel.ci]?.name]) ?? "";
        openCell(v);
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, []);

  const startColResize = (e: React.MouseEvent, i: number): void => {
    e.preventDefault();
    e.stopPropagation();
    const startX = e.pageX;
    const th = (e.target as HTMLElement).closest("th");
    const startWidth = th?.offsetWidth ?? 180;
    const onMove = (ev: MouseEvent): void => {
      setColumnWidths((prev) => ({ ...prev, [i]: Math.max(50, startWidth + ev.pageX - startX) }));
    };
    const onUp = (): void => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  };

  const startSidebarResize = (e: React.MouseEvent): void => {
    e.preventDefault();
    const startX = e.pageX;
    const startWidth = sidebarWidth;
    const onMove = (ev: MouseEvent): void => {
      setSidebarWidth(startWidth + ev.pageX - startX);
    };
    const onUp = (): void => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  };

  const exportName = (ext: string): string => `quarry-${queryDb || "export"}.${ext}`;
  const exportCsv = (): void => {
    if (!result) return;
    // BOM prefix keeps Excel reading the file as UTF-8
    download(exportName("csv"), "\ufeff" + toCSV(result.columns, shownRows), "text/csv;charset=utf-8");
  };
  const exportJson = (): void => {
    if (!result) return;
    download(exportName("json"), JSON.stringify(shownRows, null, 2), "application/json");
  };

  const openHistory = (): void => {
    if (!history.length) {
      toast(t("no_hist"), true);
      return;
    }
    setHistoryOpen(true);
  };

  const copyQueryLink = (): void => {
    if (!activeTab.db) {
      toast(t("pick_conn"), false);
      return;
    }
    const link = encodeQueryLink(window.location.href, {
      db: activeTab.db,
      env: activeTab.env ?? null,
      sql: activeTab.sql,
    });
    copy(link);
  };

  const recallHistory = (entrySql: string): void => {
    const cur = useConnStore.getState().current;
    keepDraft(sqlRef.current, entrySql, cur?.db ?? null, cur?.env ?? null);
    setSql(entrySql);
    setHistoryOpen(false);
    (document.querySelector("#sql") as HTMLTextAreaElement | null)?.focus();
  };

  /* ---- render ---- */

  const item = current ? findItem(current.db) : undefined;
  const envs = item?.envs ?? [];
  const multiEnv = envs.length > 1;
  const showStatus = !!result && !gridError;
  const resultWillPersist = activeSnapshot ? isResultSnapshotPersistable(activeSnapshot) : true;

  return (
    <main className="vg-main">
      <Sidebar
        current={current}
        panelOpen={panelOpen}
        panel={panel}
        filter={filter}
        onFilterChange={setFilter}
        onSelect={selectDb}
        onTableClick={handleTableClick}
        onInspectKey={(key) => void handleInspectKey(key)}
        onRefresh={() => {
          const cur = useConnStore.getState().current;
          if (cur) loadTables(cur.db, cur.env, true);
        }}
        savedQueries={savedQueries}
        onOpenSaved={openSaved}
      />
      <div className="vg-resizer resizer" id="resizer" onMouseDown={startSidebarResize} />
      <section className="vg-section">
        <div className="vg-qhead qhead">
          <span className="vg-qtitle qtitle" id="qtitle">
            {current?.db ?? t("no_conn")}
          </span>
          <span className="vg-runon runon" id="runon" style={{ display: multiEnv ? undefined : "none" }}>
            {t("runs_on")}
          </span>
          <span className="vg-esw esw" id="esw">
            {current &&
              multiEnv &&
              envs.map((e) => (
                <span
                  key={e.env ?? ""}
                  className={`vg-ep ep${e.env === current.env ? " on" : ""}${e.env === "prod" ? " prod" : ""}`}
                  data-env={e.env ?? ""}
                  onClick={() => selectDb(current.db, e.env ?? null, { viaPill: true })}
                >
                  {e.env || "default"}
                </span>
              ))}
          </span>
          <button
            className="vg-iconbtn iconbtn"
            id="ciBtn"
            title={t("conn_info")}
            aria-label={t("conn_info")}
            style={{ display: current ? undefined : "none" }}
            onClick={() => setConnInfoOpen(true)}
          >
            <i className="ti ti-info-circle" />
          </button>
          <span className="vg-sp sp" />
        </div>
        <TabBar onSwitch={handleTabSwitch} onClose={handleTabClose} />
        <SqlEditor
          value={sql}
          onChange={setSql}
          onRun={() => void run()}
          db={current?.db ?? null}
          env={current?.env ?? null}
          isRedis={!!current?.isRedis}
          tables={panel.tables ?? []}
          resultColumns={result?.columns.map((c) => c.name) ?? []}
          navigateHistory={navigateHistory}
        />
        <div className="vg-toolbar toolbar">
          <button className="vg-btn btn primary" id="runBtn" onClick={() => void run()}>
            <i className="ti ti-player-play" /> <span id="runLbl">{t("run")}</span>
          </button>
          <button className="vg-btn btn" id="fmtBtn" onClick={formatSql}>
            <i className="ti ti-wand" /> <span id="fmtLbl">{t("fmt")}</span>
          </button>
          <button
            className="vg-btn btn"
            id="expBtn"
            title={t("explain_title")}
            aria-label={t("explain_title")}
            disabled={explainBusy}
            onClick={() => void runExplain()}
          >
            <i className="ti ti-route" /> EXPLAIN
          </button>
          <button className="vg-btn btn" id="csvBtn" onClick={exportCsv}>
            <i className="ti ti-download" /> CSV
          </button>
          <button className="vg-btn btn" id="jsonBtn" onClick={exportJson}>
            <i className="ti ti-braces" /> JSON
          </button>
          <select
            id="maxRows"
            className="vg-btn btn"
            title={t("max_rows")}
            aria-label={t("max_rows")}
            style={{ padding: "5px 7px" }}
            value={String(maxRows)}
            onChange={(e) => setMaxRows(Number(e.target.value))}
          >
            {MAX_ROWS_OPTIONS.map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
          <span className="vg-sp sp" />
          <button className="vg-btn btn" id="histBtn" onClick={openHistory}>
            <i className="ti ti-history" /> <span id="histLbl">{t("hist")}</span>
          </button>
          <button className="vg-btn btn" id="linkBtn" onClick={copyQueryLink}>
            <i className="ti ti-link" /> <span id="linkLbl">{t("copy_query_link")}</span>
          </button>
        </div>
        <div className="vg-gridwrap gridwrap" id="grid">
          {loading ? (
            <div className="vg-empty spin">
              <i className="ti ti-loader" /> {t("running")}
            </div>
          ) : gridError ? (
            <div className="vg-err err">{gridError}</div>
          ) : !result ? (
            <div className="vg-empty empty">{t("empty_grid")}</div>
          ) : shownRows.length === 0 ? (
            <div className="vg-empty empty">0 {t("rows")}</div>
          ) : (
            <table className="vg-table">
              <thead>
                <tr>
                  <th className="rownum">#</th>
                  {result.columns.map((c, i) => (
                    <th
                      key={`${i}-${c.name}`}
                      data-i={i}
                      style={
                        columnWidths[i]
                          ? { width: columnWidths[i], minWidth: columnWidths[i] }
                          : undefined
                      }
                      onClick={(e) => {
                        if ((e.target as HTMLElement).classList.contains("vg-rz")) return;
                        onSort(i);
                      }}
                    >
                      {c.name}
                      {c.type && <span className="vg-ty ty">{c.type}</span>}
                      {sortState?.i === i && (
                        <span className="vg-ar ar">{sortState.dir > 0 ? "↑" : "↓"}</span>
                      )}
                      <span className="vg-rz rz" onMouseDown={(e) => startColResize(e, i)} />
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {shownRows.map((r, ri) => (
                  <tr key={ri}>
                    <td
                      className="rownum"
                      data-ri={ri}
                      title={t("row_detail")}
                      onClick={() => setModal({ type: "row", row: r, columns: result.columns })}
                    >
                      {ri + 1}
                    </td>
                    {result.columns.map((c, ci) => {
                      const v = r[c.name];
                      const preview = cellPreview(v);
                      const isSel = selectedCell?.ri === ri && selectedCell?.ci === ci;
                      return (
                        <td
                          key={`${ri}-${ci}`}
                          className={`${cellClass(v)}${isSel ? " sel" : ""}`}
                          data-v={!preview.truncated ? (preview.text ?? "") : undefined}
                          data-truncated={preview.truncated ? "true" : undefined}
                          title={preview.truncated ? t("cell_preview_tip") : (preview.text ?? "NULL")}
                          onClick={() => setSelectedCell({ ri, ci })}
                          onDoubleClick={() => onCellDblClick(v)}
                        >
                          {preview.text === null ? "NULL" : preview.text}
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
        <div
          className="vg-status status"
          id="status"
          style={{ display: showStatus ? undefined : "none" }}
        >
          {result && (
            <>
              <span>
                <span className="vg-cu cu">{result.rowCount}</span> {t("rows")}
              </span>
              <span>
                <i className="ti ti-clock" /> {result.elapsedMs} ms
              </span>
              {result.downloadBytes != null && (
                <>
                  <span
                    id="dlSize"
                    title={result.sizeIsEstimated ? t("size_estimated_tip") : undefined}
                  >
                    <i className="ti ti-download" /> {result.sizeIsEstimated ? "≈" : ""}
                    {formatBytes(result.downloadBytes)}
                  </span>
                  <span
                    id="avgSpeed"
                    title={result.sizeIsEstimated ? t("size_estimated_tip") : undefined}
                  >
                    <i className="ti ti-gauge" /> {result.sizeIsEstimated ? "≈" : ""}
                    {formatBytes(result.downloadBytes / ((result.elapsedMs / 1000) || 0.001))}/s
                  </span>
                </>
              )}
              {result.truncated && (
                <span className="vg-tr tr">
                  <i className="ti ti-arrow-narrow-down" /> {t("truncated")}
                </span>
              )}
              {!resultWillPersist && (
                <span id="resultNotSaved" title={t("large_result_not_saved_tip")}>
                  <i className="ti ti-database-off" /> {t("large_result_not_saved")}
                </span>
              )}
              {canLoadMore && (
                <button
                  className="vg-lmbtn lmBtn"
                  id="loadMoreBtn"
                  disabled={loadMoreBusy}
                  onClick={() => void onLoadMore()}
                >
                  {loadMoreBusy ? t("loading_more") : t("load_more")}
                </button>
              )}
              <span style={{ flex: 1 }} />
              <span>
                {queryDb}
                {queryEnv ? `@${queryEnv}` : ""} · {result.engine}
              </span>
            </>
          )}
        </div>
      </section>

      {modal?.type === "cell" && <CellModal value={modal.value} onClose={() => setModal(null)} />}
      {modal?.type === "row" && (
        <RowDetailModal row={modal.row} columns={modal.columns} onClose={() => setModal(null)} />
      )}
      {modal?.type === "explain" && (
        <ExplainModal
          plan={modal.plan}
          db={modal.db}
          env={modal.env}
          onClose={() => setModal(null)}
        />
      )}
      {historyOpen && (
        <HistoryModal history={history} onRecall={recallHistory} onClose={() => setHistoryOpen(false)} />
      )}
      {paramModal && (
        <ParamModal
          query={paramModal}
          onClose={() => setParamModal(null)}
          onSubmit={(params) => void runSavedQuery(paramModal.name, params)}
        />
      )}
      {connInfoOpen && current && (
        <ConnInfoModal
          db={current.db}
          env={current.env}
          onClose={() => setConnInfoOpen(false)}
          onAfterLocalUp={(db) => {
            // select the fresh local env once the tree has reloaded
            pendingSelectRef.current = { db, env: "local" };
            useConnStore.getState().requestReload();
          }}
          onAfterSync={(db) => {
            useConnStore.getState().dropTcache(`${db}@local`);
            loadTables(db, "local", true);
          }}
        />
      )}
    </main>
  );
}
