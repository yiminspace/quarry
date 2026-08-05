import { useEffect, useMemo, useRef, useState } from "react";
import type { QueryColumn, SavedQuery } from "./api";
import { cellPreview, cellText, MODAL_TEXT_CHUNK_CHARS } from "./cellValue";
import { copy } from "./clip";
import { t } from "./i18n";
import { useModalEscape } from "./modalStack";
import type { HistEntry } from "./useSqlHistory";

type Row = Record<string, unknown>;

/** Backdrop + box wrapper shared by every modal — legacy `.modal > .box`. */
function Modal({
  onClose,
  boxStyle,
  boxId,
  children,
}: {
  onClose: () => void;
  boxStyle?: React.CSSProperties;
  boxId?: string;
  children: React.ReactNode;
}) {
  useModalEscape(onClose);
  return (
    <div className="vg-modal modal" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div className="vg-box box" id={boxId} style={boxStyle}>
        {children}
      </div>
    </div>
  );
}

/* ---- collapsible JSON tree (cell modal), legacy .jt/.jrow markup ---- */

function JsonTree({ value, jsonKey }: { value: unknown; jsonKey?: string | number }) {
  const [open, setOpen] = useState(false);
  const k = jsonKey !== undefined ? (
    <>
      <span className="vg-jk jk">{String(jsonKey)}</span>:{" "}
    </>
  ) : null;
  if (value === null) {
    return (
      <div className="vg-jrow jrow">
        {k}
        <span className="vg-jnull jnull">null</span>
      </div>
    );
  }
  if (Array.isArray(value)) {
    if (!value.length) return <div className="vg-jrow jrow">{k}[]</div>;
    return (
      <details className="vg-jt jt" onToggle={(e) => setOpen(e.currentTarget.open)}>
        <summary>
          {k}
          <span className="vg-jm jm">[{value.length}]</span>
        </summary>
        {open && value.map((x, i) => <JsonTree key={i} value={x} jsonKey={i} />)}
      </details>
    );
  }
  if (typeof value === "object") {
    return (
      <details className="vg-jt jt" onToggle={(e) => setOpen(e.currentTarget.open)}>
        <summary>
          {k}
          <span className="vg-jm jm">{"{…}"}</span>
        </summary>
        {open &&
          Object.keys(value as object).map((kk) => (
            <JsonTree key={kk} value={(value as Row)[kk]} jsonKey={kk} />
          ))}
      </details>
    );
  }
  const cls =
    typeof value === "number"
      ? "vg-jnum jnum"
      : typeof value === "boolean"
        ? "vg-jbool jbool"
        : "vg-jstr jstr";
  return (
    <div className="vg-jrow jrow">
      {k}
      <span className={cls}>{cellPreview(value, 2_000).text}</span>
    </div>
  );
}

/** Cell-value modal: JSON renders as a collapsible tree, anything else as
 * preformatted text; the header offers a one-click Copy. */
