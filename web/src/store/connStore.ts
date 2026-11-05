import { create } from "zustand";
import type { ConnectionsResponse, ConnEnv, ConnGroup, ConnItem, KeepAliveResponse, TablesResponse } from "../api";
import { t } from "../i18n";
import { displayEnvironment } from "../platform";

export type CurrentConn = {
  db: string;
  env: string | null;
  engine: string;
  isRedis: boolean;
  production?: boolean;
  source?: ConnEnv["source"];
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
  cloudStatus: ConnectionsResponse["cloudStatus"];
  setConnMeta: (workspace: string, workspaces: string[], groups: ConnGroup[], cloudStatus?: ConnectionsResponse["cloudStatus"]) => void;
  setCloudStatus: (status: ConnectionsResponse["cloudStatus"]) => void;
  setCurrent: (current: CurrentConn) => void;
  setCurrentTable: (table: string | null) => void;
  setHealth: (db: string, ok: boolean, error?: string) => void;
  setChecking: (checking: boolean) => void;
  putTcache: (key: string, data: TablesResponse) => void;
  dropTcache: (key: string) => void;
  requestReload: () => void;
  setKeepAlive: (keepAlive: KeepAliveResponse | null) => void;
};

/** Connection-tree state shared by the shell (production context), header
 * (workspace/health), workbench and sidebar (connections and tables). */
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
  cloudStatus: undefined,
  setConnMeta: (workspace, workspaces, groups, cloudStatus) =>
    set((s) => ({ workspace, workspaces, groups, cloudStatus, loaded: true, loadSeq: s.loadSeq + 1 })),
  setCloudStatus: (cloudStatus) => set({ cloudStatus }),
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

export function connectionEnvLabel(env: ConnEnv): string {
  return env.label || (env.source === "cloud" ? env.display_env || t("platform_default") : env.env || "default");
}

export function defaultEnvFor(item: ConnItem): string | null {
  const configured = item.envs.filter((entry) => entry.source !== "cloud");
  const candidates = configured.length ? configured : item.envs;
  const preferred = candidates.find((entry) =>
    (entry.source === "cloud" ? entry.display_env : entry.env) === "dev");
  return (preferred ?? candidates[0])?.env ?? null;
}

/** Keep route IDs in tab/request state and show the environment name to users. */
export function displayConnectionEnvironment(db: string, env: string | null): string {
  if (env?.startsWith("cloud:")) {
    const state = useConnStore.getState();
    const entry = state.groups.flatMap((group) => group.items)
      .find((item) => item.db === db)?.envs.find((entry) => entry.env === env);
    if (entry?.source === "cloud") return `${connectionEnvLabel(entry)} · ${t("cloud_source")}`;
    if (!entry && state.cloudStatus) return t("cloud_target_unavailable");
  }
  return displayEnvironment(db, env);
}
