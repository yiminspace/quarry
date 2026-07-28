import { create } from "zustand";
import type { ConnGroup, KeepAliveResponse, TablesResponse } from "../api";

export type CurrentConn = {
  db: string;
  env: string | null;
  engine: string;
  isRedis: boolean;
} | null;

export type HealthState = { ok: boolean; error?: string };

type ConnState = {
  loaded: boolean;
  /** Bumped on every completed /api/connections load — lets consumers react
   * once per *fresh* tree (reloadToken alone fires before the refetch lands). */
  loadSeq: number;
  workspace: string;
  workspaces: string[];
  groups: ConnGroup[];
  current: CurrentConn;
  /** Selected table (the sidebar highlight); cleared when custom SQL runs. */
  currentTable: string | null;
  /** db -> last known health, painted as the sidebar dots. */
  health: Record<string, HealthState>;
  /** true while the header button is probing every connection. */
  checking: boolean;
  /** `db@env` -> last table/key list (session cache: instant paint + SWR). */
  tcache: Record<string, TablesResponse>;
  /** Bumped when something outside the workbench (currently: the workspace
   * manager after add/remove) needs `/api/connections` reloaded. */
  reloadToken: number;
  keepAlive: KeepAliveResponse | null;
  setConnMeta: (workspace: string, workspaces: string[], groups: ConnGroup[]) => void;
  setCurrent: (current: CurrentConn) => void;
  setCurrentTable: (table: string | null) => void;
  setHealth: (db: string, ok: boolean, error?: string) => void;
  setChecking: (checking: boolean) => void;
  putTcache: (key: string, data: TablesResponse) => void;
  dropTcache: (key: string) => void;
  requestReload: () => void;
  setKeepAlive: (keepAlive: KeepAliveResponse | null) => void;
};

/** Connection-tree state shared by the header (workspace label, prod badge,
 * conn-info button, health button) and the sidebar (rows, dots, pills). */
export const useConnStore = create<ConnState>((set) => ({
  loaded: false,
  loadSeq: 0,
  workspace: "",
  workspaces: [],
  groups: [],
  current: null,
  currentTable: null,
  health: {},
  checking: false,
  tcache: {},
  reloadToken: 0,
  keepAlive: null,
  setConnMeta: (workspace, workspaces, groups) =>
    set((s) => ({ workspace, workspaces, groups, loaded: true, loadSeq: s.loadSeq + 1 })),
  setCurrent: (current) => set({ current }),
  setCurrentTable: (currentTable) => set({ currentTable }),
  setHealth: (db, ok, error) =>
    set((s) => ({ health: { ...s.health, [db]: { ok, error } } })),
  setChecking: (checking) => set({ checking }),
  putTcache: (key, data) => set((s) => ({ tcache: { ...s.tcache, [key]: data } })),
  dropTcache: (key) =>
    set((s) => {
      const tcache = { ...s.tcache };
      delete tcache[key];
      return { tcache };
    }),
  requestReload: () => set((s) => ({ reloadToken: s.reloadToken + 1 })),
  setKeepAlive: (keepAlive) => set({ keepAlive }),
}));
