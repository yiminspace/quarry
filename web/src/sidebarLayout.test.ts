import { describe, expect, it } from "vitest";
import type { ConnItem, SavedQuery } from "./api";
import { groupQueriesByDb, itemsInEngineOrder } from "./sidebarLayout";

function item(db: string, engine: string, keys: string[] = [db]): ConnItem {
  return {
    db,
    is_env_set: keys.length > 1,
    engine,
    envs: keys.map((key) => ({
      env: null,
      key,
      engine,
      region: null,
      ssh: false,
      proxied: false,
    })),
  };
}

function query(name: string, db: string): SavedQuery {
  return { name, db, desc: null, sql: "select 1", params: [] };
}

describe("itemsInEngineOrder", () => {
  it("keeps registration order inside each engine and orders engines postgres→mysql→redis→neptune", () => {
    expect(
      itemsInEngineOrder([
        item("brain_redis", "redis"),
        item("planning", "postgres"),
        item("userly", "mysql"),
        item("matrix_runtime", "postgres"),
        item("graph", "neptune"),
      ]).map((i) => i.db),
    ).toEqual(["planning", "matrix_runtime", "userly", "brain_redis", "graph"]);
  });

  it("passes through a single-engine group without reordering", () => {
    expect(
      itemsInEngineOrder([item("shop", "postgres"), item("blog", "postgres")]).map((i) => i.db),
    ).toEqual(["shop", "blog"]);
  });
});

describe("groupQueriesByDb", () => {
  it("maps connection-key @db values onto the logical db and does not split by env", () => {
    const items = [
      item("matrix_runtime", "postgres", ["west2_matrix_runtime", "tokyo_matrix_runtime"]),
      item("neptune", "neptune", ["west2_neptune_dev"]),
    ];
    const sections = groupQueriesByDb(
      [
        query("session-list", "west2_matrix_runtime"),
        query("session-finder-tokyo", "tokyo_matrix_runtime"),
        query("neptune-list", "west2_neptune_dev"),
      ],
      items,
    );
    expect(sections.map((s) => s.db)).toEqual(["matrix_runtime", "neptune"]);
    expect(sections[0].queries.map((q) => q.name)).toEqual([
      "session-list",
      "session-finder-tokyo",
    ]);
  });

  it("keeps unmatched @db values as their own source group", () => {
    const sections = groupQueriesByDb(
      [query("orphan", "gone_db")],
      [item("testpg", "postgres")],
    );
    expect(sections.map((s) => s.db)).toEqual(["gone_db"]);
  });
});
