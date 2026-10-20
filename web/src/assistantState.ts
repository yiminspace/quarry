import type { Conversation } from "./assistantApi";
import type { Tab } from "./store/tabsStore";

export type SqlBaseline = Pick<Tab, "id" | "sql" | "db" | "env" | "workspace" | "sqlRevision">;
export function sameAssistantContext(a: Pick<Tab, "db" | "env" | "workspace">, b: Pick<Conversation, "db" | "env" | "workspace" | "guiWorkspace" | "connectionAvailable">): boolean {
  return b.connectionAvailable !== false && a.db === b.db && a.env === b.env && (a.workspace ?? null) === (b.guiWorkspace === undefined ? b.workspace ?? null : b.guiWorkspace);
}
export function canApplyAssistantSql(tab: Tab | undefined, baseline: SqlBaseline): boolean {
  return !!tab && tab.id === baseline.id && sameAssistantContext(tab, { ...baseline, workspace: baseline.workspace ?? null }) &&
    tab.sql === baseline.sql && (tab.sqlRevision ?? 0) === (baseline.sqlRevision ?? 0);
}
export function shouldSubmitAssistant(event: { key: string; shiftKey: boolean; isComposing: boolean; keyCode?: number }): boolean {
  return event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode !== 229;
}
