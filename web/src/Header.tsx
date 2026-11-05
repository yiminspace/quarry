import PlatformStatus from "./PlatformStatus";
import { PLATFORM_MODE } from "./platform";
import { VoyageToolbar } from "@yiminlab/voyage/react";
import { useEffect, useRef, useState } from "react";
import { fetchHealth, keepAliveDown, keepAliveUp } from "./api";
import { t, LANG, toggleLang } from "./i18n";
import { defaultEnvFor, useConnStore } from "./store/connStore";
import { useUiStore } from "./store/uiStore";
import UpdatePanel from "./UpdatePanel";
import WhatsNewPanel from "./WhatsNewPanel";
import WorkspaceModal from "./WorkspaceModal";

/** The header bar: project links, workspace controls, and appearance settings. */
export default function Header() {
  const headerRef = useRef<HTMLElement>(null);
  // Voyage exposes accessible labels but no title props. Mirror those
  // labels into native hover hints, including mode changes and locale reloads.
  useEffect(() => {
    const header = headerRef.current;
    if (!header) return;
    const syncTitles = () => {
      header.querySelectorAll<HTMLElement>(".vg-lang-switch, .vg-switcher-mode, .vg-switcher-trigger").forEach((button) => {
        button.title = button.getAttribute("aria-label") || "";
      });
    };
    syncTitles();
    const observer = new MutationObserver(syncTitles);
    observer.observe(header, { subtree: true, childList: true, attributes: true, attributeFilter: ["aria-label"] });
    return () => observer.disconnect();
  }, []);

  const workspace = useConnStore((s) => s.workspace);
  const workspaces = useConnStore((s) => s.workspaces);
  const groups = useConnStore((s) => s.groups);
  const checking = useConnStore((s) => s.checking);
  const keepAlive = useConnStore((s) => s.keepAlive);
  const cloudStatus = useConnStore((s) => s.cloudStatus);
  const updateInfo = useUiStore((s) => s.updateInfo);
  const whatsNew = useUiStore((s) => s.whatsNew);
  const setWhatsNew = useUiStore((s) => s.setWhatsNew);
  const [wsOpen, setWsOpen] = useState(false);
  const [updOpen, setUpdOpen] = useState(false);

  const multiWs = workspaces.length > 1;
  const kaState = (() => {
    if (!keepAlive?.keeper?.running) return "down";
    if ((keepAlive.tunnels || []).some((x) => x.state === "reconnecting")) return "reconnecting";
    return "up";
  })();
  const kaLabel = kaState === "up" ? t("ka_up") : kaState === "reconnecting" ? t("ka_reconnecting") : t("ka_down");
  const kaBtn = keepAlive?.keeper?.running ? t("ka_stop") : t("ka_start");

  /** Probe every connection (concurrency-capped at 3 — parallel SSH tunnels
   * skew each other's results) and paint the sidebar dots as results land. */
  const checkHealth = async (): Promise<void> => {
    const { checking: inProgress, setChecking, setHealth } = useConnStore.getState();
    if (inProgress) return;
    setChecking(true);
    try {
      const items = groups.flatMap((g) => g.items);
      let i = 0;
      const worker = async (): Promise<void> => {
        while (i < items.length) {
          const it = items[i++];
          const env = defaultEnvFor(it);
          try {
            const d = await fetchHealth(it.db, env, { fresh: true });
            setHealth(it.db, !!d.ok, d.error);
          } catch (error) {
            setHealth(it.db, false, error instanceof Error ? error.message : String(error));
          }
        }
      };
      await Promise.all(Array.from({ length: 3 }, worker));
    } finally {
      setChecking(false);
    }
  };

  return (
    <header className="vg-header" ref={headerRef}>
      <a
        className="header-brand-link"
        href={LANG === "zh" ? "https://yiminspace.github.io/quarry/zh.html" : "https://yiminspace.github.io/quarry/"}
        target="_blank"
        rel="noreferrer"
      >
        <span className="vg-logo logo" aria-hidden="true">Q</span>
        <span className="vg-brand brand">Quarry</span>
      </a>
      <span className="vg-ws ws" id="ws" title={workspaces.join("\n")}>
        {multiWs ? (
          <>
            <i className="ti ti-stack-2" /> {workspaces.length} workspaces
          </>
        ) : (
          <>
            <i className="ti ti-folder" /> {workspace}
          </>
        )}
      </span>
      <button
        className="vg-iconbtn iconbtn"
        style={{ display: PLATFORM_MODE ? "none" : undefined }}
        id="wsBtn"
        title={t("ws_manage")}
        aria-label={t("ws_manage")}
        onClick={() => setWsOpen(true)}
      >
        <i className="ti ti-settings" />
      </button>
      <span className="vg-sp sp" />
      <PlatformStatus />
      {cloudStatus && <span className={`vg-badge badge ${cloudStatus === "connected" ? "ok" : "err"}`} data-testid="cloud-status" role="status">
        {t(`cloud_${cloudStatus}`)}
      </span>}
      <button
        className={`vg-badge badge keeper-control ${kaState === "up" ? "ok" : kaState === "down" ? "err" : ""}`}
        style={{ display: PLATFORM_MODE ? "none" : undefined }}
        id="kaBadge"
        title={`${kaLabel} · ${kaBtn}`}
        aria-label={`${kaLabel} · ${kaBtn}`}
        aria-pressed={keepAlive?.keeper?.running ?? false}
        onClick={() =>
          (keepAlive?.keeper?.running ? keepAliveDown() : keepAliveUp())
            .then((next) => useConnStore.getState().setKeepAlive(next))
            .catch(() => {})
        }
      >
        <i className={`ti ${kaState === "up" ? "ti-plug-connected" : "ti-plug"}`} /> {kaLabel}
      </button>
      {!PLATFORM_MODE && updateInfo?.available && (
        <button className="vg-badge badge update" id="updateBadge" onClick={() => setUpdOpen(true)}>
          <span className="vg-update-dot update-dot" /> {t("update_available")}
        </button>
      )}
      <button
        className={`vg-iconbtn iconbtn${checking ? " vg-spin spin" : ""}`}
        id="healthBtn"
        title={t("check_health")}
        aria-label={t("check_health")}
        aria-busy={checking}
        disabled={checking}
        onClick={() => void checkHealth()}
      >
        <i className={`ti ${checking ? "ti-loader" : "ti-activity"}`} />
      </button>
      <VoyageToolbar
        locale={LANG}
        onLocaleChange={toggleLang}
        githubHref="https://github.com/yiminspace/quarry"
        icons={{
          moon: <i className="ti ti-moon" />,
          sun: <i className="ti ti-sun" />,
          trigger: <i className="ti ti-palette" />,
        }}
      />
      {wsOpen && <WorkspaceModal onClose={() => setWsOpen(false)} />}
      {updOpen && updateInfo && <UpdatePanel info={updateInfo} onClose={() => setUpdOpen(false)} />}
      {whatsNew && <WhatsNewPanel versions={whatsNew} onClose={() => setWhatsNew(null)} />}
    </header>
  );
}
