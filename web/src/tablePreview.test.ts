import { describe, expect, it } from "vitest";
import { findPreviewTab, focusRestorePlan, previewSql, quoteIdent, tableClickPlan } from "./tablePreview";

describe("previewSql", () => {
  it("emits select-star with no LIMIT", () => {
    expect(previewSql("customers", "postgres")).toBe("select * from customers");
    expect(previewSql("qy_gui_schema.events", "postgres")).toBe("select * from qy_gui_schema.events");
  });

  it("quotes mixed-case and reserved names", () => {
    expect(previewSql("QyCamelZz", "postgres")).toBe('select * from "QyCamelZz"');
    expect(quoteIdent("Order", "mysql")).toBe("`Order`");
  });
});

describe("tableClickPlan", () => {
  const tabs = [
    { id: "t1", sql: "select 123 as draft", db: "shop", env: "dev" },
    { id: "t2", sql: "select * from customers", db: "shop", env: "dev" },
  ];

  it("reuses an existing same-table preview tab", () => {
    expect(tableClickPlan(tabs, "t1", "shop", "dev", "customers", "postgres")).toEqual({
      action: "reuse",
      tabId: "t2",
    });
    expect(findPreviewTab(tabs, "shop", "dev", "customers", "postgres")?.id).toBe("t2");
  });

  it("reuses the empty active tab", () => {
    const empty = [{ id: "t1", sql: "", db: "shop", env: "dev" }];
    expect(tableClickPlan(empty, "t1", "shop", "dev", "orders", "postgres")).toEqual({
      action: "empty",
      tabId: "t1",
    });
  });

  it("opens a new tab when the active tab already has SQL", () => {
    expect(tableClickPlan(tabs, "t1", "shop", "dev", "orders", "postgres")).toEqual({ action: "new" });
  });

  it("does not reuse a preview on another connection", () => {
    expect(tableClickPlan(tabs, "t1", "other", "dev", "customers", "postgres")).toEqual({
      action: "new",
    });
  });
});

describe("focusRestorePlan", () => {
  const dirty = [{ id: "t1", sql: "select 123 as draft", db: "shop", env: "dev" }];

  it("opens a new tab instead of rebinding a nonempty draft", () => {
    expect(focusRestorePlan(dirty, "t1", "testpg", "test", "customers", "postgres")).toEqual({
      action: "new",
    });
  });

  it("keeps a nonempty tab when the focus is the same connection and has no table", () => {
    expect(focusRestorePlan(dirty, "t1", "shop", "dev", null, "postgres")).toEqual({
      action: "same",
      tabId: "t1",
    });
  });

  it("reuses an empty tab for a table focus", () => {
    const empty = [{ id: "t1", sql: "", db: null, env: null }];
    expect(focusRestorePlan(empty, "t1", "testpg", "test", "customers", "postgres")).toEqual({
      action: "empty",
      tabId: "t1",
    });
  });
});
