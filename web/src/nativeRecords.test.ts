import { describe, expect, it } from "vitest";
import { mergeNativeRecords } from "./nativeRecords";

type T = { id: string; sql: string; db?: string | null; env?: string | null; title?: string | null; visited?: number };
const doc = (tabs: T[], ati = 0, extra: Record<string, string> = {}) => ({
  qy_tabs: JSON.stringify(tabs), qy_ati: String(ati), ...extra,
});
const tabsOf = (values: Record<string, string> | null) => JSON.parse(values!.qy_tabs) as T[];

describe("mergeNativeRecords", () => {
  const t1 = { id: "t1", sql: "select 1", db: "notes", env: "dev", visited: 1 };

  it("adopts the saved document when this window has no changes", () => {
    const base = doc([t1]);
    const remote = doc([t1, { id: "t2", sql: "", db: "auth", env: "prod", visited: 2 }], 1);
    expect(mergeNativeRecords(base, { ...base }, remote)).toEqual(remote);
  });

  it("unions tabs added in both windows and keeps this window's active tab", () => {
    const base = doc([t1]);
    const local = doc([t1, { id: "t2", sql: "select 2", db: "notes", env: "dev", visited: 2 }], 1);
    const remote = doc([t1, { id: "t2", sql: "", db: "auth", env: "prod", visited: 2 }], 1);
    const merged = mergeNativeRecords(base, local, remote)!;
    const tabs = tabsOf(merged);
    expect(tabs.map((tab) => [tab.id, tab.sql, tab.db])).toEqual([
      ["t1", "select 1", "notes"], ["t2", "", "auth"], ["t3", "select 2", "notes"],
    ]);
    expect(tabs[Number(merged.qy_ati)].sql).toBe("select 2");
  });

  it("collapses the same new connection opened in both windows into one tab", () => {
    const base = doc([t1]);
    const fresh = { sql: "", db: "auth", env: "prod", title: null };
    const local = doc([t1, { id: "t2", ...fresh, visited: 2 }], 1);
    const remote = doc([t1, { id: "t5", ...fresh, visited: 3 }], 1);
    const merged = mergeNativeRecords(base, local, remote)!;
    expect(tabsOf(merged).map((tab) => tab.id)).toEqual(["t1", "t5"]);
    expect(merged.qy_ati).toBe("1");
  });

  it("merges edits to different tabs and unions history", () => {
    const t2 = { id: "t2", sql: "select 2", visited: 2 };
    const base = doc([t1, t2], 0, { qy_hist: JSON.stringify([{ sql: "a", ts: 1 }]) });
    const local = doc([{ ...t1, sql: "select 10" }, t2], 0, { qy_hist: JSON.stringify([{ sql: "b", ts: 3 }, { sql: "a", ts: 1 }]) });
    const remote = doc([t1, { ...t2, sql: "select 20" }], 1, { qy_hist: JSON.stringify([{ sql: "c", ts: 2 }, { sql: "a", ts: 1 }]) });
    const merged = mergeNativeRecords(base, local, remote)!;
    expect(tabsOf(merged).map((tab) => tab.sql)).toEqual(["select 10", "select 20"]);
    expect(JSON.parse(merged.qy_hist).map((entry: { sql: string }) => entry.sql)).toEqual(["b", "c", "a"]);
  });

  it("keeps an edited tab that another window closed", () => {
    const t2 = { id: "t2", sql: "select 2" };
    const merged = mergeNativeRecords(doc([t1, t2]), doc([t1, { ...t2, sql: "select 22" }]), doc([t1]))!;
    expect(tabsOf(merged).map((tab) => tab.sql)).toEqual(["select 1", "select 22"]);
    expect(tabsOf(mergeNativeRecords(doc([t1, t2]), doc([t1, t2]), doc([t1]))).map((tab) => tab.id)).toEqual(["t1"]);
  });

  it("reports divergent SQL in the same tab", () => {
    expect(mergeNativeRecords(doc([t1]), doc([{ ...t1, sql: "select 10" }]), doc([{ ...t1, sql: "select 11" }]))).toBeNull();
  });
});
