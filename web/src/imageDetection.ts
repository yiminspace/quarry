const IMAGE_SUFFIX = /\.(?:avif|bmp|gif|ico|jpe?g|png|svg|webp)$/i;

/** Identify preview candidates locally. Only a successful decode establishes image content. */
export function resultImageUrl(value: unknown): URL | null {
  if (typeof value !== "string" || value.length > 8192 || !/^https:\/\/[^/\\?#\s]/i.test(value.trimStart())) return null;
  try {
    const url = new URL(value);
    const filename = url.searchParams.get("filename");
    const hasImageSuffix = IMAGE_SUFFIX.test(url.pathname) || (filename !== null && IMAGE_SUFFIX.test(filename));
    return url.protocol === "https:" && url.hostname && !url.username && !url.password && hasImageSuffix ? url : null;
  } catch {
    return null;
  }
}

export function collectColumnImages(rows: Record<string, unknown>[], column: string) {
  const checked = new Set<string>();
  const urls = new Set<string>();
  for (const row of rows) {
    const value = row[column];
    if (typeof value !== "string" || checked.has(value)) continue;
    checked.add(value);
    if (resultImageUrl(value)) urls.add(value);
  }
  return urls;
}
