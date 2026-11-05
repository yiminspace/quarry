import { PLATFORM_MODE } from "./platform";

/** A previously loaded editor can reopen offline. No API, identity, SQL or result is cached. */
export async function prepareOfflineShell(): Promise<void> {
  if (!PLATFORM_MODE || !("serviceWorker" in navigator)) return;
  const script = document.querySelector<HTMLScriptElement>('script[type="module"][src]');
  const build = script?.src.split("/").pop()?.replace(/[^A-Za-z0-9_.-]/g, "") || "development";
  try {
    const probe = await fetch(`/quarry/quarry-offline.js?build=${encodeURIComponent(build)}`, { cache: "no-store", credentials: "omit", signal: AbortSignal.timeout(2500) });
    if (!probe.ok || !probe.headers.get("content-type")?.includes("javascript")) return;
    const registration = await navigator.serviceWorker.register(`/quarry/quarry-offline.js?build=${encodeURIComponent(build)}`, { scope: "/quarry/" });
    const worker = registration.installing || registration.waiting || registration.active;
    if (!worker) return;
    // ready can still name the old active worker during an application upgrade.
    // Warm the worker we just registered after its new cache policy activates.
    if (worker.state !== "activated") await new Promise<void>((resolve, reject) => {
      const finish = (error?: Error) => {
        clearTimeout(timeout);
        worker.removeEventListener("statechange", changed);
        if (error) reject(error); else resolve();
      };
      const changed = () => {
        if (worker.state === "activated") finish();
        else if (worker.state === "redundant") finish(new Error("Offline worker replaced"));
      };
      const timeout = setTimeout(() => finish(new Error("Offline worker activation timed out")), 5000);
      worker.addEventListener("statechange", changed);
      changed();
    });
    await document.fonts.ready;
    worker.postMessage({ type: "quarry-warm-shell", assets: performance.getEntriesByType("resource").map((entry) => entry.name) });
  } catch { /* The editor and browser recovery remain usable if the browser denies offline caching. */ }
}
