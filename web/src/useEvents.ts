import { useEffect } from "react";
import { fetchChangelog, fetchUpdate, fetchVersion } from "./api";
import { t } from "./i18n";
import { useConnStore } from "./store/connStore";
import { toast } from "./store/toastStore";
import { useUiStore } from "./store/uiStore";

function refreshUpdateInfo(): void {
  fetchUpdate()
    .then((u) => useUiStore.getState().setUpdateInfo(u))
    .catch(() => {});
}

// localStorage key for the What's New panel's "already showed this version"
// marker — deliberately separate from qy_theme/qy_lang-style prefs, since it
// tracks the running __version__, not a user preference.
const LAST_SEEN_VERSION_KEY = "qy_last_seen_version";

/** Numeric dot-segment comparison, mirroring gui.py's `_version_gt` (so
 * 0.10.0 > 0.9.0, not a string compare). Never throws on odd input. */
function versionGt(a: string, b: string): boolean {
  const seg = (v: string): number[] => v.split(".").map((s) => parseInt(s, 10) || 0);
  const pa = seg(a);
  const pb = seg(b);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const x = pa[i] ?? 0;
    const y = pb[i] ?? 0;
    if (x !== y) return x > y;
  }
  return false;
}

/** Populate the What's New panel the first time a page load sees a
 * __version__ that differs from the last one recorded in localStorage —
 * with ONLY the changelog entries strictly between lastSeen and the current
 * version (never the full history `/api/changelog` returns). A first-ever
 * run (no recorded version yet) just establishes the baseline silently —
 * nothing to compare a fresh install against, so nothing to show. Marks the
 * new version as seen immediately (not on panel close): the panel becoming
 * visible IS the "viewing" the acceptance criteria means, and a reload right
 * after must not show it again. */
function checkWhatsNew(version: string): void {
  const lastSeen = localStorage.getItem(LAST_SEEN_VERSION_KEY);
  if (lastSeen === null) {
    localStorage.setItem(LAST_SEEN_VERSION_KEY, version);
    return;
  }
  if (lastSeen === version) return;
  fetchChangelog()
    .then((versions) => {
      const between = versions.filter(
        (v) => versionGt(v.version, lastSeen) && !versionGt(v.version, version),
      );
      if (between.length > 0) useUiStore.getState().setWhatsNew(between);
      localStorage.setItem(LAST_SEEN_VERSION_KEY, version);
    })
    .catch(() => {});
}

/** Subscribe to the backend's `/api/events` SSE channel (see the Events
 * contract in `src/quarry/gui.py`). Events are refetch *hints*, never data:
 *
 * - `workspace_changed` → bump `reloadToken` (one bump refetches both the
 *   connection tree and the saved-query list) + a confirmation toast.
 * - `update_available` → refetch `/api/update` so the header badge picks up
 *   the newer PyPI release the backend just found.
 * - EventSource reconnect after an error → the server restarted (or the
 *   network blipped): re-read `/api/version`; a changed version means the
 *   user upgraded Quarry, so raise the "reload page" banner. Data may also
 *   have changed while we were disconnected, so refetch regardless.
 */
export function useEvents(): void {
  useEffect(() => {
    let baseVersion: string | null = null;
    let wasDown = false;
    let closed = false;
    fetchVersion()
      .then((v) => {
        baseVersion = v.version;
        checkWhatsNew(v.version);
      })
      .catch(() => {});
    refreshUpdateInfo();
    const es = new EventSource("/api/events");
    es.onerror = () => {
      wasDown = true;
    };
    es.onopen = () => {
      if (!wasDown || closed) return;
      wasDown = false;
      useConnStore.getState().requestReload();
      fetchVersion()
        .then((v) => {
          if (baseVersion !== null && v.version !== baseVersion)
            useUiStore.getState().setUpgradedTo(v.version);
        })
        .catch(() => {});
    };
    es.onmessage = (m) => {
      let type: string | undefined;
      try {
        type = (JSON.parse(m.data) as { type?: string }).type;
      } catch {
        return; // malformed event — ignore, next one will hint again
      }
      if (type === "workspace_changed") {
        useConnStore.getState().requestReload();
        toast(t("ws_files_changed"), true);
      } else if (type === "update_available") {
        refreshUpdateInfo();
      }
    };
    return () => {
      closed = true;
      es.close();
    };
  }, []);
}
