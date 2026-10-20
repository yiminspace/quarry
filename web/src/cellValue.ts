export const GRID_CELL_PREVIEW_CHARS = 512;
export const MODAL_TEXT_CHUNK_CHARS = 20_000;

export type CellPreview = { text: string | null; truncated: boolean };

function decimalParts(value: unknown): { sign: number; digits: string; power: number } | null {
  if (typeof value !== "number" && typeof value !== "string") return null;
  const match = /^([+-]?)(\d*)(?:\.(\d*))?(?:e([+-]?\d+))?$/i.exec(String(value).trim());
  if (!match || !(match[2] || match[3])) return null;
  const exponent = Number(match[4] || 0);
  if (!Number.isSafeInteger(exponent)) return null;
  const raw = match[2] + (match[3] || "");
  const digits = raw.replace(/^0+/, "");
  if (!digits) return { sign: 0, digits: "0", power: 0 };
  return { sign: match[1] === "-" ? -1 : 1, digits,
    power: match[2].length - (raw.length - digits.length) + exponent };
}

/** Compare lossless decimal/bigint strings without converting to Number. */
export function compareCellValues(a: unknown, b: unknown): number {
  const x = decimalParts(a), y = decimalParts(b);
  if (x && y) {
    if (x.sign !== y.sign) return x.sign - y.sign;
    if (x.sign === 0) return 0;
    if (x.power !== y.power) return (x.power > y.power ? 1 : -1) * x.sign;
    const width = Math.max(x.digits.length, y.digits.length);
    const left = x.digits.padEnd(width, "0"), right = y.digits.padEnd(width, "0");
    return (left === right ? 0 : left > right ? 1 : -1) * x.sign;
  }
  return String(a).localeCompare(String(b));
}

/** Full-fidelity conversion used only by explicit actions (copy/export/open).
 * Grid rendering must use cellPreview() so one multi-megabyte value cannot be
 * copied into the DOM several times. */
export function cellText(value: unknown): string | null {
  if (value === null || value === undefined) return null;
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function boundedJsonPreview(value: object, maxChars: number): CellPreview {
  let out = "";
  let truncated = false;
  const seen = new WeakSet<object>();

  const append = (piece: string): boolean => {
    const room = maxChars - out.length;
    if (room <= 0) {
      truncated = true;
      return false;
    }
    if (piece.length <= room) {
      out += piece;
      return true;
    }
    out += piece.slice(0, room);
    truncated = true;
    return false;
  };

  const write = (item: unknown): void => {
    if (truncated) return;
    if (item === null) {
      append("null");
      return;
    }
    if (typeof item === "string") {
      // Slice before JSON.stringify so escaping a huge nested string remains
      // bounded too. The output is a preview, not a parseable JSON contract.
      const sample = item.slice(0, maxChars - out.length);
      append(JSON.stringify(sample));
      if (sample.length < item.length) truncated = true;
      return;
    }
    if (typeof item !== "object") {
      append(JSON.stringify(item) ?? String(item));
      return;
    }
    if (seen.has(item)) {
      append('"[Circular]"');
      return;
    }
    seen.add(item);

    if (Array.isArray(item)) {
      append("[");
      for (let i = 0; i < item.length && !truncated; i += 1) {
        if (i > 0) append(",");
        write(item[i]);
      }
      if (!truncated) append("]");
    } else {
      append("{");
      let first = true;
      for (const key in item as Record<string, unknown>) {
        if (!Object.prototype.hasOwnProperty.call(item, key)) continue;
        if (!first) append(",");
        first = false;
        write(key);
        append(":");
        write((item as Record<string, unknown>)[key]);
        if (truncated) break;
      }
      if (!truncated) append("}");
    }
    seen.delete(item);
  };

  write(value);
  if (truncated && maxChars > 0) out = out.slice(0, Math.max(0, maxChars - 1)) + "…";
  return { text: out, truncated };
}

/** Bounded cell representation for paint paths. Large objects are traversed
 * only until the preview budget is exhausted; their full JSON is not built. */
export function cellPreview(value: unknown, maxChars = GRID_CELL_PREVIEW_CHARS): CellPreview {
  if (value === null || value === undefined) return { text: null, truncated: false };
  const limit = Math.max(1, Math.trunc(maxChars));
  if (typeof value === "object") return boundedJsonPreview(value, limit);
  const text = String(value);
  if (text.length <= limit) return { text, truncated: false };
  return { text: text.slice(0, Math.max(0, limit - 1)) + "…", truncated: true };
}

export function cellOpensInspector(value: unknown): boolean {
  if (value !== null && typeof value === "object") return true;
  const preview = cellPreview(value, 61);
  return preview.truncated || (preview.text?.length ?? 0) > 60 || /^[{[]/.test(preview.text ?? "");
}