export function CellModal({ value, onClose }: { value: unknown; onClose: () => void }) {
  const [visibleChars, setVisibleChars] = useState(MODAL_TEXT_CHUNK_CHARS);
  const [parsed, setParsed] = useState<unknown>(() => {
    if (value !== null && typeof value === "object") return value;
    if (typeof value !== "string" || value.length > MODAL_TEXT_CHUNK_CHARS || !/^\s*[{[]/.test(value)) {
      return null;
    }
    try {
      const candidate = JSON.parse(value);
      return candidate && typeof candidate === "object" ? candidate : null;
    } catch {
      return null;
    }
  });
  const preview = useMemo(() => cellPreview(value, visibleChars), [value, visibleChars]);
  const canParseJson =
    parsed === null && typeof value === "string" && /^\s*[{[]/.test(value);

  const inspectJson = (): void => {
    if (typeof value !== "string") return;
    try {
      const candidate = JSON.parse(value);
      if (candidate && typeof candidate === "object") setParsed(candidate);
    } catch {
      // It only looked like JSON. Keep the bounded text preview instead.
    }
  };
  return (
    <Modal onClose={onClose} boxStyle={{ minWidth: "min(560px, 80vw)" }}>
      <div className="vg-mh mh">
        <i className="ti ti-eye" /> {t("cell")}{" "}
        <span
          id="cpy"
          style={{ cursor: "pointer", color: "var(--accent)" }}
          onClick={() => {
            copy(cellText(value) ?? "");
            onClose();
          }}
        >
          {t("copy")}
        </span>
      </div>
      {parsed !== null ? <JsonTree value={parsed} /> : <pre>{preview.text}</pre>}
      {parsed === null && (preview.truncated || canParseJson) && (
        <div className="vg-ciactions ciactions">
          {preview.truncated && (
            <button
              className="vg-btn btn"
              onClick={() => setVisibleChars((n) => n + MODAL_TEXT_CHUNK_CHARS)}
            >
              {t("show_more")}
            </button>
          )}
          {canParseJson && (
            <button className="vg-btn btn" onClick={inspectJson}>
              {t("inspect_json")}
            </button>
          )}
        </div>
      )}
    </Modal>
  );
}

/** Whole-row detail modal (opened from the row-number cell). */
export function RowDetailModal({
  row,
  columns,
  onClose,
}: {
  row: Row;
  columns: QueryColumn[];
  onClose: () => void;
}) {
  return (
    <Modal onClose={onClose} boxStyle={{ width: "60%" }}>
      <div className="vg-mh mh">
        <i className="ti ti-list-details" /> {t("row_detail")}
      </div>
      <table style={{ border: 0, width: "100%" }}>
        <tbody>
          {columns.map((c) => {
            const preview = cellPreview(row[c.name]);
            return (
              <tr key={c.name}>
                <td
                  style={{
                    color: "var(--fg2)",
                    padding: "4px 12px 4px 0",
                    verticalAlign: "top",
                    whiteSpace: "nowrap",
                  }}
                >
                  {c.name}
                  {c.type && (
                    <>
                      {" "}
                      <span className="vg-ty ty" style={{ color: "var(--fg3)" }}>
                        {c.type}
                      </span>
                    </>
                  )}
                </td>
                <td
                  data-truncated={preview.truncated ? "true" : undefined}
                  title={preview.truncated ? t("cell_preview_tip") : undefined}
                  style={{ padding: "4px 0", wordBreak: "break-word", fontFamily: "var(--mono)" }}
                >
                  {preview.text === null ? (
                    <span style={{ color: "var(--null)", fontStyle: "italic" }}>NULL</span>
                  ) : (
                    preview.text
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </Modal>
  );
}

/** EXPLAIN plan modal (single-column plans; tabular plans go to the grid). */
export function ExplainModal({
  plan,
  db,
  env,
  onClose,
}: {
  plan: string;
  db: string;
  env: string | null;
  onClose: () => void;
}) {
  return (
    <Modal onClose={onClose} boxStyle={{ minWidth: "min(760px, 85vw)" }}>
      <div className="vg-mh mh">
        <i className="ti ti-route" /> EXPLAIN · {db}
        {env ? `@${env}` : ""}
      </div>
      <pre>{plan}</pre>
    </Modal>
  );
}

function fmtAgo(ts: number): string {
  if (!ts) return "";
  const s = (Date.now() - ts) / 1000;
  return s < 60
    ? t("just_now")
    : s < 3600
      ? Math.floor(s / 60) + t("min_ago")
      : s < 86400
        ? Math.floor(s / 3600) + t("hr_ago")
        : Math.floor(s / 86400) + t("day_ago");
}

/** Query-history modal: search box + recallable entries with db@env / age
 * metadata (legacy `.hsearch/.hitem/.hmeta`). */
export function HistoryModal({
  history,
  onRecall,
  onClose,
}: {
  history: HistEntry[];
  onRecall: (sql: string) => void;
  onClose: () => void;
}) {
  const [q, setQ] = useState("");
  const shown = useMemo(() => {
    const needle = q.trim().toLowerCase();
    if (!needle) return history;
    return history.filter(
      (h) =>
        h.sql.toLowerCase().includes(needle) || (h.db ?? "").toLowerCase().includes(needle),
    );
  }, [history, q]);

  return (
    <Modal onClose={onClose} boxStyle={{ width: "min(680px, 80%)" }}>
      <div className="vg-mh mh">
        <i className="ti ti-history" /> {t("hist_title")} · {history.length}
      </div>
      <input
        className="vg-input hsearch"
        autoFocus
        placeholder={t("hist_search")}
        value={q}
        onChange={(e) => setQ(e.target.value)}
      />
      <div id="hlist">
        {shown.length === 0 && <div className="vg-empty empty">{t("no_match")}</div>}
        {shown.map((h, i) => {
          const meta = [h.db ? h.db + (h.env ? `@${h.env}` : "") : "", fmtAgo(h.ts)]
            .filter(Boolean)
            .join(" · ");
          return (
            <div
              key={i}
              className="vg-hitem hitem"
              style={{ cursor: "pointer", padding: "7px 6px", borderBottom: "1px solid var(--line)" }}
              onClick={() => onRecall(h.sql)}
            >
              <pre
                style={{
                  margin: 0,
                  fontFamily: "var(--mono)",
                  fontSize: "12.5px",
                  whiteSpace: "pre-wrap",
                  wordBreak: "break-word",
                }}
              >
                {h.sql}
              </pre>
              {meta && <div className="vg-hmeta hmeta">{meta}</div>}
            </div>
          );
        })}
      </div>
    </Modal>
  );
}

/** Saved-query parameter modal: required/default hints, Enter submits,
 * click-out closes (legacy `.pf` fields + `#pgo`). */
export function ParamModal({
  query,
  onClose,
  onSubmit,
}: {
  query: SavedQuery;
  onClose: () => void;
  onSubmit: (params: Record<string, string>) => void;
}) {
  const boxRef = useRef<HTMLDivElement | null>(null);

  const submit = (): void => {
    const params: Record<string, string> = {};
    boxRef.current?.querySelectorAll<HTMLInputElement>(".pf").forEach((el) => {
      if (el.value !== "") params[el.dataset.p as string] = el.value;
    });
    onClose();
    onSubmit(params);
  };

  useModalEscape(onClose);
  useEffect(() => {
    const first = boxRef.current?.querySelector<HTMLInputElement>(".pf");
    first?.focus();
    first?.select();
  }, []);

  return (
    <div
      className="vg-modal modal"
      onClick={(e) => e.target === e.currentTarget && onClose()}
      onKeyDown={(e) => e.key === "Enter" && submit()}
    >
      <div className="vg-box box" ref={boxRef} style={{ width: "min(460px, 80%)" }}>
        <div className="vg-mh mh">
          <i className="ti ti-adjustments" /> {query.name} · {t("fill_params")}
        </div>
        {query.desc && (
          <div style={{ color: "var(--fg3)", fontSize: "11.5px", marginBottom: 6 }}>
            {query.desc}
          </div>
        )}
        {query.params.map((p) => (
          <div key={p.name} style={{ margin: "8px 0" }}>
            <label
              style={{
                fontSize: 12,
                color: "var(--fg2)",
                display: "block",
                marginBottom: 3,
              }}
            >
              {p.name} <span style={{ color: "var(--fg3)" }}>{p.type || "text"}</span>
              {p.required ? (
                <span style={{ color: "var(--red-fg)" }}> {t("required")}</span>
              ) : p.default != null ? (
                <span style={{ color: "var(--fg3)" }}>
                  {" "}
                  {t("default_v")} {String(p.default)}
                </span>
              ) : null}
            </label>
            <input
              className="vg-input pf"
              data-p={p.name}
              defaultValue={p.default != null ? String(p.default) : ""}
              placeholder={p.name}
              style={{
                width: "100%",
                background: "var(--bg2)",
                border: "1px solid var(--line2)",
                borderRadius: 6,
                color: "var(--fg)",
                padding: "6px 9px",
                fontFamily: "var(--mono)",
                fontSize: "12.5px",
              }}
            />
          </div>
        ))}
        <div style={{ textAlign: "right", marginTop: 12 }}>
          <button className="vg-btn btn primary" id="pgo" onClick={submit}>
            <i className="ti ti-player-play" /> {t("run")}
          </button>
        </div>
      </div>
    </div>
  );
}
