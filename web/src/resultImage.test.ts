import { describe, expect, it, vi } from "vitest";
import type { QueryResult } from "./api";
import { getResultImageColumns, resultImageUrl } from "./imageDetection";

describe("result image URL eligibility", () => {
  it.each(["png", "JPG", "jpeg", "webp", "gif", "avif", "bmp", "ico", "svg"])("accepts %s with signed parameters intact", (extension) => {
    const url = `https://images.example/a.${extension}?signature=abc%2Fxyz&expires=123#fragment`;
    expect(resultImageUrl(url, "auto")).toBe(url);
    expect(resultImageUrl(url, "text")).toBeNull();
  });
  it("accepts extensionless URLs for browser decoding", () => {
    expect(resultImageUrl("https://images.example/opaque?token=1", "auto")).toBe("https://images.example/opaque?token=1");
  });
  it("allows manual image mode for otherwise unrecognized URLs", () => {
    const url = "https://images.example/opaque";
    expect(resultImageUrl(url, "auto")).toBe(url);
    expect(resultImageUrl(url, "image")).toBe(url);
    expect(resultImageUrl("https://example.com/?file=a.png", "auto")).toBe("https://example.com/?file=a.png");
  });
  it.each([null, 1, {}, "", "relative.png", "file:///tmp/a.png", "data:image/png;base64,abc", "javascript:alert(1)", "https://user:password@example.com/a.png", `https://example.com/${"a".repeat(8192)}.png`])("rejects unsupported values %s", (value) => {
    expect(resultImageUrl(value, "image")).toBeNull();
  });
});

describe("query result image indexing", () => {
  it("parses each distinct HTTP URL once and reuses row identities across sorting and tab return", () => {
    const NativeURL = URL;
    let parses = 0;
    vi.stubGlobal("URL", class extends NativeURL {
      constructor(value: string, base?: string | URL) {
        super(value, base);
        parses++;
      }
    });
    try {
      const url = "https://images.example/opaque?sig=123";
      const rows = [{ value: null }, { value: "" }, { value: 10 }, { value: "ordinary text" }, { value: url }, { value: url }];
      const result: QueryResult = {
        columns: [{ name: "value", type: "text" }], rows, rowCount: rows.length,
        truncated: false, elapsedMs: 1, engine: "sqlite", sql: "select value",
        downloadBytes: 0, sizeIsEstimated: true,
      };
      const columns = getResultImageColumns(result);
      expect(parses).toBe(1);
      const column = columns.get("value")!;
      expect(column.urls.get(rows[0])).toBeUndefined();
      expect(column.urls.get(rows[4])).toBe(url);
      expect(column.urls.get(rows.slice().reverse()[0])).toBe(url);
      expect(getResultImageColumns(result)).toBe(columns);
      expect(parses).toBe(1);
      const changed = { ...result, rows: [...rows, { value: "https://images.example/new" }] };
      expect(getResultImageColumns(changed)).not.toBe(columns);
      expect(getResultImageColumns(changed).get("value")!.urls.get(changed.rows[6])).toBe("https://images.example/new");
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("notifies only the decoded column once, with confirmation retained on resubscription", () => {
    const result: QueryResult = {
      columns: [{ name: "a", type: "text" }, { name: "b", type: "text" }],
      rows: [{ a: "https://images.example/a", b: "https://images.example/b" }],
      rowCount: 1, truncated: false, elapsedMs: 1, engine: "sqlite", sql: "select a, b",
      downloadBytes: 0, sizeIsEstimated: true,
    };
    const columns = getResultImageColumns(result);
    const a = columns.get("a")!;
    const b = columns.get("b")!;
    const aChanged = vi.fn();
    const bChanged = vi.fn();
    const unsubscribe = a.subscribe(aChanged);
    b.subscribe(bChanged);
    expect(a.getSnapshot()).toBe(false);
    a.confirm(result.rows[0]);
    a.confirm(result.rows[0]);
    expect(aChanged).toHaveBeenCalledTimes(1);
    expect(bChanged).not.toHaveBeenCalled();
    const appended = { ...result, rows: [...result.rows, { a: "https://images.example/new", b: null }] };
    expect(getResultImageColumns(appended).get("a")!.getSnapshot()).toBe(true);
    expect(getResultImageColumns(appended).get("b")!.getSnapshot()).toBe(false);
    unsubscribe();
    expect(getResultImageColumns(result).get("a")!.getSnapshot()).toBe(true);
  });
});
