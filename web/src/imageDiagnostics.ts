export function traceImagePreview(event: string, details = {}): void {
  try {
    if (sessionStorage.getItem("qy_debug_images") !== "1") return;
  } catch {
    return;
  }
  console.info("[Quarry images]", new Date().toISOString(), event, JSON.stringify({
    page: performance.timeOrigin,
    visibility: document.visibilityState,
    ...details,
  }));
}

export function observeImagePreviewPage(): () => void {
  const navigation = performance.getEntriesByType("navigation")[0] as PerformanceNavigationTiming | undefined;
  traceImagePreview("page loaded", {
    startedAt: new Date(performance.timeOrigin).toISOString(),
    navigation: navigation?.type,
    discarded: (document as Document & { wasDiscarded?: boolean }).wasDiscarded,
  });
  const visibility = () => traceImagePreview("visibility changed", { state: document.visibilityState });
  const hide = (event: PageTransitionEvent) => traceImagePreview("page hidden", { persisted: event.persisted });
  const show = (event: PageTransitionEvent) => traceImagePreview("page shown", { persisted: event.persisted });
  document.addEventListener("visibilitychange", visibility);
  window.addEventListener("pagehide", hide);
  window.addEventListener("pageshow", show);
  return () => {
    document.removeEventListener("visibilitychange", visibility);
    window.removeEventListener("pagehide", hide);
    window.removeEventListener("pageshow", show);
  };
}
