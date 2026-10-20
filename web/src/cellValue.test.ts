import { describe, expect, it } from "vitest";
import {
  cellOpensInspector,
  cellPreview,
  cellText,
  compareCellValues,
  GRID_CELL_PREVIEW_CHARS,
} from "./cellValue";

describe("lossless numeric comparison", () => {
  it.each([
    ["9007199254740992", "9007199254740993"],
    ["0.123456789012345678901", "0.123456789012345678902"],
    ["-9007199254740993", "-9007199254740992"],
    ["9.99e100", "1e101"],
    ["0", "0.00000000000000001"],
  ])("orders %s before %s without rounding", (a, b) => {
    expect(compareCellValues(a, b)).toBeLessThan(0);
    expect(compareCellValues(b, a)).toBeGreaterThan(0);
  });
  it("treats alternate decimal spellings as equal", () => {
    expect(compareCellValues("01.000", "1e0")).toBe(0);
    expect(compareCellValues("-0", 0)).toBe(0);
  });
});

describe("cellPreview", () => {
  it("keeps short scalar and JSON values unchanged", () => {
    expect(cellPreview("hello")).toEqual({ text: "hello", truncated: false });
    expect(cellPreview({ a: 1, b: [true, null] })).toEqual({
      text: '{"a":1,"b":[true,null]}',
      truncated: false,
    });
  });

  it("bounds multi-megabyte strings without losing the original value", () => {
    const value = "x".repeat(2_000_000);
    const preview = cellPreview(value);
    expect(preview.truncated).toBe(true);
    expect(preview.text).toHaveLength(GRID_CELL_PREVIEW_CHARS);
    expect(preview.text?.endsWith("…")).toBe(true);
    expect(cellText(value)).toHaveLength(2_000_000);
  });

  it("stops traversing a large JSON value at the preview budget", () => {
    const value = { payload: "x".repeat(2_000_000), unreachable: "tail" };
    const preview = cellPreview(value);
    expect(preview.truncated).toBe(true);
    expect(preview.text).toHaveLength(GRID_CELL_PREVIEW_CHARS);
    expect(preview.text).not.toContain("tail");
  });

  it("routes structured and long values to the inspector, but short text to copy", () => {
    expect(cellOpensInspector({ a: 1 })).toBe(true);
    expect(cellOpensInspector("x".repeat(61))).toBe(true);
    expect(cellOpensInspector("copyme")).toBe(false);
  });
});
