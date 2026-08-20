import { useCallback, useEffect, useRef, useState } from "react";
import { fetchColumns } from "./api";
import { t } from "./i18n";
import { useUiStore } from "./store/uiStore";

type AcKind = "kw" | "tbl" | "col";
type AcItem = { text: string; kind: AcKind };
type AcRange = { from: number; to: number };

type SqlEditorProps = {
  value: string;
  onChange: (next: string) => void;
  onRun: () => void;
  db: string | null;
  env: string | null;
  isRedis: boolean;
  tables: string[];
  resultColumns: string[];
  navigateHistory: (
    dir: "up" | "down",
    currentValue: string,
    db?: string | null,
    env?: string | null,
  ) => string | null;
};

const AC_KEYWORDS = (
  "select from where and or not in is null order by group having limit offset join left " +
  "right inner outer on as distinct count sum avg min max coalesce with case when then " +
  "else end asc desc between like ilike union all insert update delete set values into"
).split(" ");
const AC_MAX_ITEMS = 12;
const FROM_LIKE_KEYWORD = /^(from|join|into|update)$/i;
const TABLE_DOT_COL = /((?:[A-Za-z_][\w$]*\.)?[A-Za-z_][\w$]*)\.([\w$]*)$/;
const BARE_WORD = /([A-Za-z_][\w$]*)$/;

// The legacy highlighter's keyword set, verbatim.
const KEYWORD_RE =
  /\b(select|from|where|and|or|not|in|is|null|order|by|group|having|limit|offset|join|left|right|inner|outer|on|as|distinct|count|sum|avg|min|max|insert|update|delete|set|values|into|create|drop|alter|table|with|case|when|then|else|end|asc|desc|between|like|ilike|union|all)\b/gi;

const MIRROR_PROPS: Array<[keyof CSSStyleDeclaration, string]> = [
  ["fontFamily", "font-family"],
  ["fontSize", "font-size"],
  ["fontWeight", "font-weight"],
  ["lineHeight", "line-height"],
  ["letterSpacing", "letter-spacing"],
  ["paddingTop", "padding-top"],
  ["paddingRight", "padding-right"],
  ["paddingBottom", "padding-bottom"],
  ["paddingLeft", "padding-left"],
];

