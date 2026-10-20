import { describe, expect, it } from "vitest";
import { connectionStarterPlan, NEPTUNE_STARTER_SQL } from "./connectionStarter";

const draft = { id: "t1", sql: "select 1", db: "shop", env: "dev" };

describe("connectionStarterPlan", () => {
  it("does nothing for relational and Redis connections", () => {
    expect(connectionStarterPlan([draft], "t1", "shop", "dev", "postgres")).toEqual({ action: "none" });
    expect(connectionStarterPlan([draft], "t1", "cache", "dev", "redis")).toEqual({ action: "none" });
  });

  it("fills an empty tab for a Neptune connection", () => {
    const empty = { id: "t1", sql: "", db: null, env: null };
    expect(connectionStarterPlan([empty], "t1", "graph", "dev", "neptune")).toEqual({
      action: "empty",
      tabId: "t1",
      sql: NEPTUNE_STARTER_SQL,
    });
  });

  it("opens a new tab rather than overwriting a draft", () => {
    expect(connectionStarterPlan([draft], "t1", "graph", "dev", "neptune")).toEqual({
      action: "new",
      sql: NEPTUNE_STARTER_SQL,
    });
  });

  it("reuses the starter tab for the exact Neptune environment", () => {
    const starter = { id: "t2", sql: NEPTUNE_STARTER_SQL, db: "graph", env: "local" };
    expect(connectionStarterPlan([draft, starter], "t1", "graph", "local", "neptune")).toEqual({
      action: "reuse",
      tabId: "t2",
      sql: NEPTUNE_STARTER_SQL,
    });
  });
});
