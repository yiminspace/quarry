import { describe, expect, it } from "vitest";
import { canApplyAssistantSql, sameAssistantContext, shouldSubmitAssistant } from "./assistantState";
import type { Tab } from "./store/tabsStore";
const tab: Tab = { id: "t1", db: "shop", env: "dev", workspace: "~/shop", title: null, sql: "select 1", sqlRevision: 4 };

describe("assistant SQL ownership", () => {
  it("allows the unchanged origin tab independent of which tab is active", () => {
    expect(canApplyAssistantSql({ ...tab }, tab)).toBe(true);
    expect(canApplyAssistantSql({ ...tab, id: "t2" }, tab)).toBe(false);
  });
  it("rejects edits including edit-and-revert, removed tabs and changed connections", () => {
    expect(canApplyAssistantSql({ ...tab, sql: "select 2" }, tab)).toBe(false);
    expect(canApplyAssistantSql({ ...tab, sqlRevision: 6 }, tab)).toBe(false);
    expect(canApplyAssistantSql(undefined, tab)).toBe(false);
    expect(canApplyAssistantSql({ ...tab, env: "prod" }, tab)).toBe(false);
    expect(canApplyAssistantSql({ ...tab, workspace: "~/another" }, tab)).toBe(false);
  });
  it("uses the server-validated GUI group identity without guessing home paths", () => {
    expect(sameAssistantContext(tab, { db: "shop", env: "dev", workspace: "/Users/test/shop", guiWorkspace: "~/shop" })).toBe(true);
    expect(sameAssistantContext(tab, { db: "shop", env: "dev", workspace: "/Users/test/shop", guiWorkspace: "~/shop", connectionAvailable: false })).toBe(false);
  });
});
describe("assistant composer keyboard", () => {
  it("sends only a plain Enter outside IME composition", () => {
    expect(shouldSubmitAssistant({ key: "Enter", shiftKey: false, isComposing: false })).toBe(true);
    expect(shouldSubmitAssistant({ key: "Enter", shiftKey: true, isComposing: false })).toBe(false);
    expect(shouldSubmitAssistant({ key: "Enter", shiftKey: false, isComposing: true })).toBe(false);
    expect(shouldSubmitAssistant({ key: "Enter", shiftKey: false, isComposing: false, keyCode: 229 })).toBe(false);
  });
});
