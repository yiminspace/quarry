import { personalStorage } from "./platform";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

export type HistEntry = { sql: string; db: string | null; env: string | null; ts: number };

// The legacy GUI's own history key and entry format ({sql,db,env,ts}). Keep
// enough entries for a realistically large unbounded tab group to be closed
// without dropping its earliest drafts.
const STORAGE_KEY = "qy_hist";
const MAX_ENTRIES = 1000;

// Pre-{sql,db,env,ts} entries were bare SQL strings; normalize so `.sql` is
// always a string (a bare-string entry would otherwise crash the History
// modal's search the moment the user filters).
function normalizeEntry(h: unknown): HistEntry | null {
  if (typeof h === "string") return h ? { sql: h, db: null, env: null, ts: 0 } : null;
  if (!h || typeof h !== "object") return null;
  const o = h as Partial<HistEntry>;
  if (typeof o.sql !== "string" || !o.sql) return null;
  return { sql: o.sql, db: o.db ?? null, env: o.env ?? null, ts: o.ts ?? 0 };
}

function readHistory(): HistEntry[] {
  try {
    const raw = JSON.parse(personalStorage.getItem(STORAGE_KEY) ?? "[]");
    if (!Array.isArray(raw)) return [];
    return raw.map(normalizeEntry).filter((e): e is HistEntry => e !== null);
  } catch {
    return [];
  }
}

function persist(entries: HistEntry[]): void {
  try {
    personalStorage.setItem(STORAGE_KEY, JSON.stringify(entries));
  } catch {
    // storage full/unavailable — history just won't survive a reload
  }
}

function unshift(prev: HistEntry[], sql: string, db: string | null, env: string | null): HistEntry[] {
  if (prev[0]?.sql === sql) return prev;
  const next = [{ sql, db, env, ts: Date.now() }, ...prev].slice(0, MAX_ENTRIES);
  persist(next);
  return next;
}

/**
 * Owns the SQL history list plus the "never silently lose a hand-written
 * draft" invariant: any site that's about to overwrite the editor (table
 * click, saved query, history recall, …) must call `keepDraft` first so the
 * draft is recoverable from history instead of vanishing.
 *
 * Cmd/Ctrl+Up/Down walks history without touching it (`navigateHistory`);
 * only `pushHist` (called on run, or via `keepDraft`) mutates the list.
 *
 * Subtlety: while mid-navigation (`hiRef` pointing at a recalled entry, not
 * -1), the user's ORIGINAL hand-written draft lives only in `draftRef` — it
 * is neither in `history` nor the `current` value any overwrite site sees.
 * `pushHist` is the single choke point that resets `hiRef` back to -1
 * (whether reached directly, e.g. running the recalled query, or via
 * `keepDraft`, e.g. clicking a table while mid-navigation), so it also
 * rescues that orphaned draft into history first — otherwise it would
 * become silently unreachable the moment `hiRef` resets.
 */
export function useSqlHistory() {
  const [browserHistory, setHistory] = useState<HistEntry[]>(() => readHistory());
  const [executionHistory, setExecutionHistory] = useState<HistEntry[]>([]);
  useEffect(() => {
    if (document.querySelector('meta[name="quarry-mode"]')?.getAttribute("content") !== "native") return;
    const controller = new AbortController();
    async function reload() {
      try {
        const response = await fetch("/api/history", { cache: "no-store", signal: controller.signal });
        if (!response.ok) return;
        const entries: unknown = (await response.json()).history;
        if (Array.isArray(entries)) setExecutionHistory(entries.map(normalizeEntry).filter((entry): entry is HistEntry => entry !== null));
      } catch { /* Local browser drafts remain available. */ }
    }
    void reload();
    window.addEventListener("focus", reload);
    return () => {
      controller.abort();
      window.removeEventListener("focus", reload);
    };
  }, []);
  const history = useMemo(() => {
    if (!executionHistory.length) return browserHistory;
    const seen = new Set<string>();
    return [...browserHistory, ...executionHistory].sort((a, b) => b.ts - a.ts).filter((entry) => {
      const key = JSON.stringify([entry.db, entry.env, entry.sql]);
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    }).slice(0, MAX_ENTRIES);
  }, [browserHistory, executionHistory]);
  const hiRef = useRef(-1);
  const draftRef = useRef("");
  const draftMetaRef = useRef<{ db: string | null; env: string | null }>({ db: null, env: null });

  const pushHist = useCallback((sql: string, db: string | null, env: string | null) => {
    if (hiRef.current !== -1) {
      const orphan = draftRef.current.trim();
      if (orphan && orphan !== sql.trim()) {
        setHistory((prev) => unshift(prev, orphan, draftMetaRef.current.db, draftMetaRef.current.env));
      }
    }
    const trimmed = sql.trim();
    if (trimmed) setHistory((prev) => unshift(prev, trimmed, db, env));
    hiRef.current = -1;
  }, []);

  const keepDraft = useCallback(
    (current: string, next: string, db: string | null, env: string | null) => {
      const s = current.trim();
      if (s && s !== next.trim()) pushHist(s, db, env);
    },
    [pushHist],
  );

  // Walks history in-place without recording it; the in-progress draft is
  // stashed on the way up and restored once navigation returns past the
  // most recent entry (the legacy Cmd/Ctrl+Up/Down behavior).
  const navigateHistory = useCallback(
    (
      dir: "up" | "down",
      currentValue: string,
      db: string | null = null,
      env: string | null = null,
    ): string | null => {
      if (dir === "up") {
        if (hiRef.current >= history.length - 1) return null;
        if (hiRef.current === -1) {
          draftRef.current = currentValue;
          draftMetaRef.current = { db, env };
        }
        hiRef.current += 1;
        return history[hiRef.current]?.sql ?? null;
      }
      if (hiRef.current <= -1) return null;
      if (hiRef.current === 0) {
        hiRef.current = -1;
        return draftRef.current;
      }
      hiRef.current -= 1;
      return history[hiRef.current]?.sql ?? null;
    },
    [history],
  );

  return { history, pushHist, keepDraft, navigateHistory };
}
