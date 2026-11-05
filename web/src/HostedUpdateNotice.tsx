import { useEffect, useState } from "react";
import { t, tv } from "./i18n";

const DISMISSED_RELEASE = "qy_hosted_update_dismissed";
const CHECK_INTERVAL_MS = 30 * 60 * 1000;

type Release = { version: string; notesUrl: string };

export default function HostedUpdateNotice() {
  const [release, setRelease] = useState<Release | null>(null);
  const [dismissed, setDismissed] = useState(() => {
    try { return localStorage.getItem(DISMISSED_RELEASE); } catch { return null; }
  });

  useEffect(() => {
    let checking = false;
    let closed = false;
    const check = async () => {
      if (checking || document.hidden) return;
      checking = true;
      try {
        const response = await fetch("/api/hosted/update", {
          cache: "no-store", credentials: "omit", signal: AbortSignal.timeout(5000),
        });
        if (!response.ok) return;
        const { available, latest, releaseNotesUrl } = await response.json();
        if (closed) return;
        setRelease(available === true && typeof latest === "string"
          && typeof releaseNotesUrl === "string" && releaseNotesUrl.startsWith("https://")
          ? { version: latest, notesUrl: releaseNotesUrl } : null);
      } catch {
        // Release metadata can be unavailable while the workbench is usable.
      } finally {
        checking = false;
      }
    };
    const checkOnFocus = () => { void check(); };
    const syncDismissal = (event: StorageEvent) => {
      if (event.key === DISMISSED_RELEASE) setDismissed(event.newValue);
    };
    checkOnFocus();
    window.addEventListener("focus", checkOnFocus);
    window.addEventListener("storage", syncDismissal);
    const timer = window.setInterval(checkOnFocus, CHECK_INTERVAL_MS);
    return () => {
      closed = true;
      window.removeEventListener("focus", checkOnFocus);
      window.removeEventListener("storage", syncDismissal);
      window.clearInterval(timer);
    };
  }, []);

  return <div aria-live="polite">
    {release && release.version !== dismissed && <section className="hosted-update" data-testid="hosted-update" aria-labelledby="hosted-update-title">
      <strong id="hosted-update-title">{tv("hosted_update_title", { version: release.version })}</strong>
      <a href={release.notesUrl} target="_blank" rel="noopener noreferrer">{t("hosted_update_notes")}</a>
      <button className="vg-iconbtn hosted-update-dismiss" type="button" aria-label={t("hosted_update_dismiss")} title={t("hosted_update_dismiss")} onClick={() => {
        setDismissed(release.version);
        try { localStorage.setItem(DISMISSED_RELEASE, release.version); } catch { /* Kept for this page. */ }
      }}><i className="ti ti-x" aria-hidden="true" /></button>
    </section>}
  </div>;
}