function escapeHtml(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

/** Highlights already-escaped SQL by wrapping tokens in span tags. `text` is
 * the user's own draft in this browser session (not remote query-result
 * data); escaping happens first, so no markup from the raw string can leak
 * through the spans added below. */
function highlightSql(text: string): string {
  let h = escapeHtml(text);
  h = h.replace(/(--[^\n]*)/g, '<span class="vg-tok-cm tok-cm">$1</span>');
  h = h.replace(/('(?:[^']|'')*')/g, '<span class="vg-tok-str tok-str">$1</span>');
  h = h.replace(/\b(\d+\.?\d*)\b/g, '<span class="vg-tok-num tok-num">$1</span>');
  h = h.replace(KEYWORD_RE, '<span class="vg-tok-kw tok-kw">$&</span>');
  return h + "\n";
}

/** Pixel position of the caret (for anchoring the autocomplete box): a hidden
 * mirror div with the textarea's text metrics, measured at the caret span. */
function caretXY(ta: HTMLTextAreaElement): { x: number; y: number } {
  const style = getComputedStyle(ta);
  const mirror = document.createElement("div");
  for (const [camel, kebab] of MIRROR_PROPS) {
    mirror.style.setProperty(kebab, style[camel] as string);
  }
  mirror.style.cssText +=
    ";position:absolute;visibility:hidden;box-sizing:border-box;white-space:pre-wrap;word-break:break-word;top:0;left:-9999px";
  mirror.style.width = `${ta.clientWidth}px`;
  mirror.textContent = ta.value.slice(0, ta.selectionStart ?? 0);
  const caret = document.createElement("span");
  caret.textContent = "​";
  mirror.appendChild(caret);
  document.body.appendChild(mirror);
  const x = caret.offsetLeft;
  const y = caret.offsetTop;
  const lineHeight = parseFloat(style.lineHeight) || 16;
  mirror.remove();
  const rect = ta.getBoundingClientRect();
  return { x: rect.left + x - ta.scrollLeft, y: rect.top + y - ta.scrollTop + lineHeight };
}

function dedupCap(items: AcItem[], cap: number): AcItem[] {
  const seen = new Set<string>();
  const out: AcItem[] = [];
  for (const it of items) {
    const key = `${it.kind}:${it.text.toLowerCase()}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(it);
    if (out.length >= cap) break;
  }
  return out;
}

/** The SQL editor: highlight overlay (`#hl`) + transparent textarea (`#sql`),
 * the drag-to-resize bar under it, and the caret-anchored autocomplete box —
 * legacy DOM, ids and token classes throughout. */
export default function SqlEditor({
  value, onChange, onRun, db, env, isRedis, tables, resultColumns, navigateHistory,
}: SqlEditorProps) {
  const taRef = useRef<HTMLTextAreaElement | null>(null);
  const hlRef = useRef<HTMLPreElement | null>(null);
  const pendingCaretRef = useRef<number | null>(null);
  const columnsCacheRef = useRef<Record<string, string[]>>({});
  const inFlightRef = useRef<Set<string>>(new Set());

  const [acItems, setAcItems] = useState<AcItem[]>([]);
  const [acIndex, setAcIndex] = useState(0);
  const [acRange, setAcRange] = useState<AcRange | null>(null);
  const [acPos, setAcPos] = useState<{ x: number; y: number } | null>(null);

  const editorHeight = useUiStore((s) => s.editorHeight);
  const setEditorHeight = useUiStore((s) => s.setEditorHeight);

  const acOpen = acItems.length > 0 && acRange !== null;

  const closeAc = useCallback(() => {
    setAcItems([]);
    setAcRange(null);
    setAcPos(null);
  }, []);

  useEffect(() => {
    if (hlRef.current) hlRef.current.scrollTop = taRef.current?.scrollTop ?? 0;
  }, [value]);

  useEffect(() => {
    if (pendingCaretRef.current == null || !taRef.current) return;
    const pos = pendingCaretRef.current;
    pendingCaretRef.current = null;
    taRef.current.selectionStart = taRef.current.selectionEnd = pos;
  }, [value]);

  const updateSuggestions = useCallback(() => {
    const ta = taRef.current;
    if (!ta || !db || isRedis || document.activeElement !== ta) {
      closeAc();
      return;
    }
    const pos = ta.selectionStart ?? 0;
    const pre = ta.value.slice(0, pos);

    const dotMatch = pre.match(TABLE_DOT_COL);
    if (dotMatch) {
      const table = dotMatch[1];
      const frag = dotMatch[2].toLowerCase();
      const cached = columnsCacheRef.current[table];
      if (cached === undefined && !inFlightRef.current.has(table)) {
        inFlightRef.current.add(table);
        fetchColumns(db, env, table)
          .then((res) => {
            columnsCacheRef.current[table] = res.columns;
          })
          .catch(() => {
            columnsCacheRef.current[table] = [];
          })
          .finally(() => {
            inFlightRef.current.delete(table);
            updateSuggestions();
          });
      }
      const candidates = (cached ?? [])
        .filter((c) => c.toLowerCase().startsWith(frag))
        .map((text): AcItem => ({ text, kind: "col" }));
      if (!candidates.length) {
        closeAc();
        return;
      }
      setAcItems(candidates.slice(0, AC_MAX_ITEMS));
      setAcIndex(0);
      setAcRange({ from: pos - dotMatch[2].length, to: pos });
      setAcPos(caretXY(ta));
      return;
    }

    const wordMatch = pre.match(BARE_WORD);
    if (wordMatch) {
      const frag = wordMatch[1];
      const lf = frag.toLowerCase();
      const prevKwMatch = pre.slice(0, pre.length - frag.length).match(/([A-Za-z_]\w*)\s+$/);
      const prevKw = prevKwMatch?.[1] ?? "";
      const tableItems: AcItem[] = tables
        .filter((t) => t.toLowerCase().startsWith(lf))
        .map((text) => ({ text, kind: "tbl" }));
      let list = tableItems;
      if (!FROM_LIKE_KEYWORD.test(prevKw)) {
        // outside FROM/JOIN position: also offer result columns + keywords
        const colItems: AcItem[] = resultColumns
          .filter((c) => c.toLowerCase().startsWith(lf))
          .map((text) => ({ text, kind: "col" }));
        const kwItems: AcItem[] = AC_KEYWORDS
          .filter((k) => k.startsWith(lf))
          .map((k) => ({ text: k.toUpperCase(), kind: "kw" }));
        list = tableItems.concat(colItems, kwItems);
      }
      const out = dedupCap(list, AC_MAX_ITEMS);
      if (!out.length || (out.length === 1 && out[0].text.toLowerCase() === lf)) {
        closeAc();
        return;
      }
      setAcItems(out);
      setAcIndex(0);
      setAcRange({ from: pos - frag.length, to: pos });
      setAcPos(caretXY(ta));
      return;
    }

    closeAc();
  }, [db, env, isRedis, tables, resultColumns, closeAc]);

  const acAccept = useCallback(
    (index?: number) => {
      if (!acOpen || !acRange) return;
      const item = acItems[index ?? acIndex];
      if (!item) {
        closeAc();
        return;
      }
      const nextValue = value.slice(0, acRange.from) + item.text + value.slice(acRange.to);
      pendingCaretRef.current = acRange.from + item.text.length;
      onChange(nextValue);
      closeAc();
      taRef.current?.focus();
    },
    [acOpen, acRange, acItems, acIndex, value, onChange, closeAc],
  );

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>): void => {
    const meta = e.metaKey || e.ctrlKey;
    // Cmd/Ctrl combos (run, history) always pass through the AC box.
    if (acOpen && !meta) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setAcIndex((i) => (i + 1) % acItems.length);
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setAcIndex((i) => (i - 1 + acItems.length) % acItems.length);
        return;
      }
      if (e.key === "Enter" || e.key === "Tab") {
        e.preventDefault();
        acAccept();
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        closeAc();
        return;
      }
    }
    if (meta && e.key === "Enter") {
      e.preventDefault();
      onRun();
      return;
    }
    if (meta && e.key === "ArrowUp") {
      e.preventDefault();
      const next = navigateHistory("up", value, db, env);
      if (next !== null) onChange(next);
      return;
    }
    if (meta && e.key === "ArrowDown") {
      e.preventDefault();
      const next = navigateHistory("down", value, db, env);
      if (next !== null) onChange(next);
    }
  };

  const startResize = (e: React.MouseEvent): void => {
    e.preventDefault();
    const startY = e.clientY;
    const startHeight = editorHeight;
    const onMove = (moveEvt: MouseEvent): void => {
      setEditorHeight(startHeight + (moveEvt.clientY - startY));
    };
    const onUp = (): void => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  };

  const placeholder = !db ? t("ph_sql_first") : isRedis ? t("ph_redis") : t("ph_sql");

  return (
    <>
      <div className="vg-edwrap edwrap" style={{ height: editorHeight }}>
        <pre
          id="hl"
          aria-hidden="true"
          ref={hlRef}
          dangerouslySetInnerHTML={{ __html: highlightSql(value) }}
        />
        <textarea
          id="sql"
          ref={taRef}
          spellCheck={false}
          placeholder={placeholder}
          value={value}
          onChange={(e) => {
            onChange(e.target.value);
            requestAnimationFrame(updateSuggestions);
          }}
          onScroll={(e) => {
            if (hlRef.current) hlRef.current.scrollTop = e.currentTarget.scrollTop;
            closeAc();
          }}
          onBlur={() => window.setTimeout(closeAc, 120)}
          onKeyDown={onKeyDown}
        />
      </div>
      <div
        className="vg-hresizer hresizer"
        id="edresizer"
        title={t("drag_editor")}
        onMouseDown={startResize}
      />
      {/* Kept mounted with display:none when closed (like the legacy box). */}
      <div
        className="vg-acbox acbox"
        style={
          acOpen && acPos
            ? { display: "block", left: acPos.x, top: acPos.y }
            : { display: "none" }
        }
      >
        {acOpen &&
          acItems.map((it, i) => (
            <div
              key={`${it.kind}:${it.text}`}
              className={`vg-acitem acitem${i === acIndex ? " on" : ""}`}
              onMouseDown={(e) => {
                e.preventDefault();
                acAccept(i);
              }}
            >
              <span className={`vg-ack ack vg-ack-${it.kind} ack-${it.kind}`}>{it.kind}</span>
              {it.text}
            </div>
          ))}
      </div>
    </>
  );
}
