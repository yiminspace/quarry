import { PLATFORM_MODE } from "./platform";

/** A previously loaded editor can reopen offline. No API, identity, SQL or result is cached. */
export async function prepareOfflineShell(): Promise<void> {
  if (!PLATFORM_MODE || !("serviceWorker" in navigator)) return;
  const script = document.querySelector<HTMLScriptElement>('script[type="module"][src]');
  const build = script?.src.split("/").pop()?.replace(/[^A-Za-z0-9_.-]/g, "") || "development";
  try {
    const probe = await fetch(`/quarry/quarry-offline.js?build=${encodeURIComponent(build)}`, { cache: "no-store", credentials: "omit", signal: AbortSignal.timeout(2500) });
    if (!probe.ok || !probe.headers.get("content-type")?.includes("javascript")) return;
    await navigator.serviceWorker.register(`/quarry/quarry-offline.js?build=${encodeURIComponent(build)}`, { scope: "/quarry/" });
    const registration = await navigator.serviceWorker.ready;
    await document.fonts.ready;
    registration.active?.postMessage({ type: "quarry-warm-shell", assets: performance.getEntriesByType("resource").map((entry) => entry.name) });
  } catch { /* The editor and browser recovery remain usable if the browser denies offline caching. */ }
}
