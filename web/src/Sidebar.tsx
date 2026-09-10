import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { fetchColumns, type ColumnsResponse, type ConnItem, type RedisKeyMeta, type SavedQuery } from "./api";
import { t, tv } from "./i18n";
import { useModalEscape } from "./modalStack";
import { groupKey, groupQueriesByDb, itemsInEngineOrder } from "./sidebarLayout";
import { useConnStore } from "./store/connStore";
import { useUiStore } from "./store/uiStore";

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
  onOpenSaved: (name: string) => void;
};

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
            <div className="vg-tname tname vg-knode knode" onClick={() => onToggle(childPath)}>
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
  onFilterChange,
  onTableClick,
  onInspectKey,
  onRefresh,
  visible,
}: Pick<
  SidebarProps,
  "current" | "panel" | "filter" | "onFilterChange" | "onTableClick" | "onInspectKey" | "onRefresh"
> & { visible: boolean }) {
  const currentTable = useConnStore((s) => s.currentTable);
  const selectedRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (visible) selectedRef.current?.scrollIntoView({ block: "nearest" });
  }, [currentTable, panel.tables, visible]);
  const [folded, setFolded] = useState<Set<string>>(new Set());
  const [structTable, setStructTable] = useState<string | null>(null);
  const isRedis = panel.engine === "redis";

  const q = filter.trim().toLowerCase();
  const shownTables = useMemo(
    () => (panel.tables ?? []).filter((tb) => tb.toLowerCase().includes(q)),
    [panel.tables, q],
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
      {panel.error && !loaded ? (
        <div className="vg-empty empty">{panel.error}</div>
      ) : !loaded ? (
        <div className="vg-empty spin" style={{ padding: 8 }}>
          <i className="ti ti-loader" />
        </div>
      ) : (
        <>
          <div className="vg-trow trow">
            <label className="vg-tfilter">
              <i className="ti ti-search" aria-hidden="true" />
              <input
                className="vg-input tsearch"
                placeholder={isRedis ? t("filter_keys") : t("filter_tables")}
                value={filter}
                onChange={(e) => onFilterChange(e.target.value)}
              />
            </label>
            <button className="vg-iconbtn treload" title={t("refresh_list")} onClick={onRefresh}>
              <i className="ti ti-refresh" />
            </button>
          </div>
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
                <div
                  key={tb}
                  className={`vg-tname tname${tb === currentTable ? " on" : ""}`}
                  ref={tb === currentTable ? selectedRef : undefined}
                  data-t={tb}
                  title={`${tb}\n${t("alt_insert")}`}
                  onClick={(e) => onTableClick(tb, e.altKey)}
                  onDoubleClick={() => setStructTable(tb)}
                >
                  <i className="ti ti-table" />
                  {tb}
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
  const { current, panelOpen, onSelect, savedQueries, onOpenSaved } = props;
  const loaded = useConnStore((s) => s.loaded);
  const groups = useConnStore((s) => s.groups);
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
    <aside className="vg-aside" id="side" style={{ width: sidebarWidth }}>
      {!loaded && (
        <div className="vg-empty spin">
          <i className="ti ti-loader" /> {t("loading")}
        </div>
      )}
      {groups.map((g) => {
        const gkey = groupKey(g.ws, g.group);
        const isCollapsed = collapsed.has(gkey);
        const orig = g.ws ? g.ws.split("/").slice(-2).join("/") : "";
        return (
          <div key={gkey}>
            <div
              className="vg-grp grp"
              data-grp
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
                      {item.db}
                      <small className="vg-engine-tag">{item.engine}</small>
                    </div>
                    {isCurrent && (
                      <TablePanel
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
            </div>
          </div>
        );
      })}
      {savedQueries.length > 0 && (
        <div>
          <div
            className="vg-grp grp"
            data-grp
            data-gkey="__saved__"
            onClick={() => toggleCollapsedGroup("__saved__")}
          >
            <i
              className={`ti ${collapsed.has("__saved__") ? "ti-chevron-right" : "ti-chevron-down"}`}
            />{" "}
            {t("saved_queries")}
          </div>
          <div className="gbody" style={{ display: collapsed.has("__saved__") ? "none" : undefined }}>
            {groupQueriesByDb(
              savedQueries,
              groups.flatMap((g) => itemsInEngineOrder(g.items)),
            ).map((section) => (
              <div key={section.db}>
                <div className="vg-qsrc" data-qsrc={section.db}>
                  {section.db}
                </div>
                {section.queries.map((q) => (
                  <div
                    key={q.name}
                    className="vg-tname qname"
                    data-q={q.name}
                    title={q.desc || q.name}
                    onClick={() => onOpenSaved(q.name)}
                  >
                    <i className="ti ti-bookmark" />
                    <span className="vg-qname-label">{q.name}</span>
                    {q.params.length > 0 && (
                      <span className="vg-rbadge rbadge">
                        {q.params.length} {t("params_suffix")}
                      </span>
                    )}
                  </div>
                ))}
              </div>
            ))}
          </div>
        </div>
      )}
    </aside>
  );
}
