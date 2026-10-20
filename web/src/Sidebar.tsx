import { createContext, useContext, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { fetchColumns, type ColumnsResponse, type ConnItem, type RedisKeyMeta, type SavedQuery } from "./api";
import { t, tv } from "./i18n";
import { useModalEscape } from "./modalStack";
import { groupKey, groupQueriesByDb, groupsWithQueries, itemsInEngineOrder, queryObjects } from "./sidebarLayout";
import { useConnStore } from "./store/connStore";
import { useUiStore } from "./store/uiStore";
import { useTabsStore } from "./store/tabsStore";

export type PanelData = {
  loading: boolean;
  error: string | null;
  engine: string;
  tables: string[] | null;
  keys: RedisKeyMeta[] | null;
  capped: boolean;
};

export type SidebarProps = {
  current: { db: string; env: string | null; isRedis: boolean } | null;
  panelOpen: boolean;
  panel: PanelData;
  filter: string;
  onFilterChange: (v: string) => void;
  onSelect: (db: string, env: string | null, opts?: { viaPill?: boolean }) => void;
  onTableClick: (table: string, altKey: boolean) => void;
  onInspectKey: (key: string) => void;
  onRefresh: () => void;
  savedQueries: SavedQuery[];
  onOpenSaved: (name: string, preview?: boolean) => void;
  collapseToken: number;
  onCollapseAll: () => void;
  onOpenSearchObject: (db: string, env: string | null, name: string, redis: boolean) => void;
};

const QuerySelection = createContext<string | null>(null);

function QueryItem({ query, onOpen }: { query: SavedQuery; onOpen: () => void }) {
  const selected = useContext(QuerySelection) === (query.queryId ?? JSON.stringify([query.ws ?? null, query.name, query.db]));
  const title = (query.desc || query.name).split(/[，,；;。\n]/)[0];
  return <button className={'vg-tname qname query-item' + (selected ? ' selected' : '')}
    data-q={query.name} title={(query.desc || query.name) + '\n' + query.name}
    aria-pressed={selected} onClick={onOpen}>
    <i className="ti ti-bookmark" />
    <span className="query-copy"><span className="query-title">{title}</span>
      {selected && query.desc && <span className="query-description">{query.desc}</span>}
    </span>
  </button>;
}

function QueryDisclosure({ open, queries, onOpenSaved }: {
  open: boolean; queries: SavedQuery[]; onOpenSaved: SidebarProps["onOpenSaved"];
}) {
  if (!queries.length) return null;
  return <div className="query-disclosure">
    {open && <div className="query-children">{queries.map((query) =>
      <QueryItem key={query.queryId ?? query.name} query={query}
        onOpen={() => onOpenSaved(query.queryId ?? query.name, true)} />)}</div>}
  </div>;
}

export function defaultEnvFor(item: ConnItem): string | null {
  return item.envs.find((e) => e.env === "dev")?.env ?? item.envs[0]?.env ?? null;
}

/* ---- redis key tree (`:`-hierarchy, fold, count/type/ttl badges) ---- */

type RNode = { dirs: Map<string, RNode>; leaves: Array<RedisKeyMeta & { label: string }> };

function buildTree(keys: RedisKeyMeta[]): RNode {
  const root: RNode = { dirs: new Map(), leaves: [] };
  for (const k of keys) {
    const parts = k.key.split(":");
    let node = root;
    for (let i = 0; i < parts.length - 1; i++) {
      const seg = parts[i];
      if (!node.dirs.has(seg)) node.dirs.set(seg, { dirs: new Map(), leaves: [] });
      node = node.dirs.get(seg)!;
    }
    node.leaves.push({ ...k, label: parts[parts.length - 1] || k.key });
  }
  return root;
}

function countNode(n: RNode): number {
  let c = n.leaves.length;
  for (const d of n.dirs.values()) c += countNode(d);
  return c;
}

function fmtTtl(s: number): string {
  return s > 86400
    ? `${Math.round(s / 86400)}d`
    : s > 3600
      ? `${Math.round(s / 3600)}h`
      : s > 60
        ? `${Math.round(s / 60)}m`
        : `${s}s`;
}

function RedisTree({
  node,
  path,
  folded,
  onToggle,
  onInspect,
}: {
  node: RNode;
  path: string;
  folded: Set<string>;
  onToggle: (path: string) => void;
  onInspect: (key: string) => void;
}) {
  return (
    <>
      {[...node.dirs.entries()].map(([name, child]) => {
        const childPath = path ? `${path}:${name}` : name;
        const closed = folded.has(childPath);
        return (
          <div key={childPath}>
            <div className="vg-tname tname vg-knode knode" title={t(closed ? "expand" : "collapse")} onClick={() => onToggle(childPath)}>
              <i className={`ti ${closed ? "ti-chevron-right" : "ti-chevron-down"}`} />
              {name}
              <span className="vg-rbadge rbadge">{countNode(child)}</span>
            </div>
            <div className="vg-kchild kchild" style={{ display: closed ? "none" : undefined }}>
              <RedisTree
                node={child}
                path={childPath}
                folded={folded}
                onToggle={onToggle}
                onInspect={onInspect}
              />
            </div>
          </div>
        );
      })}
      {node.leaves.map((lf) => (
        <div
          key={lf.key}
          className="vg-tname tname"
          data-key={lf.key}
          title={lf.key}
          onClick={() => onInspect(lf.key)}
        >
          <i className="ti ti-key" />
          {lf.label}
          <span className="vg-rbadge rbadge">{lf.type}</span>
          {lf.ttl > 0 && <span className="vg-rbadge rbadge ttl">{fmtTtl(lf.ttl)}</span>}
        </div>
      ))}
    </>
  );
}

/* ---- table-structure modal (issue #11): double-click a table name to see
 * its columns + types without running anything — legacy modal styling. ---- */

function TableStructModal({
  db,
  env,
  table,
  onClose,
}: {
  db: string;
  env: string | null;
  table: string;
  onClose: () => void;
}) {
  const [cols, setCols] = useState<ColumnsResponse | null>(null);
  useModalEscape(onClose);
  useEffect(() => {
    let cancelled = false;
    setCols(null);
    fetchColumns(db, env, table)
      .then((res) => !cancelled && setCols(res))
      .catch(() => !cancelled && setCols({ columns: [], types: {} }));
    return () => {
      cancelled = true;
    };
  }, [db, env, table]);

  return (
    <div className="vg-modal modal" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div className="vg-box box" id="structbox" style={{ width: "min(460px, 80%)" }}>
        <div className="vg-mh mh">
          <i className="ti ti-table" /> {table}
        </div>
        {cols === null && (
          <div className="vg-empty spin">
            <i className="ti ti-loader" />
          </div>
        )}
        {cols !== null && cols.columns.length === 0 && (
          <div className="vg-empty empty">{t("no_tables")}</div>
        )}
        {cols !== null &&
          cols.columns.map((name) => (
            <div className="vg-cirow cirow" key={name}>
              <span className="vg-civ civ">{name}</span>
              <span className="vg-cik cik" style={{ width: "auto", marginLeft: "auto" }}>
                {cols.types[name] ?? ""}
              </span>
            </div>
          ))}
      </div>
    </div>
  );
}

/** One connection's table/redis-key panel, rendered directly under its
 * sidebar row (the legacy `#tbl-panel`). */
function TablePanel({
  current,
  panel,
  filter,
  onTableClick,
  onInspectKey,
  visible,
  savedQueries,
  onOpenSaved,
  collapseToken,
}: Pick<
  SidebarProps,
  "current" | "panel" | "filter" | "onFilterChange" | "onTableClick" | "onInspectKey" | "onRefresh" | "savedQueries" | "onOpenSaved"
> & { visible: boolean; collapseToken: number }) {
  const currentTable = useConnStore((s) => s.currentTable);
  const selectedRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (visible) selectedRef.current?.scrollIntoView({ block: "nearest" });
  }, [currentTable, panel.tables, visible]);
  const [folded, setFolded] = useState<Set<string>>(new Set());
  const [structTable, setStructTable] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const activeId = useTabsStore((s) => s.activeId);
  const selectedQuery = useContext(QuerySelection);
  const isRedis = panel.engine === "redis";
  const objectQueries = new Map<string, SavedQuery[]>();
  for (const query of savedQueries) {
    const objects = queryObjects(query.sql, panel.engine);
    const matches = panel.engine === "neptune" ? objects : (panel.tables ?? []).filter((table) =>
      objects.includes(table) || (!table.includes(".") && objects.includes('public.' + table)) ||
      (table.startsWith('public.') && objects.includes(table.slice(7))));
    for (const object of matches) objectQueries.set(object, [...(objectQueries.get(object) ?? []), query]);
  }
  const revealObjects = JSON.stringify([
    ...(currentTable ? [currentTable] : []),
    ...[...objectQueries].filter(([, qs]) => qs.some((query) =>
      (query.queryId ?? JSON.stringify([query.ws ?? null, query.name, query.db])) === selectedQuery)).map(([name]) => name),
  ]);
  useEffect(() => {
    if (panel.loading || panel.error || (panel.engine !== "neptune" && panel.tables === null)) return;
    setExpanded((previous) => new Set([...previous, ...JSON.parse(revealObjects) as string[]]));
  }, [activeId, revealObjects, panel.loading, panel.error, panel.engine, panel.tables]);
  const previousCollapse = useRef(collapseToken);
  useEffect(() => {
    if (previousCollapse.current === collapseToken) return;
    previousCollapse.current = collapseToken;
    setExpanded(new Set());
    setStructTable(null);
    // Redis namespaces also collapse, without changing the selected key/result.
    const paths = (panel.keys ?? []).flatMap(({ key }) => {
      const parts = key.split(':');
      return parts.slice(0, -1).map((_, index) => parts.slice(0, index + 1).join(':'));
    });
    setFolded(new Set(paths));
  }, [collapseToken, panel.keys]);

  const q = filter.trim().toLowerCase();
  const shownTables = useMemo(
    () => (panel.engine === "neptune"
      ? [...new Set(savedQueries.flatMap((query) => queryObjects(query.sql, "neptune")))]
      : panel.tables ?? []).filter((tb) => tb.toLowerCase().includes(q)),
    [panel.tables, panel.engine, savedQueries, q],
  );
  const shownKeys = useMemo(
    () => (panel.keys ?? []).filter((k) => k.key.toLowerCase().includes(q)),
    [panel.keys, q],
  );
  const tree = useMemo(() => buildTree(shownKeys), [shownKeys]);

  const toggleFold = (path: string): void => {
    setFolded((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  };

  const loaded = isRedis ? panel.keys !== null : panel.tables !== null;

  return (
    <div id="tbl-panel" data-db={current?.db} style={{ display: visible ? undefined : "none" }}>
      {panel.error && loaded && <div className="vg-empty" role="status">{panel.error}</div>}
      {panel.error && !loaded ? (
        <div className="vg-empty empty">{panel.error}</div>
      ) : !loaded ? (
        <div className="vg-empty spin" style={{ padding: 8 }}>
          <i className="ti ti-loader" />
        </div>
      ) : (
        <>
          {panel.capped && (
            <div className="vg-hmeta hmeta" style={{ padding: "0 12px 5px" }}>
              {isRedis
                ? tv("keys_capped", { n: panel.keys?.length ?? 0 })
                : tv("list_capped", { n: panel.tables?.length ?? 0 })}
            </div>
          )}
          {!isRedis &&
            (shownTables.length ? (
              shownTables.map((tb) => (
                <div key={tb} className="table-query-group">
                <div
                  className={`vg-tname tname${tb === currentTable ? " on" : ""}`}
                  ref={tb === currentTable ? selectedRef : undefined}
                  data-t={tb}
                  role="button"
                  tabIndex={0}
                  aria-expanded={expanded.has(tb)}
                  title={`${tb}\n${t("alt_insert")}`}
                  onClick={(e) => { setExpanded((previous) => new Set([...previous, tb])); onTableClick(tb, e.altKey); }}
                  onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setExpanded((previous) => new Set([...previous, tb])); onTableClick(tb, false); } }}
                  onDoubleClick={() => panel.engine !== "neptune" && setStructTable(tb)}
                >
                  <i className="ti ti-table" />
                  {tb}
                </div>
                <QueryDisclosure open={expanded.has(tb)}
                  queries={objectQueries.get(tb) ?? []} onOpenSaved={onOpenSaved} />
                </div>
              ))
            ) : (
              <div className="vg-empty empty">{t("no_tables")}</div>
            ))}
          {isRedis && (
            <div id="ktree">
              {shownKeys.length ? (
                <RedisTree
                  node={tree}
                  path=""
                  folded={folded}
                  onToggle={toggleFold}
                  onInspect={onInspectKey}
                />
              ) : (
                <div className="vg-empty empty">{t("no_keys")}</div>
              )}
            </div>
          )}
        </>
      )}
      {structTable && current && (
        <TableStructModal
          db={current.db}
          env={current.env}
          table={structTable}
          onClose={() => setStructTable(null)}
        />
      )}
    </div>
  );
}

