import { ResultImageCell, ResultImageModal, ResultImageColumnMenu } from "./ResultImage";
import { CLI_SESSION_MODE, PLATFORM_MODE } from "./platform";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  cancelPlatformQuery,
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
import { cellOpensInspector, cellPreview, cellText, compareCellValues } from "./cellValue";
import { copy } from "./clip";
import { NEPTUNE_STARTER_SQL } from "./connectionStarter";
import ConnInfoModal from "./ConnInfoModal";
import { t } from "./i18n";
import { CellModal, ExplainModal, HistoryModal, ParamModal, RowDetailModal } from "./Modals";
import { anyModalOpen } from "./modalStack";
import { NATIVE_MODE, restoreNativeScope } from "./nativeRecords";
import { decodeFocus, decodeQueryLink, encodeFocusSearch, encodeQueryLink, focusTitle, type QueryLinkPayload } from "./queryLink";
import { focusRestorePlan, previewSql, tableClickPlan } from "./tablePreview";
import Sidebar, { type PanelData } from "./Sidebar";
import { groupKey, groupsWithQueries } from "./sidebarLayout";
import SqlEditor from "./SqlEditor";
import EditorWorkspace from "./EditorWorkspace";
import SqlAssistant from "./SqlAssistant";
import { useSqlAssistant } from "./useSqlAssistant";
import { getConversation } from "./assistantApi";
import RunControls from "./RunControls";
import { ambiguousConnection, cloudRouteVisible, connectionEnvLabel, connectionTargetId, defaultEnvFor, displayConnectionEnvironment, federatedRoute, localTargetRoute, needsLocalTargetChoice, useConnStore } from "./store/connStore";
import {
  isResultSnapshotPersistable,
  mostRecent,
  workspaceFor,
  parseMainTable,
  useTabsStore,
  type Tab,
  type TabId,
  type TabResultSnapshot,
} from "./store/tabsStore";
import { toast } from "./store/toastStore";
import { autoRunEnabled, MAX_ROWS_OPTIONS, useUiStore } from "./store/uiStore";
import TabBar from "./TabBar";
import { useSqlHistory } from "./useSqlHistory";

type Row = Record<string, unknown>;
type SortState = { i: number; dir: 1 | -1 } | null;
type SelectedCell = { ri: number; ci: number } | null;
type ModalState =
  | { type: "cell"; value: unknown; env: string | null }
  | { type: "row"; row: Row; columns: QueryColumn[]; db: string | null; env: string | null; sql: string }
  | { type: "explain"; plan: string; db: string; env: string | null }
  | null;

/** Snapshot of the connection a request was fired against — used to route the
 * response back to its issuing tab and drop it if that tab was re-pointed to
 * another connection while the request was in flight. */
type ReqCtx = { tabId: TabId; requestId: string; seq: number; db: string; env: string | null; targetId?: string };

function tabMatchesTarget(tab: Tab, ctx: ReqCtx): boolean {
  return tab.db === ctx.db && tab.env === ctx.env && tab.targetId === ctx.targetId &&
    cloudRouteVisible(useConnStore.getState().groups, ctx.env) &&
    connectionTargetId(ctx.db, ctx.env) === ctx.targetId;
}

function canAutoRun(): boolean {
  const current = useConnStore.getState().current;
  if (current?.sourceStatus && current.sourceStatus !== "connected") return false;
  return autoRunEnabled(current ? { ...current, workspace: workspaceFor(current.db) } : null,
    useUiStore.getState().autoRunPreferences);
}

function missingCloudTarget(item: ConnItem | undefined, env: string | null): boolean {
  if (!env) return false;
  if (NATIVE_MODE && (env.startsWith("remote:") || env.startsWith("local:"))) return true;
  return !!useConnStore.getState().cloudStatus && env.startsWith("cloud:") &&
    !item?.envs.some((entry) => entry.env === env && entry.source === "cloud");
}

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
  return rows.slice().sort((a, b) => {
    const x = a[col];
    const y = b[col];
    if (x == null && y == null) return 0;
    if (x === null || x === undefined) return 1;
    if (y === null || y === undefined) return -1;
    return compareCellValues(x, y) * dir;
  });
}

