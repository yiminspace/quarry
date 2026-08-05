import { describe, expect, it } from "vitest";
import {
  cellOpensInspector,
  cellPreview,
  cellText,
  GRID_CELL_PREVIEW_CHARS,
} from "./cellValue";

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