/** The connection sidebar: workspace-grouped rows with health dots,
 * engine-sorted connections, the selected connection's table/key panel,
 * and saved queries — legacy DOM (`.grp/.gbody/.dbrow/.qname`) throughout. */
export default function Sidebar(props: SidebarProps) {
  const { current, panelOpen, onSelect, savedQueries } = props;
  const selectedQuery = useTabsStore((s) => s.tabs.find((tab) => tab.id === s.activeId)?.savedQueryId ?? null);
  const collapseToken = props.collapseToken;
  const [search, setSearch] = useState("");
  const groups = useConnStore((s) => s.groups);
  const tcache = useConnStore((s) => s.tcache);
  const needle = search.trim().toLowerCase();
  const matches = (value: string) => value.toLowerCase().includes(needle);
  const searchGroups = groupsWithQueries(groups, savedQueries).map((group) => {
    const sections = groupQueriesByDb(group.queries, group.items);
    const dbs = [...new Set([...group.items.map(item => item.db), ...sections.map(section => section.db)])];
    return { ...group, results: dbs.map(db => {
      const item = group.items.find(item => item.db === db);
      const queries = (sections.find(section => section.db === db)?.queries ?? []).filter(query =>
        matches(db) || matches(query.name) || matches(query.desc || ""));
      const objects = (item?.envs ?? []).flatMap(env => {
        const data = tcache[`${db}@${env.env || ""}`];
        if (!data) return [];
        const names = 'keys' in data ? data.keys.map(key => key.key) : data.tables;
        return names.filter(name => matches(db) || matches(name)).map(name => ({ name, env: env.env, redis: data.engine === 'redis' }));
      });
      return { db, item, queries, objects };
    }).filter(result => matches(result.db) || result.queries.length || result.objects.length) };
  }).filter(group => group.results.length);
  const onOpenSaved = props.onOpenSaved;
  const loaded = useConnStore((s) => s.loaded);
  const health = useConnStore((s) => s.health);
  const checking = useConnStore((s) => s.checking);
  const sidebarWidth = useUiStore((s) => s.sidebarWidth);
  const collapsed = useUiStore((s) => s.collapsedGroups);
  const toggleCollapsedGroup = useUiStore((s) => s.toggleCollapsedGroup);

  const dotClass = useCallback(
    (db: string): string => {
      const h = health[db];
      if (h === undefined) return checking ? "vg-dot dot chk" : "vg-dot dot";
      return h.ok ? "vg-dot dot ok" : "vg-dot dot down";
    },
    [health, checking],
  );

  return (
    <QuerySelection.Provider value={selectedQuery}>
    <aside className="vg-aside" id="side" style={{ width: sidebarWidth }}>
      <div className="sidebar-searchbar">
        <label className="vg-tfilter">
          <i className="ti ti-search" aria-hidden="true" />
          <input className="vg-input tsearch" aria-label={t("search_sidebar")} placeholder={t("search_sidebar")}
            title={t("search_sidebar_scope")} value={search} onChange={event => setSearch(event.target.value)}
            onKeyDown={event => { if (event.key === 'Escape') setSearch(""); }} />
        </label>
        <button className="vg-iconbtn collapse-all" title={t("collapse_all")} aria-label={t("collapse_all")}
          onClick={() => { setSearch(""); props.onCollapseAll(); }}><i className="ti ti-fold" /></button>
      </div>
      {needle && <div className="sidebar-search-results">
        <div className="search-scope">{t("search_sidebar_scope")}</div>
        {!searchGroups.length && <div className="vg-empty">{t("search_no_matches")}</div>}
        {searchGroups.map(group => <div key={groupKey(group.ws, group.group)}>
          <div className="vg-grp">{group.group || t("other")}</div>
          {group.results.map(result => <div key={result.db}>
            <button className="search-db" onClick={() => { if (result.item) onSelect(result.db, null); }}>{result.db}</button>
            {result.objects.map(object => <button className="search-object" key={object.env + ':' + object.name}
              onClick={() => props.onOpenSearchObject(result.db, object.env, object.name, object.redis)}>
              <i className={'ti ' + (object.redis ? 'ti-key' : 'ti-table')} /> {object.name}
              <small>{object.env}</small>
            </button>)}
            {result.queries.map(query => <QueryItem key={query.queryId ?? query.name} query={query}
              onOpen={() => onOpenSaved(query.queryId ?? query.name, true)} />)}
          </div>)}
        </div>)}
      </div>}
      <div className="sidebar-tree" style={{ display: needle ? 'none' : undefined }}>
      {!loaded && (
        <div className="vg-empty spin">
          <i className="ti ti-loader" /> {t("loading")}
        </div>
      )}
      {groupsWithQueries(groups, savedQueries).map((g) => {
        const gkey = groupKey(g.ws, g.group);
        const isCollapsed = collapsed.has(gkey);
        const orig = g.ws ? g.ws.split("/").slice(-2).join("/") : "";
        return (
          <div key={gkey}>
            <div
              className="vg-grp grp"
              data-grp
              title={t(isCollapsed ? "expand" : "collapse")}
              data-gkey={gkey}
              onClick={() => toggleCollapsedGroup(gkey)}
            >
              <i className={`ti ${isCollapsed ? "ti-chevron-right" : "ti-chevron-down"}`} />{" "}
              {g.group || t("other")}
              {orig && (
                <span className="vg-ws-note wsorig" title={g.ws ?? undefined}>
                  {orig}
                </span>
              )}
            </div>
            <div className="gbody" style={{ display: isCollapsed ? "none" : undefined }}>
              {itemsInEngineOrder(g.items).map((item) => {
                const queries = groupQueriesByDb(g.queries, g.items).find((s) => s.db === item.db)?.queries ?? [];
                const isCurrent = current?.db === item.db;
                const h = health[item.db];
                return (
                  <div key={item.db}>
                    <div
                      className={`vg-row dbrow${item.engine === "redis" ? " redis" : ""}${isCurrent ? " on" : ""}${h?.ok === false ? " down" : ""}`}
                      data-db={item.db}
                      title={h?.ok === false ? h.error || "unreachable" : ""}
                      onClick={() => onSelect(item.db, null)}
                    >
                      <span className={dotClass(item.db)} />
                      <span className="db-name">{item.db}</span>
                      <small className="vg-engine-tag">{item.engine}</small>
                      {isCurrent && <button className="vg-iconbtn treload" title={t("refresh_list")}
                        aria-label={t("refresh_list")} onClick={(event) => {
                          event.stopPropagation();
                          props.onRefresh();
                        }}><i className="ti ti-refresh" /></button>}
                    </div>
                    {isCurrent && (
                      <TablePanel
                        collapseToken={collapseToken}
                        key={gkey + ':' + item.db + ':' + current?.env}
                        savedQueries={queries}
                        onOpenSaved={onOpenSaved}
                        current={current}
                        panel={props.panel}
                        filter={props.filter}
                        onFilterChange={props.onFilterChange}
                        onTableClick={props.onTableClick}
                        onInspectKey={props.onInspectKey}
                        onRefresh={props.onRefresh}
                        visible={panelOpen}
                      />
                    )}
                  </div>
                );
              })}
              {g.queries.length > 0 && (
                <div className="vg-workspace-queries">
                  <div
                    className="query-index-heading"
                    data-grp
                    title={t(collapsed.has(`${gkey}::queries`) ? "expand" : "collapse")}
                    data-gkey={`${gkey}::queries`} data-saved-ws={g.ws ?? ""}
                    onClick={() => toggleCollapsedGroup(`${gkey}::queries`)}
                  >
                    <i className="ti ti-list" aria-hidden="true" />
                    <span>{t("all_queries")}</span>
                  </div>
                  <div className="gbody query-index-children" style={{ display: collapsed.has(`${gkey}::queries`) ? "none" : undefined }}>
                    {groupQueriesByDb(
                      g.queries,
                      itemsInEngineOrder(g.items),
                    ).map((section) => (
                      <div key={section.db}>
                        <div className="vg-qsrc" data-qsrc={section.db}>
                          {section.db}
                        </div>
                        {section.queries.map((q) => (
                          <QueryItem query={q}
                            key={q.queryId ?? q.name}
                            onOpen={() => onOpenSaved(q.queryId ?? q.name)} />
                        ))}
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          </div>
        );
      })}

      </div>
    </aside>
    </QuerySelection.Provider>
  );
}
