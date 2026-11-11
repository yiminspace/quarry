import type { QueryResult } from "./api";

export type ImageMode = "auto" | "image" | "text";

/** Only URL eligibility is checked here. Browser decoding confirms image content. */
export function resultImageUrl(value: unknown, mode: ImageMode): string | null {
  if (mode === "text" || typeof value !== "string" || value.length > 8192 || !/^https?:\/\//i.test(value.trimStart())) return null;
  try {
    const url = new URL(value);
    if (!["https:", "http:"].includes(url.protocol) || url.username || url.password) return null;
    return value;
  } catch {
    return null;
  }
}

type ResultRow = QueryResult["rows"][number];
const decodedRows = new WeakMap<ResultRow, Set<string>>();

/** Each column publishes its first successful decode to its own header. */
export class ResultImageColumn {
  readonly urls = new WeakMap<ResultRow, string>();
  private confirmed = false;
  private listeners = new Set<() => void>();

  getSnapshot = (): boolean => this.confirmed;

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => { this.listeners.delete(listener); };
  };

  confirm = (row: ResultRow): void => {
    const url = this.urls.get(row);
    if (!url) return;
    let decoded = decodedRows.get(row);
    if (!decoded) {
      decoded = new Set();
      decodedRows.set(row, decoded);
    }
    decoded.add(url);
    if (this.confirmed) return;
    this.confirmed = true;
    this.listeners.forEach((listener) => listener());
  };
}

const resultImages = new WeakMap<QueryResult, Map<string, ResultImageColumn>>();

/** Derived metadata stays outside persisted query values and is reused on tab return. */
export function getResultImageColumns(result: QueryResult): Map<string, ResultImageColumn> {
  const cached = resultImages.get(result);
  if (cached) return cached;
  const columns = new Map<string, ResultImageColumn>();
  const checked = new Map<string, string | null>();
  for (const column of result.columns) {
    const images = new ResultImageColumn();
    columns.set(column.name, images);
    for (const row of result.rows) {
      const value = row[column.name];
      if (typeof value !== "string" || !value) continue;
      if (!checked.has(value)) checked.set(value, resultImageUrl(value, "auto"));
      const url = checked.get(value);
      if (url) {
        images.urls.set(row, url);
        if (decodedRows.get(row)?.has(url)) images.confirm(row);
      }
    }
  }
  resultImages.set(result, columns);
  return columns;
}
