import { beforeEach, describe, expect, it, vi } from "vitest";
import type { QueryResult } from "../api";
import {
  MAX_PERSISTED_RESULT_BYTES,
  parseMainTable,
  tabTitle,
  useTabsStore,
  type Tab,
  type TabResultSnapshot,
} from "./tabsStore";

function tab(patch: Partial<Tab>): Tab {
  return { id: "t1", title: null, sql: "", db: null, env: null, ...patch };
}

function snapshot(downloadBytes: number, value = "ok"): TabResultSnapshot {
  const result: QueryResult = {
    columns: [{ name: "v", type: "text" }],
    rows: [{ v: value }],
    rowCount: 1,
    truncated: false,
    elapsedMs: 1,
    engine: "postgres",
    sql: "select 1",
    downloadBytes,
    sizeIsEstimated: true,
  };
  return { result, queryDb: "shop", queryEnv: "dev", querySql: "select 1" };
}

describe("parseMainTable", () => {
  it("finds the table in a single-table SELECT", () => {
    expect(parseMainTable("select id, name from mind_trace where id = 1")).toBe("mind_trace");
  });

  it("finds the table in UPDATE/INSERT/DELETE", () => {
    expect(parseMainTable("update mind_attribute set v = 1 where id = 1")).toBe("mind_attribute");
    expect(parseMainTable("insert into mind_attribute (a, b) values (1, 2)")).toBe("mind_attribute");
    expect(parseMainTable("delete from mind_trace where id = 1")).toBe("mind_trace");
  });

  it("returns null for a multi-table JOIN", () => {
    expect(parseMainTable("select a.id from mind_trace a join mind_attribute b on a.id = b.id")).toBeNull();
  });

  it("returns null for a non-DML statement", () => {
    expect(parseMainTable("explain analyze select 1")).toBeNull();
  });

  it("finds the table when it's quoted (mixed-case/reserved names)", () => {
    // Postgres double-quote form, as produced by ResultWorkbench's quoteIdent()
    expect(parseMainTable('select * from "QyCamelZz" limit 5')).toBe("QyCamelZz");
    // schema-qualified, quoted
    expect(parseMainTable('select * from public."QyCamelZz" limit 5')).toBe("public.QyCamelZz");
    expect(parseMainTable("select * from audit.events")).toBe("audit.events");
    // MySQL backtick form
    expect(parseMainTable("select * from `QyCamelZz` limit 5")).toBe("QyCamelZz");
    // escaped quote inside the identifier
    expect(parseMainTable('select * from "Weird""Name" limit 5')).toBe('Weird"Name');
  });
});

describe("tabTitle", () => {
  it("prefers a user-set title over anything else", () => {
    expect(tabTitle(tab({ title: "My tab", sql: "select * from mind_trace", db: "shop", env: "prod" }))).toBe(
      "My tab",
    );
  });

  it("derives the title from the SQL's main table, distinguishing same-connection tabs", () => {
    const t1 = tab({ sql: "select * from mind_trace", db: "shop", env: "prod" });
    const t2 = tab({ sql: "select * from mind_attribute", db: "shop", env: "prod" });
    expect(tabTitle(t1)).toBe("mind_trace");
    expect(tabTitle(t2)).toBe("mind_attribute");
    expect(tabTitle(t1)).not.toBe(tabTitle(t2));
  });

  it("distinguishes tabs querying quoted mixed-case tables", () => {
    const t1 = tab({ sql: 'select * from "QyCamelZz" limit 5', db: "shop", env: "prod" });
    const t2 = tab({ sql: 'select * from "OtherCamel" limit 5', db: "shop", env: "prod" });
    expect(tabTitle(t1)).toBe("QyCamelZz");
    expect(tabTitle(t2)).toBe("OtherCamel");
  });

  it("allows same-table queries to share a title", () => {
    const t1 = tab({ sql: "select id from mind_trace", db: "shop", env: "prod" });
    const t2 = tab({ sql: "select name from mind_trace", db: "shop", env: "prod" });
    expect(tabTitle(t1)).toBe(tabTitle(t2));
  });

  it("falls back to the first SQL words when no single table can be parsed", () => {
    expect(tabTitle(tab({ sql: "explain analyze select 1", db: "shop", env: "prod" }))).toBe("explain analyze");
  });

  it("falls back to db@env when there is no SQL", () => {
    expect(tabTitle(tab({ sql: "  ", db: "shop", env: "prod" }))).toBe("shop@prod");
  });

  it("falls back to the new-query placeholder with neither SQL nor a connection", () => {
    expect(tabTitle(tab({}))).toBe("new query");
  });
});

describe("bounded result persistence", () => {
  beforeEach(() => {
    localStorage.clear();
    useTabsStore.setState({
      tabs: [tab({ id: "t1", db: "shop", env: "dev" })],
      activeId: "t1",
      results: {},
    });
  });

  it("persists a small result but keeps an oversized result session-only", () => {
    useTabsStore.getState().setTabResult("t1", snapshot(100));
    expect(JSON.parse(localStorage.getItem("qy_tabres") ?? "null")[0].res.rows[0].v).toBe("ok");

    useTabsStore.getState().setTabResult("t1", snapshot(MAX_PERSISTED_RESULT_BYTES + 1, "large"));
    expect(JSON.parse(localStorage.getItem("qy_tabres") ?? "null")).toEqual([null]);
    expect(useTabsStore.getState().results.t1.result?.rows[0].v).toBe("large");
  });

  it("does not rewrite unchanged results on SQL input or tab switches", () => {
    useTabsStore.getState().setTabResult("t1", snapshot(100));
    const original = localStorage.setItem.bind(localStorage);
    const writes: string[] = [];
    const spy = vi.spyOn(localStorage, "setItem").mockImplementation((key, value) => {
      writes.push(key);
      original(key, value);
    });
    try {
      useTabsStore.getState().updateActiveTab({ sql: "select typed" });
      useTabsStore.getState().addTab();
      useTabsStore.getState().switchTab("t1");
      expect(writes.filter((key) => key === "qy_tabres")).toEqual([]);
    } finally {
      spy.mockRestore();
    }
  });
});