function toCSV(columns: QueryColumn[], rows: Row[]): string {
  const cols = columns.map((c) => c.name);
  const esc = (v: unknown): string => {
    const s = cellText(v);
    if (s === null) return "";
    return /[",\r\n]/.test(s) ? `"${s.replaceAll('"', '""')}"` : s;
  };
  return [cols.map(esc).join(","), ...rows.map((r) => cols.map((c) => esc(r[c])).join(","))].join("\n");
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
  const selected = useConnStore((s) => s.current);
  const current = cloudRouteVisible(groups, selected?.env) ? selected : null;
  const currentTable = useConnStore((s) => s.currentTable);
  const reloadToken = useConnStore((s) => s.reloadToken);
  const maxRows = useUiStore((s) => s.maxRows);
  const setMaxRows = useUiStore((s) => s.setMaxRows);
  const autoRun = useUiStore((s) => autoRunEnabled(
    current ? { ...current, workspace: workspaceFor(current.db) } : null, s.autoRunPreferences));
  const sidebarWidth = useUiStore((s) => s.sidebarWidth);
  const setSidebarWidth = useUiStore((s) => s.setSidebarWidth);

  const tabs = useTabsStore((s) => s.tabs);
  const storedActiveTabId = useTabsStore((s) => s.activeId);
  const activeTabId = cloudRouteVisible(groups, tabs.find((tab) => tab.id === storedActiveTabId)?.env) ? storedActiveTabId : "";
  const results = useTabsStore((s) => s.results);
  const updateActiveTab = useTabsStore((s) => s.updateActiveTab);
  const updateTab = useTabsStore((s) => s.updateTab);
  const setTabResult = useTabsStore((s) => s.setTabResult);

  const activeTab = useMemo(
    () => tabs.find((tb) => tb.id === activeTabId),
    [tabs, activeTabId],
  );
  const sql = activeTab?.sql ?? "";
  const setSql = useCallback(
    (v: string): void => {
      const state = useTabsStore.getState();
      if (!state.activeId || state.tabs.find((tab) => tab.id === state.activeId)?.savedQueryId) state.addTab();
      updateActiveTab({ sql: v });
    },
    [updateActiveTab],
  );
  const sqlRef = useRef(sql);
  sqlRef.current = sql;

  const [collapseToken, setCollapseToken] = useState(0);
  const [savedQueries, setSavedQueries] = useState<SavedQuery[]>([]);
  const [panel, setPanel] = useState<PanelData>(EMPTY_PANEL);
  const [panelOpen, setPanelOpen] = useState(true);
  const [filter, setFilter] = useState("");
  const [pendingLocalDb, setPendingLocalDb] = useState<string | null>(null);
  const [sortState, setSortState] = useState<SortState>(null);
  const [selectedCell, setSelectedCell] = useState<SelectedCell>(null);
  const [imagePreview, setImagePreview] = useState<{ tabId: string; result: QueryResult; column: number; url: string } | null>(null);
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
  const reqIdsRef = useRef<Record<TabId, string | undefined>>({});
  const reqSeqRef = useRef<Record<TabId, number>>({});
  const [pendingByTab, setPendingByTab] = useState<Record<TabId, boolean>>({});
  const [errorByTab, setErrorByTab] = useState<Record<TabId, string | null>>({});
  const loading = pendingByTab[activeTabId] ?? false;
  const gridError = errorByTab[activeTabId] ?? null;

  const snapshot = results[activeTabId] as TabResultSnapshot | undefined;
  const activeSnapshot = !pendingLocalDb && snapshot && snapshot.queryDb === activeTab?.db && snapshot.queryEnv === activeTab.env &&
    snapshot.targetId === activeTab.targetId && (!snapshot.targetId || snapshot.targetId === connectionTargetId(snapshot.queryDb, snapshot.queryEnv))
    ? snapshot : undefined;
  const result = activeSnapshot?.result ?? null;
  const queryDb = activeSnapshot?.queryDb ?? null;
  const queryEnv = activeSnapshot?.queryEnv ?? null;
  const querySql = activeSnapshot?.querySql ?? null;

  const { history, pushHist, keepDraft, navigateHistory } = useSqlHistory();
  const previewNavigationRef = useRef<{ tabId: string; environmentSwitch: boolean } | null>(null);
  const beforeAssistantWrite = useCallback((tabId: string) => {
    if (previewNavigationRef.current?.tabId === tabId) previewNavigationRef.current = null;
  }, []);
  const assistant = useSqlAssistant(keepDraft, beforeAssistantWrite);
  const closeAssistantTab = assistant.closeTab;
  const assistantSession = assistant.sessions[activeTabId] ?? {};

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
                if (stillCurrent()) applyPanel(fd);
              })
              .catch(() => {});
          }
        })
        .catch((e) => {
          const msg = String((e as Error).message ?? e);
          setHealth(db, false, msg);
          if (stillCurrent()) {
            if (cached) applyPanel(cached);
            setPanel((previous) => ({ ...previous, loading: false, error: msg }));
          }
        });
    },
    [applyPanel],
  );

  /* ---- connection selection (the legacy selectDb) ---- */

  const clearCurrentSelection = useCallback((): void => {
    const state = useConnStore.getState();
    state.setCurrent(null);
    state.setCurrentTable(null);
    setPanel(EMPTY_PANEL);
    panelKeyRef.current = null;
    setPendingLocalDb(null);
  }, []);
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
      if (missingCloudTarget(item, env) || !cloudRouteVisible(state.groups, env)) {
        clearCurrentSelection();
        return;
      }
      if (!item) return;
      if (cur?.db !== db) {
        state.setCurrentTable(null);
        setFilter("");
      }
      previewNavigationRef.current = null;
      const tabsState = useTabsStore.getState();
      const source = tabsState.tabs.find((tab) => tab.id === tabsState.activeId);
      const environmentSwitch = !!opts?.viaPill && cur?.db === db && cur.env !== env;
      const sourceTable = cur?.db === db && state.currentTable &&
        source?.sql.trim() === previewSql(state.currentTable, cur.engine) ? state.currentTable : null;
      const recent = mostRecent(tabsState.tabs.filter((tab) =>
        tab.db === db && tab.workspace === workspaceFor(db) &&
        item.envs.some((e) => e.env === tab.env)));
      const explicit = env !== null || ((opts?.viaPill || opts?.force) &&
        item.envs.some((entry) => entry.env === null));
      if (!explicit && !recent && needsLocalTargetChoice(item)) {
        clearCurrentSelection();
        setPendingLocalDb(item.db);
        return;
      }
      const realEnv = explicit ? env : recent ? recent.env : defaultEnvFor(item);
      if (ambiguousConnection(db, realEnv)) {
        clearCurrentSelection();
        toast(t("connection_ambiguous"), false);
        return;
      }
      const target = item.envs.find((entry) => entry.env === realEnv);
      const engine = target?.engine ?? item.engine;
      const existed = tabsState.tabs.some((tab) => tab.db === db && tab.env === realEnv && tab.workspace === workspaceFor(db));
      if (!opts?.force) tabsState.selectGroup(db, realEnv);
      else tabsState.updateActiveTab({ db, env: realEnv });
      const nextState = useTabsStore.getState();
      const next = nextState.tabs.find((tab) => tab.id === nextState.activeId);
      if (!opts?.force && next) {
        // Empty destination editors inherit SQL on an explicit environment switch.
        // Existing drafts and deliberately closed groups retain their own state.
        if (!next.sql.trim() && environmentSwitch && source?.sql.trim() &&
            !federatedRoute(source.env) && !federatedRoute(realEnv) &&
            !localTargetRoute(source.env) && !localTargetRoute(realEnv)) {
          nextState.updateActiveTab({ sql: source.sql });
        } else if (!existed && !next.sql.trim() && sourceTable && !federatedRoute(source?.env) && !federatedRoute(realEnv) &&
                   !localTargetRoute(source?.env) && !localTargetRoute(realEnv)) {
          nextState.updateActiveTab({ sql: previewSql(sourceTable, engine) });
        } else if (engine === "neptune" && !next.sql.trim()) {
          nextState.updateActiveTab({ sql: NEPTUNE_STARTER_SQL });
        }
        if (!localTargetRoute(realEnv)) previewNavigationRef.current = { tabId: next.id, environmentSwitch };
      }
      state.setCurrent({ db, env: realEnv, engine, isRedis: engine === "redis", production: target?.production, source: target?.source,
        targetId: target?.targetId, sourceStatus: target?.sourceStatus });
      setPendingLocalDb(null);
      state.setCurrentTable(null);
      setFilter("");
      setPanelOpen(true);
      loadTables(db, realEnv, false);
    },
    [clearCurrentSelection, findItem, loadTables],
  );

  /** No / vanished connection: unbind the tab — never silently rebind. */
  const unbindActiveTab = useCallback((): void => {
    updateActiveTab({ db: null, env: null });
    clearCurrentSelection();
  }, [clearCurrentSelection, updateActiveTab]);

  /* ---- initial load + workspace-change reload (the legacy loadSide) ---- */

  useEffect(() => {
    let cancelled = false;
    let revision = 0;
    let refreshing = false;
    const loadConnections = (refresh = false): void => {
      refreshing = true;
      const seq = ++revision;
      fetchConnections()
        .then((data) => {
          if (cancelled || seq !== revision) return;
          const conn = useConnStore.getState();
          const gainedCloudScope = refresh && conn.loaded && data.groups.some((group) => group.items.some((item) =>
            item.envs.some((env) => federatedRoute(env.env) && !cloudRouteVisible(conn.groups, env.env))));
          if (refresh && data.workspace === conn.workspace &&
            JSON.stringify(data.workspaces) === JSON.stringify(conn.workspaces) &&
            JSON.stringify(data.groups) === JSON.stringify(conn.groups)) {
            conn.setCloudStatus(data.cloudStatus);
            return;
          }
          const revoked = useTabsStore.getState().tabs.filter((tab) =>
            cloudRouteVisible(conn.groups, tab.env) && !cloudRouteVisible(data.groups, tab.env));
          if (revoked.length) {
            for (const tab of revoked) {
              reqSeqRef.current[tab.id] = (reqSeqRef.current[tab.id] ?? 0) + 1;
              delete reqIdsRef.current[tab.id];
            }
            setPendingByTab((pending) => Object.fromEntries(Object.entries(pending).filter(([id]) =>
              !revoked.some((tab) => tab.id === id))));
          }
          conn.setConnMeta(data.workspace, data.workspaces, data.groups, data.cloudStatus);
          useTabsStore.getState().claimWorkspaces();
          useTabsStore.getState().showAvailableTabs();
          if (gainedCloudScope) void restoreNativeScope();
          // Paint health dots instantly from the backend cache, without probing.
          for (const g of data.groups) {
            for (const item of g.items) {
              const env = defaultEnvFor(item);
              fetchHealth(item.db, env, { cachedOnly: true })
                .then((d) => {
                  if (!cancelled && seq === revision && (d.ok === true || d.ok === false))
                    conn.setHealth(item.db, d.ok, d.error);
                })
                .catch(() => {});
            }
          }
        })
        .catch(() => {
          if (cancelled || seq !== revision) return;
          if (refresh) useConnStore.getState().setCloudStatus("unavailable");
          else useConnStore.getState().setConnMeta("", [], []);
        }).finally(() => { if (seq === revision) refreshing = false; });
    };
    const federated = document.querySelector('meta[name="quarry-federated"]') !== null;
    const refreshOnFocus = (): void => {
      if (!refreshing && (federated || useConnStore.getState().cloudStatus)) loadConnections(true);
    };
    loadConnections();
    window.addEventListener("focus", refreshOnFocus);
    const timer = federated ? window.setInterval(refreshOnFocus, 6000) : undefined;
    fetchQueries()
      .then((qs) => !cancelled && setSavedQueries(qs))
      .catch(() => !cancelled && setSavedQueries([]));
    return () => {
      cancelled = true;
      window.removeEventListener("focus", refreshOnFocus);
      if (timer !== undefined) window.clearInterval(timer);
    };
  }, [reloadToken]);

  useEffect(() => {
    if (PLATFORM_MODE) return;
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
    if (useConnStore.getState().current?.source === "cloud" &&
        !cloudRouteVisible(groups, useConnStore.getState().current?.env)) {
      setModal(null);
      setHistoryOpen(false);
      setParamModal(null);
      setConnInfoOpen(false);
      clearCurrentSelection();
    }
    // a just-created local env takes precedence over restoring the tab's
    // previous connection (the legacy `loadSide(); selectDb(db,'local')`)
    const pending = pendingSelectRef.current;
    pendingSelectRef.current = null;
    if (pending && findItem(pending.db)) {
      selectDb(pending.db, pending.env);
      return;
    }
    const tab = useTabsStore.getState().tabs.find((tb) => tb.id === useTabsStore.getState().activeId);
    if (tab?.db && missingCloudTarget(findItem(tab.db), tab.env)) {
      // Keep a cloud draft bound to its original target through outages and revocation.
      clearCurrentSelection();
      return;
    }
    if (tab?.db && findItem(tab.db) && tab.workspace === workspaceFor(tab.db) &&
        tab.targetId === connectionTargetId(tab.db, tab.env)) selectDb(tab.db, tab.env ?? null, { force: true });
    else if (tab?.db && !PLATFORM_MODE) unbindActiveTab();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loaded, loadSeq]);

  /* ---- per-tab request guards ---- */

  const startReq = (tabId: TabId, target: { db: string; env: string | null }, cancellable = true): ReqCtx => {
    const seq = (reqSeqRef.current[tabId] ?? 0) + 1;
    reqSeqRef.current[tabId] = seq;
    const requestId = crypto.randomUUID();
    reqIdsRef.current[tabId] = cancellable ? requestId : undefined;
    setPendingByTab((prev) => ({ ...prev, [tabId]: true }));
    setErrorByTab((prev) => ({ ...prev, [tabId]: null }));
    return { tabId, requestId, seq, db: target.db, env: target.env, targetId: connectionTargetId(target.db, target.env) };
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
    if (!tabMatchesTarget(tab, ctx)) return;
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
    if (!tabMatchesTarget(tab, ctx)) return;
    setTabResult(ctx.tabId, { ...build(), targetId: ctx.targetId });
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
    const tabState = useTabsStore.getState();
    const fixed = tabState.tabs.find((tab) => tab.id === tabState.activeId);
    if (fixed?.savedQueryId && overrideSql === undefined) {
      const current = useConnStore.getState().current;
      if (!current || current.db !== fixed.db || current.env !== fixed.env || current.targetId !== fixed.targetId) return;
      const saved = savedQueries.find((q) => (q.queryId ?? JSON.stringify([q.ws ?? null, q.name, q.db])) === fixed.savedQueryId);
      if (!saved) { toast(t("saved_missing"), false); return; }
      if (saved.params.length) setParamModal(saved);
      else await runSavedQuery(saved.queryId ?? saved.name, {});
      return;
    }
    const state = useConnStore.getState();
    const target = overrideTarget ?? (state.current ? { db: state.current.db, env: state.current.env } : null);
    // an explicit SQL is passed by call sites that just called setSql() — the
    // store update hasn't re-rendered into sqlRef yet
    const currentSql = (overrideSql ?? sqlRef.current).trim();
    if (!target || !currentSql || ambiguousConnection(target.db, target.env) || !cloudRouteVisible(state.groups, target.env) ||
        (fixed?.targetId && fixed.targetId !== connectionTargetId(target.db, target.env))) return;
    pushHist(currentSql, target.db, target.env);
    const ctx = startReq(useTabsStore.getState().activeId, target);
    try {
      const data = await runQuery({ db: target.db, env: target.env, sql: currentSql, maxRows, requestId: ctx.requestId, target: ctx.targetId });
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
      if (focus && (!focus.targetId || focus.targetId === connectionTargetId(focus.db, focus.env))) {
        const item = findItem(focus.db);
        if (item) {
          const tabsState = useTabsStore.getState();
          if (!focus.table && !tabsState.activeId) {
            selectDb(focus.db, focus.env);
            setSessionReady(true);
            return;
          }
          const plan = focusRestorePlan(
            tabsState.tabs.filter((tab) => tab.workspace === workspaceFor(focus.db)),
            tabsState.activeId,
            focus.db,
            focus.env,
            focus.table,
            item.engine,
          );
          if (plan.action === "reuse" || plan.action === "same") {
            if (plan.tabId !== tabsState.activeId) tabsState.switchTab(plan.tabId);
          } else if (plan.action === "new") {
            tabsState.addTab({ db: focus.db, env: focus.env });
          }
          if (focus.table && plan.action !== "same") {
            useTabsStore.getState().updateActiveTab({
              db: focus.db,
              env: focus.env,
              sql: previewSql(focus.table, item.engine),
            });
            useConnStore.getState().setCurrentTable(focus.table);
          }
          selectDb(focus.db, focus.env, { force: true });
        }
      }
      setSessionReady(true);
      return;
    }
    if (payload.targetId && payload.targetId !== connectionTargetId(payload.db, payload.env)) {
      toast(t("share_link_env_missing"), false);
      setSessionReady(true);
      return;
    }
    const tabsState = useTabsStore.getState();
    const existing = tabsState.tabs.find(
      (tb) =>
        tb.workspace === workspaceFor(payload.db) &&
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
      if (missingCloudTarget(item, payload.env)) {
        clearCurrentSelection();
        toast(t("cloud_target_unavailable"), false);
        return;
      }
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
      if (!canAutoRun()) return;
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
        requestId: ctx.requestId,
        target: ctx.targetId,
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

  // Only exact generated previews represent a selected table. Derive this
  // after every tab / SQL / table-list change, including cached and late lists.
  useEffect(() => {
    const table = current && activeTab?.db === current.db && activeTab?.env === current.env
      ? panel.tables?.find((name) => sql.trim() === previewSql(name, current.engine)) ?? null
      : null;
    if (table !== currentTable) useConnStore.getState().setCurrentTable(table);
  }, [sql, activeTab?.db, activeTab?.env, currentTable, current, panel.tables]);

  // Consume navigation once, after the destination table list has arrived.
  // Missing production metadata (e.g. an older server) never authorizes auto-run.
  useEffect(() => {
    const navigation = previewNavigationRef.current;
    if (!navigation) return;
    const { tabId, environmentSwitch } = navigation;
    if (activeTabId !== tabId || !current || !canAutoRun()) {
      previewNavigationRef.current = null;
      return;
    }
    if (panel.loading) return;
    previewNavigationRef.current = null;
    const snapshot = useTabsStore.getState().results[tabId];
    // Returning to a tab restores its result, including when SQL has since been
    // edited. Only explicit Run should replace that saved result with the draft.
    const unexecutedPreview = !panel.error && panel.tables?.some(
      (name) => sql.trim() === previewSql(name, current.engine),
    );
    if (!snapshot?.result && !pendingByTab[tabId] && sql.trim() && !current.isRedis &&
        (environmentSwitch || unexecutedPreview)) {
      void run({ db: current.db, env: current.env }, sql);
    }
    // run reads the current stores; only navigation and destination readiness trigger this.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeTabId, current, panel, sql]);

  useEffect(() => {
    if (!sessionReady) return;
    const db = current?.db ?? null;
    const env = current?.env ?? null;
    const table = currentTable ?? parseMainTable(sql);
    const search = encodeFocusSearch({ db, env, table, targetId: current?.targetId });
    const url = new URL(window.location.href);
    const pathname = CLI_SESSION_MODE ? url.pathname : "/quarry/";
    if (url.search !== search || url.pathname !== pathname) {
      window.history.replaceState(null, "", `${pathname}${search}${url.hash}`);
    }
    document.title = focusTitle({ db: db && (findItem(db)?.label ?? db), env: db && (current?.source === "cloud" || localTargetRoute(env)) ? displayConnectionEnvironment(db, env) : env, table });
  }, [sessionReady, current, currentTable, sql, findItem]);

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
    const plan = tableClickPlan(tabsState.tabs.filter((tab) => tab.workspace === workspaceFor(cur.db)), tabsState.activeId, cur.db, cur.env, table, cur.engine);
    if (plan.action === "reuse") {
      const tab = tabsState.tabs.find((tb) => tb.id === plan.tabId);
      if (tab && tab.id !== tabsState.activeId) {
        tabsState.switchTab(tab.id);
        revalidateTab(tab);
      }
      useConnStore.getState().setCurrentTable(table);
      if (canAutoRun() && !pendingByTab[plan.tabId] && !useTabsStore.getState().results[plan.tabId]?.result) {
        void run({ db: cur.db, env: cur.env }, next);
      }
      return;
    }
    if (plan.action === "new") {
      tabsState.addTab({ db: cur.db, env: cur.env });
    }
    useTabsStore.getState().updateActiveTab({ db: cur.db, env: cur.env, sql: next });
    useConnStore.getState().setCurrentTable(table);
    if (canAutoRun()) void run({ db: cur.db, env: cur.env }, next);
  };

  const handleInspectKey = async (key: string): Promise<void> => {
    const cur = useConnStore.getState().current;
    if (!cur) return;
    const placeholder = `# ${key}`;
    keepDraft(sqlRef.current, placeholder, cur.db, cur.env);
    setSql(placeholder);
    const ctx = startReq(useTabsStore.getState().activeId, { db: cur.db, env: cur.env }, false);
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

  const openSaved = (name: string, preview = false): void => {
    const q = savedQueries.find((x) => (x.queryId ?? x.name) === name);
    if (!q) return;
    const cur = useConnStore.getState().current;
    const nv = q.sql || `-- ${q.name}`;
    const related = groups.flatMap((group) => group.items.map((item) => ({ item, workspace: group.ws })));
    const matching = related.flatMap(({ item, workspace }) => item.envs.filter((env) => {
      if (q.targetIds) {
        if (env.source === "cloud") return !!env.associatedTargetId && !!q.ownerTargetIds?.includes(env.associatedTargetId);
        return !!env.targetId && q.targetIds.includes(env.targetId);
      }
      // Older local servers don't expose exact target IDs for saved queries.
      return (!q.ws || q.ws === workspace || q.ws === env.workspaceLabel) &&
        (item.db === q.db || (item.label === q.db && env.source !== "cloud") || env.key === q.db);
    }).map((env) => ({ item, env })));
    const current = matching.find(({ item, env }) => item.db === cur?.db && env.env === cur?.env && env.targetId === cur?.targetId);
    const local = matching.filter(({ env }) => env.source !== "cloud");
    const owned = local.filter(({ env }) => env.targetId && q.ownerTargetIds?.includes(env.targetId));
    const defaults = owned.length ? owned : local.length ? local : matching;
    const pinned = defaults.find(({ env }) => env.key === q.db);
    const defaultItem = defaults[0]?.item;
    const defaultEnv = defaultItem && defaultEnvFor({ ...defaultItem, envs: defaults.map(({ env }) => env) });
    const selected = current ?? pinned ?? defaults.find(({ env }) => env.env === defaultEnv);
    if (!selected) { toast(t("saved_missing"), false); return; }
    const { item } = selected;
    const env = selected.env.env;
    const state = useTabsStore.getState();
    const created = state.openSavedTab({ ...q, sql: nv }, item.db, env);
    selectDb(item.db, env, { force: true });
    if (!created) return;
    if (!canAutoRun()) return;
    if (preview && !q.params.length) return;
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
    const meta = savedQueries.find((x) => (x.queryId ?? x.name) === name);
    const cur = useConnStore.getState().current;
    const { activeId: tabId, tabs } = useTabsStore.getState();
    const pinned = tabs.find((tab) => tab.id === tabId);
    if (!cur || !pinned || pinned.db !== cur.db || pinned.env !== cur.env || pinned.targetId !== cur.targetId ||
        !cloudRouteVisible(useConnStore.getState().groups, cur.env) ||
        connectionTargetId(cur.db, cur.env) !== cur.targetId) return;
    const ctx = startReq(tabId, cur);
    try {
      const data = await runSaved(meta?.name ?? name, cur.env, params, maxRows, meta?.queryId, ctx.requestId, ctx.targetId, cur.db);
      if (!isCurrentReq(ctx)) return;
      const state = useTabsStore.getState();
      const tab = state.tabs.find((tb) => tb.id === tabId);
      if (!tab) return; // tab closed while in flight
      if (federatedRoute(ctx.env) && !tabMatchesTarget(tab, ctx)) return;
      const { db: resDb, env: resEnv, ...clean } = data;
      const db = resDb ?? meta?.db ?? tab.db;
      const env = resEnv ?? null;
      updateTab(tabId, { db, env, sql: clean.sql });
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
        // Keep the saved template intact; only the result contains substituted SQL.
        if (!tab.savedQueryId) setSql(clean.sql);
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
    if (activeTab?.savedQueryId) return;
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
   * tabular plans (mysql/sqlite) render in the grid. The modal is suppressed if its
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
      const prefix = cur.engine === "sqlite" ? "EXPLAIN QUERY PLAN" : "EXPLAIN";
      const explainSql = `${prefix} ${trimmed.replace(/^\s*explain\s+(?:query\s+plan\s+)?/i, "")}`;
      const data = await runQuery({ db: cur.db, env: cur.env, sql: explainSql, maxRows, requestId: ctx.requestId, target: ctx.targetId });
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
      if (!tabMatchesTarget(tab, ctx)) return;
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
      if (snap?.result && (snap.queryDb !== tab.db || (snap.queryEnv ?? null) !== (tab.env ?? null) || snap.targetId !== tab.targetId)) {
        setTabResult(tab.id, null);
      }
      if (tab.db && missingCloudTarget(findItem(tab.db), tab.env)) clearCurrentSelection();
      else if (tab.db && findItem(tab.db) && tab.workspace === workspaceFor(tab.db) &&
        tab.targetId === connectionTargetId(tab.db, tab.env)) selectDb(tab.db, tab.env ?? null, { force: true });
      else if (tab.db) unbindActiveTab();
      else clearCurrentSelection();
    },
    [clearCurrentSelection, findItem, selectDb, setTabResult, unbindActiveTab],
  );

  const handleTabSwitch = useCallback(
    (tab: Tab): void => {
      if (!cloudRouteVisible(useConnStore.getState().groups, tab.env)) return;
      useTabsStore.getState().switchTab(tab.id);
      revalidateTab(tab);
      const group = useConnStore.getState().groups.find((g) => g.items.some((item) => item.db === tab.db));
      if (group) {
        const key = groupKey(group.ws, group.group);
        const ui = useUiStore.getState();
        if (ui.collapsedGroups.has(key)) ui.toggleCollapsedGroup(key);
      }
      setFilter("");
      setPanelOpen(true);
    },
    [revalidateTab],
  );

  const handleTabClose = useCallback(
    (tab: Tab): void => {
      // Closing a tab must never silently lose hand-written SQL.
      const dying = tab.sql.trim();
      if (dying) pushHist(dying, tab.db, tab.env);
      closeAssistantTab(tab);
      const store = useTabsStore.getState();
      const wasActive = store.activeId === tab.id;
      store.closeTab(tab.id);
      if (wasActive) {
        const next = useTabsStore.getState();
        revalidateTab(next.tabs.find((tb) => tb.id === next.activeId));
      }
    },
    [pushHist, revalidateTab, closeAssistantTab],
  );

  // Cmd/Ctrl+Shift+W closes the active tab (real Cmd/Ctrl+W can't be
  // intercepted — it closes the browser tab itself); disabled while renaming
  // while renaming. Closing the last tab shows the empty workbench for that group.
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent): void => {
      if (!(e.metaKey || e.ctrlKey) || !e.shiftKey) return;
      if (e.key !== "w" && e.key !== "W") return;
      if (document.querySelector(".vg-tab.renaming")) return;
      const state = useTabsStore.getState();
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
    setImagePreview(null);
  }, [activeTabId]);

  useEffect(() => {
    setImagePreview((previous) => previous?.result === result ? previous : null);
  }, [result]);

  const enabledImageColumns = activeSnapshot?.imageColumns;

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
    (result.engine === "postgres" || result.engine === "mysql" || result.engine === "sqlite")
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
        requestId: ctx.requestId,
        target: ctx.targetId,
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

  const openCell = useCallback((value: unknown, env: string | null): void => {
    setModal({ type: "cell", value, env });
  }, []);

  const onCellDblClick = (v: unknown): void => {
    if (cellOpensInspector(v)) openCell(v, queryEnv);
    else copy(cellText(v) ?? "");
  };

  // Grid keyboard navigation: arrows move the selected cell, Enter inspects,
  // Cmd/Ctrl+C copies — only while not typing and no modal is open.
  const gridNavRef = useRef({ shownRows, selectedCell, result, queryEnv });
  gridNavRef.current = { shownRows, selectedCell, result, queryEnv };
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent): void => {
      const { shownRows: rows, selectedCell: sel, result: res, queryEnv: env } = gridNavRef.current;
      if (!sel || !res || anyModalOpen() || (document.activeElement as HTMLElement)?.closest("button, select, a")) return;
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
        openCell(v, env);
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [openCell]);

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
    setHistoryOpen(true);
  };

  const resumeConversation = async (id: string): Promise<void> => {
    const conversation = await getConversation(id);
    const state = useTabsStore.getState();
    const existing = state.tabs.find((tab) => tab.conversationId === id);
    if (existing) {
      handleTabSwitch(existing);
      assistant.attach(existing.id, conversation);
    } else {
      const item = conversation.db ? findItem(conversation.db) : undefined;
      if (conversation.db && (conversation.connectionAvailable === false || !item || workspaceFor(conversation.db) !== (conversation.guiWorkspace ?? conversation.workspace) ||
        !item.envs.some((env) => env.env === conversation.env))) {
        throw new Error(t("assistant_connection_missing"));
      }
      state.addTab({ db: conversation.db, env: conversation.env });
      const tabId = useTabsStore.getState().activeId;
      state.updateTab(tabId, { sql: conversation.sql });
      assistant.attach(tabId, conversation);
      revalidateTab(useTabsStore.getState().tabs.find((tab) => tab.id === tabId));
    }
    // Restoring a conversation is navigation only, regardless of Auto-run.
    previewNavigationRef.current = null;
    setHistoryOpen(false);
  };

  const copyQueryLink = (): void => {
    if (!activeTab?.db) {
      toast(t("pick_conn"), false);
      return;
    }
    const link = encodeQueryLink(window.location.href, {
      db: activeTab.db,
      env: activeTab?.env ?? null,
      sql: activeTab.sql,
      targetId: activeTab.targetId,
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

  const item = current ? findItem(current.db) : pendingLocalDb ? findItem(pendingLocalDb) : undefined;
  const envs = item?.envs ?? [];
  const showEnv = envs.length > 1 || envs.some((env) => env.source === "cloud" || env.workspaceLabel);
  const isProduction = current?.production === true;
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
        collapseToken={collapseToken}
        onCollapseAll={() => {
          const ui = useUiStore.getState();
          for (const group of groupsWithQueries(groups, savedQueries)) {
            const key = groupKey(group.ws, group.group);
            for (const target of [key, key + '::queries']) {
              if (!useUiStore.getState().collapsedGroups.has(target)) ui.toggleCollapsedGroup(target);
            }
          }
          setCollapseToken((n) => n + 1);
          setPanelOpen(false);
        }}
        onOpenSearchObject={(db, env, name, redis) => {
          selectDb(db, env, { viaPill: true });
          if (redis) void handleInspectKey(name);
          else handleTableClick(name, false);
        }}
      />
      <div className="vg-resizer resizer" id="resizer" onMouseDown={startSidebarResize} />
      <section className="vg-section">
        <div className="vg-qhead qhead">
          <div className="connection-context">
          <span className="vg-qtitle qtitle" id="qtitle">
            {item?.label ?? current?.db ?? pendingLocalDb ?? t("no_conn")}
          </span>
          <span className="vg-runon runon" id="runon" style={{ display: showEnv ? undefined : "none" }}>
            {t(pendingLocalDb ? "choose_local_target" : "runs_on")}
          </span>
          <span className="vg-esw esw" id="esw">
            {item &&
              showEnv &&
              envs.map((e) => (
                <span
                  key={e.env ?? ""}
                  className={`vg-ep ep${current && e.env === current.env ? " on" : ""}${e.production ? " prod" : ""}`}
                  data-env={e.env ?? ""}
                  role="button" tabIndex={0} aria-pressed={current?.env === e.env}
                  title={e.providerLabel ? `${e.providerLabel}${e.sourceStatus ? ` · ${t(`cloud_${e.sourceStatus}`)}` : ""}` : undefined}
                  onClick={() => selectDb(item.db, e.env ?? null, { viaPill: true })}
                  onKeyDown={(event) => {
                    if (event.key !== "Enter" && event.key !== " ") return;
                    event.preventDefault();
                    selectDb(item.db, e.env ?? null, { viaPill: true });
                  }}
                >
                  {connectionEnvLabel(e)}
                  {e.source === "cloud" && <span className="cloud-source-badge">{e.providerLabel ?? t("cloud_source")}</span>}
                  {e.proxied && (
                    <span
                      className="vg-proxy-badge proxy-badge"
                      data-testid="proxy-badge"
                      data-db={item.db}
                      data-env={e.env ?? ""}
                      title={t("proxy_badge_title")}
                    >
                      ⇄
                    </span>
                  )}
                </span>
              ))}
          </span>
          <button
            className="vg-iconbtn iconbtn"
            id="ciBtn"
            title={t("conn_info")}
            aria-label={t("conn_info")}
            style={{ display: current && !PLATFORM_MODE ? undefined : "none" }}
            onClick={() => setConnInfoOpen(true)}
          >
            <i className="ti ti-info-circle" />
          </button>
          </div>
        </div>
        <TabBar isProduction={isProduction} onSwitch={handleTabSwitch} onClose={handleTabClose} />
        {activeTab ? <>
        <EditorWorkspace open={!!assistantSession.open && current?.source !== "cloud" && !localTargetRoute(current?.env)}
          unavailableReason={current?.source === "cloud" ? t("assistant_native_cloud_unavailable") : localTargetRoute(current?.env) ? t("assistant_exact_workspace_unavailable") : undefined}
          onOpen={() => void assistant.open(activeTab)} onClose={() => assistant.close(activeTabId)}
          assistant={<SqlAssistant key={activeTabId} session={assistantSession} currentSql={activeTab.sql}
            onDraftChange={(update) => assistant.setDraft(activeTabId, update)}
            onSend={(prompt, explain) => assistant.send(useTabsStore.getState().tabs.find((tab) => tab.id === activeTabId) ?? activeTab, prompt, explain)}
            onApply={(message) => void assistant.apply(activeTab, message)}
            onRestore={(checkpointId) => void assistant.restore(activeTab, checkpointId)} />}
          toolbar={
        <div className="vg-toolbar toolbar">
          <RunControls
            key={JSON.stringify([current ? workspaceFor(current.db) : null, current?.db, current?.env, current?.production, activeTabId])}
            autoRun={autoRun}
            disabled={!current || typeof current.production !== "boolean"}
            scope={[current?.db, current ? displayConnectionEnvironment(current.db, current.env) : null].filter(Boolean).join(" / ")}
            onRun={() => void run()}
            onModeChange={(enabled) => {
              const target = useConnStore.getState().current;
              if (!target || typeof target.production !== "boolean") return;
              // Changing a preference never executes an earlier navigation.
              previewNavigationRef.current = null;
              useUiStore.getState().setAutoRun({ ...target, workspace: workspaceFor(target.db) }, enabled);
            }}
          />
          {pendingByTab[activeTabId] && reqIdsRef.current[activeTabId] && current && (PLATFORM_MODE || current.source === "cloud") && <button className="vg-btn btn" data-testid="cancel-platform-query" onClick={() => {
            const requestId = reqIdsRef.current[activeTabId];
            const seq = reqSeqRef.current[activeTabId];
            if (!requestId || seq === undefined) return;
            const ctx: ReqCtx = { tabId: activeTabId, requestId, seq, db: current.db, env: current.env };
            void cancelPlatformQuery(current.db, current.env, requestId).then(() => {
              // Cancellation belongs to the request clicked, even if another
              // query starts on this tab before the acknowledgement arrives.
              if (!isCurrentReq(ctx)) return;
              reqSeqRef.current[ctx.tabId] = ctx.seq + 1;
              setPendingByTab((previous) => ({ ...previous, [ctx.tabId]: false }));
            }).catch((error) => toast((error as Error).message, false));
          }}>{t("platform_cancel_query")}</button>}
          {activeTab.savedQueryId && <button className="vg-btn btn" id="copySavedBtn"
            title={t("copy_as_query")} onClick={() => {
              useTabsStore.getState().addTab({ db: activeTab.db, env: activeTab.env });
              useTabsStore.getState().updateActiveTab({ sql: activeTab.sql });
            }}><i className="ti ti-copy" /> {t("copy_as_query")}</button>}
          <button className="vg-btn btn" id="fmtBtn" title={t("fmt")} disabled={!!activeTab.savedQueryId} onClick={formatSql}>
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
          <button className="vg-btn btn" id="csvBtn" title={t("export_csv")} onClick={exportCsv}>
            <i className="ti ti-download" /> CSV
          </button>
          <button className="vg-btn btn" id="jsonBtn" title={t("export_json")} onClick={exportJson}>
            <i className="ti ti-braces" /> JSON
          </button>
          <div className="row-limit">
            <select
              id="maxRows"
              className="vg-btn btn"
              title={t("max_rows")}
              aria-label={t("max_rows")}
              value={String(maxRows)}
              onChange={(e) => setMaxRows(Number(e.target.value))}
            >
              {MAX_ROWS_OPTIONS.map((n) => (
                <option key={n} value={n}>
                  {n} {t("rows")}
                </option>
              ))}
            </select>
            <i className="ti ti-chevron-down" aria-hidden="true" />
          </div>
          <span className="vg-sp sp" />
          <button className="vg-btn btn" id="histBtn" title={t("hist")} onClick={openHistory}>
            <i className="ti ti-history" /> <span id="histLbl">{t("hist")}</span>
          </button>
          {!CLI_SESSION_MODE && <button className="vg-btn btn" id="linkBtn" title={t("copy_query_link")} onClick={copyQueryLink}>
            <i className="ti ti-link" /> <span id="linkLbl">{t("copy_query_link")}</span>
          </button>}
        </div>
          }>
        <SqlEditor
          embedded
          isProduction={isProduction}
          readOnly={!!activeTab.savedQueryId}
          value={sql}
          onChange={(next) => {
            previewNavigationRef.current = null;
            setSql(next);
          }}
          onRun={() => void run()}
          db={current?.db ?? null}
          env={current?.env ?? null}
          isRedis={!!current?.isRedis}
          tables={panel.tables ?? []}
          resultColumns={result?.columns.map((c) => c.name) ?? []}
          navigateHistory={navigateHistory}
        />
        </EditorWorkspace>
        {imagePreview?.tabId === activeTabId && imagePreview.result === result && enabledImageColumns?.has(imagePreview.column) && (
          <ResultImageModal url={imagePreview.url} onClose={() => setImagePreview(null)} />
        )}
        <div className="vg-gridwrap gridwrap" id="grid">
          {loading ? (
            <div className="vg-empty spin">
              <i className="ti ti-loader" /> {t("running")}
            </div>
          ) : gridError ? (
            <div className="vg-err err">{gridError}</div>
          ) : !result ? (
            <div className="vg-empty empty">{t("empty_grid")}</div>
          ) : shownRows.length === 0 && result.columns.length === 0 ? (
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
                      <ResultImageColumnMenu result={result} column={c.name} enabled={!!enabledImageColumns?.has(i)}
                        onChange={(urls) => {
                          const current = useTabsStore.getState().results[activeTabId];
                          if (current?.result !== result) return;
                          const columns = new Map(current.imageColumns);
                          if (urls) columns.set(i, urls);
                          else columns.delete(i);
                          setTabResult(activeTabId, { ...current, imageColumns: columns.size ? columns : undefined });
                        }} />
                      <span className="vg-rz rz" onMouseDown={(e) => startColResize(e, i)} />
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {shownRows.length === 0 && (
                  <tr><td colSpan={result.columns.length + 1} className="vg-empty empty">0 {t("rows")}</td></tr>
                )}
                {shownRows.map((r, ri) => (
                  <tr key={ri}>
                    <td
                      className="rownum"
                      data-ri={ri}
                      title={t("row_detail")}
                      onClick={() => setModal({ type: "row", row: r, columns: result.columns, db: queryDb, env: queryEnv, sql: result.sql })}
                    >
                      {ri + 1}
                    </td>
                    {result.columns.map((c, ci) => {
                      const v = r[c.name];
                      const preview = cellPreview(v);
                      const imageUrl = typeof v === "string" && enabledImageColumns?.get(ci)?.has(v) ? v : undefined;
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
                          {imageUrl ? <ResultImageCell key={imageUrl} url={imageUrl}
                            onOpen={() => setImagePreview({ tabId: activeTabId, result, column: ci, url: imageUrl })} />
                            : preview.text === null ? "NULL" : preview.text}
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
              <span id="statusRows" title={t("rows_tip")}>
                <span className="vg-cu cu">{result.rowCount}</span> {t("rows")}
              </span>
              <span id="statusElapsed" title={t("elapsed_tip")}>
                <i className="ti ti-clock" /> {result.elapsedMs} ms
              </span>
              {result.downloadBytes != null && (
                <>
                  <span
                    id="dlSize"
                    title={
                      result.sizeIsEstimated
                        ? `${t("size_tip")} (${t("size_estimated_tip")})`
                        : t("size_tip")
                    }
                  >
                    <i className="ti ti-download" /> {result.sizeIsEstimated ? "≈" : ""}
                    {formatBytes(result.downloadBytes)}
                  </span>
                  <span
                    id="avgSpeed"
                    title={
                      result.sizeIsEstimated
                        ? `${t("speed_tip")} (${t("size_estimated_tip")})`
                        : t("speed_tip")
                    }
                  >
                    <i className="ti ti-gauge" /> {result.sizeIsEstimated ? "≈" : ""}
                    {formatBytes(result.downloadBytes / ((result.elapsedMs / 1000) || 0.001))}/s
                  </span>
                </>
              )}
              {result.truncated && (
                <span className="vg-tr tr" id="statusTruncated" title={t("truncated_tip")}>
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
                  title={t("load_more_tip")}
                  disabled={loadMoreBusy}
                  onClick={() => void onLoadMore()}
                >
                  {loadMoreBusy ? t("loading_more") : t("load_more")}
                </button>
              )}
              <span style={{ flex: 1 }} />
              <span id="statusTarget" title={t("result_target_tip")}>
                {queryDb && (findItem(queryDb)?.label ?? queryDb)}
                {queryEnv ? `@${displayConnectionEnvironment(queryDb || "", queryEnv)}` : ""} · {result.engine}
              </span>
            </>
          )}
        </div>
        </> : (
          <div className="query-empty-state" id="queryEmptyState">
            <i className="ti ti-file-code" aria-hidden="true" />
            <h2>{t("no_open_queries")}</h2>
            <p>{t("no_open_queries_hint")}</p>
            <div className="query-empty-actions">
              <button className="vg-btn btn" id="emptyNewTab" onClick={() => useTabsStore.getState().addTab()}>{t("new_tab")}</button>
              <button className="vg-btn btn" id="histBtn" title={t("hist")} onClick={openHistory}>{t("hist")}</button>
            </div>
          </div>
        )}
      </section>

      {modal?.type === "cell" && cloudRouteVisible(groups, modal.env) && <CellModal value={modal.value} onClose={() => setModal(null)} />}
      {modal?.type === "row" && cloudRouteVisible(groups, modal.env) && (
        <RowDetailModal key={activeTabId} row={modal.row} columns={modal.columns} db={modal.db} env={modal.env} sql={modal.sql} onClose={() => setModal(null)} />
      )}
      {modal?.type === "explain" && cloudRouteVisible(groups, modal.env) && (
        <ExplainModal
          plan={modal.plan}
          db={modal.db}
          env={modal.env}
          onClose={() => setModal(null)}
        />
      )}
      {historyOpen && (
        <HistoryModal history={history} onRecall={recallHistory} onConversation={resumeConversation} onClose={() => setHistoryOpen(false)} />
      )}
      {paramModal && (
        <ParamModal
          query={paramModal}
          onClose={() => setParamModal(null)}
          onSubmit={(params) => void runSavedQuery(paramModal.queryId ?? paramModal.name, params)}
        />
      )}
      {connInfoOpen && current && (
        <ConnInfoModal
          key={JSON.stringify([current.db, current.env])}
          db={current.db}
          env={current.env}
          cloud={current.source === "cloud"}
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
