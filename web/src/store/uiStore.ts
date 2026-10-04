import { create } from "zustand";
import type { ChangelogVersion, UpdateInfo } from "../api";

// Legacy localStorage keys retain their value formats, so existing preferences
// carry over unchanged. Auto-run adds a separate connection-scoped preference.
// Theme is no longer tracked here — it's voyage's four-axis `vg_prefs`
// (see index.html's bootstrap script and <VoyageProvider>).
const SIDEBAR_WIDTH_KEY = "qy_sw";
const MAX_ROWS_KEY = "qy_maxrows";
const EDITOR_HEIGHT_KEY = "qy_edh";
const COLLAPSED_KEY = "qy_collapsed";
const AUTO_RUN_KEY = "qy_auto_run";

type AutoRunTarget = { workspace: string | null; db: string; env: string | null; production?: boolean };
type AutoRunPreferences = Record<string, boolean>;

function autoRunKey(target: AutoRunTarget): string {
  // Reclassifying an environment as production must reset its default.
  return JSON.stringify([target.workspace, target.db, target.env, target.production]);
}

function readAutoRunPreferences(): AutoRunPreferences {
  try {
    const parsed: unknown = JSON.parse(localStorage.getItem(AUTO_RUN_KEY) || "{}");
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return {};
    return Object.fromEntries(Object.entries(parsed).filter(([, value]) => typeof value === "boolean"));
  } catch {
    return {};
  }
}

export function autoRunEnabled(target: AutoRunTarget | null, preferences: AutoRunPreferences): boolean {
  if (!target || typeof target.production !== "boolean") return false;
  return preferences[autoRunKey(target)] ?? !target.production;
}

export const SIDEBAR_MIN = 150;
export const SIDEBAR_MAX = 480;
export const EDITOR_MIN = 70;
export const MAX_ROWS_OPTIONS = [100, 500, 2000, 5000];

function readNumber(key: string, fallback: number): number {
  const raw = Number(localStorage.getItem(key));
  return Number.isFinite(raw) && raw > 0 ? raw : fallback;
}

function readMaxRows(): number {
  const raw = Number(localStorage.getItem(MAX_ROWS_KEY));
  return MAX_ROWS_OPTIONS.includes(raw) ? raw : 500;
}

function readCollapsedGroups(): Set<string> {
  try {
    const parsed = JSON.parse(localStorage.getItem(COLLAPSED_KEY) || "[]");
    return new Set(Array.isArray(parsed) ? parsed : []);
  } catch {
    return new Set();
  }
}

type UiState = {
  sidebarWidth: number;
  maxRows: number;
  editorHeight: number;
  collapsedGroups: Set<string>;
  autoRunPreferences: AutoRunPreferences;
  /** Set when the backend restarted with a different version than the one
   * this page was loaded against — drives the "reload to upgrade" banner. */
  upgradedTo: string | null;
  /** Last-known `/api/update` result — a newer PyPI release than the one
   * currently running (still-live process, no restart) drives the header's
   * upgrade badge. Refetched on mount and on the `update_available` SSE
   * event; see useEvents.ts. */
  updateInfo: UpdateInfo | null;
  /** Set when the running __version__ differs from the last one this
   * localStorage recorded — the changelog entries between them, for the
   * header's What's New panel (auto-shown once, then dismissed for good
   * until the next real upgrade). Null means "nothing to show". See
   * useEvents.ts for the version-mismatch check that populates this. */
  whatsNew: ChangelogVersion[] | null;
  setSidebarWidth: (n: number) => void;
  setMaxRows: (n: number) => void;
  setEditorHeight: (n: number) => void;
  setAutoRun: (target: AutoRunTarget, enabled: boolean) => void;
  toggleCollapsedGroup: (key: string) => void;
  setUpgradedTo: (v: string | null) => void;
  setUpdateInfo: (v: UpdateInfo | null) => void;
  setWhatsNew: (v: ChangelogVersion[] | null) => void;
};

/** UI preferences (panel sizes, max-rows cap, collapsed sidebar groups and
 * automatic execution), persisted independently. Language is NOT here — it is
 * fixed per page load (see i18n.ts), exactly like the legacy GUI. */
export const useUiStore = create<UiState>((set, get) => {
  return {
    sidebarWidth: readNumber(SIDEBAR_WIDTH_KEY, 244),
    maxRows: readMaxRows(),
    editorHeight: readNumber(EDITOR_HEIGHT_KEY, 154),
    collapsedGroups: readCollapsedGroups(),
    autoRunPreferences: readAutoRunPreferences(),
    setAutoRun: (target, enabled) => {
      const next = { ...get().autoRunPreferences, [autoRunKey(target)]: enabled };
      localStorage.setItem(AUTO_RUN_KEY, JSON.stringify(next));
      set({ autoRunPreferences: next });
    },
    upgradedTo: null,
    updateInfo: null,
    whatsNew: null,
    setUpgradedTo: (v) => set({ upgradedTo: v }),
    setUpdateInfo: (v) => set({ updateInfo: v }),
    setWhatsNew: (v) => set({ whatsNew: v }),
    setSidebarWidth: (n) => {
      const clamped = Math.min(SIDEBAR_MAX, Math.max(SIDEBAR_MIN, n));
      localStorage.setItem(SIDEBAR_WIDTH_KEY, String(clamped));
      set({ sidebarWidth: clamped });
    },
    setMaxRows: (n) => {
      localStorage.setItem(MAX_ROWS_KEY, String(n));
      set({ maxRows: n });
    },
    setEditorHeight: (n) => {
      const clamped = Math.round(
        Math.min(window.innerHeight * 0.7, Math.max(EDITOR_MIN, n)),
      );
      localStorage.setItem(EDITOR_HEIGHT_KEY, String(clamped));
      set({ editorHeight: clamped });
    },
    toggleCollapsedGroup: (key) => {
      const next = new Set(get().collapsedGroups);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      localStorage.setItem(COLLAPSED_KEY, JSON.stringify([...next]));
      set({ collapsedGroups: next });
    },
  };
});
