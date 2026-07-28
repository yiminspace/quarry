import { VoyageToolbar } from "@yiminlab/voyage/react";
import { useState } from "react";
import { fetchHealth, keepAliveDown, keepAliveUp } from "./api";
import { t, LANG, toggleLang } from "./i18n";
import { useConnStore } from "./store/connStore";
import { useUiStore } from "./store/uiStore";
import UpdatePanel from "./UpdatePanel";
import WhatsNewPanel from "./WhatsNewPanel";
import WorkspaceModal from "./WorkspaceModal";

/** The header bar: brand, workspace label, workspace manager, prod/read-only
 * badges, health-check-all, language and theme toggles — the legacy GUI's
 * `<header>` chrome, same DOM and icons. */
export default function Header() {
  const workspace = useConnStore((s) => s.workspace);
  const workspaces = useConnStore((s) => s.workspaces);
  const groups = useConnStore((s) => s.groups);
  const current = useConnStore((s) => s.current);
  const checking = useConnStore((s) => s.checking);
  const keepAlive = useConnStore((s) => s.keepAlive);
  const updateInfo = useUiStore((s) => s.updateInfo);
  const whatsNew = useUiStore((s) => s.whatsNew);
  const setWhatsNew = useUiStore((s) => s.setWhatsNew);
  const [wsOpen, setWsOpen] = useState(false);
  const [updOpen, setUpdOpen] = useState(false);

  const isProd = (current?.env ?? "").toLowerCase() === "prod";
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
    const { setChecking, setHealth } = useConnStore.getState();
    setChecking(true);
    const items = groups.flatMap((g) => g.items);
    let i = 0;
    const worker = async (): Promise<void> => {
      while (i < items.length) {
        const it = items[i++];
        const env = it.envs.find((e) => e.env === "dev")?.env ?? it.envs[0]?.env ?? "";
        try {
          const d = await fetchHealth(it.db, env, { fresh: true });
          setHealth(it.db, !!d.ok, d.error);
        } catch {
          setHealth(it.db, false);
        }
      }
    };
    await Promise.all(Array.from({ length: 3 }, worker));
    setChecking(false);
  };

  return (
    <header className="vg-header">
      <div className="vg-logo logo">Q</div>
      <span className="vg-brand brand">Quarry</span>
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
        id="wsBtn"
        title={t("ws_manage")}
        aria-label={t("ws_manage")}
        onClick={() => setWsOpen(true)}
      >
        <i className="ti ti-settings" />
      </button>
      <span className="vg-sp sp" />
      <span
        className="vg-badge badge err prod"
        id="prodBadge"
        style={{ display: isProd ? undefined : "none" }}
      >
        <i className="ti ti-alert-triangle" /> prod
      </span>
      <span className="vg-badge badge ok ro" id="roBadge">
        <i className="ti ti-lock" /> {t("ro_badge")}
      </span>
      <span className={`vg-badge badge ${kaState === "up" ? "ok" : kaState === "down" ? "err" : ""}`} id="kaBadge">
        <i className={`ti ${kaState === "up" ? "ti-plug-connected" : "ti-plug"}`} /> {kaLabel}
      </span>
      <button
        className="vg-iconbtn iconbtn"
        id="kaBtn"
        title={kaBtn}
        aria-label={kaBtn}
        onClick={() =>
          (keepAlive?.keeper?.running ? keepAliveDown() : keepAliveUp())
            .then((next) => useConnStore.getState().setKeepAlive(next))
            .catch(() => {})
        }
      >
        <i className={`ti ${keepAlive?.keeper?.running ? "ti-player-stop" : "ti-player-play"}`} />
      </button>
      {updateInfo?.available && (
        <button className="vg-badge badge update" id="updateBadge" onClick={() => setUpdOpen(true)}>
          <span className="vg-update-dot update-dot" /> {t("update_available")}
        </button>
      )}
      <button
        className={`vg-iconbtn iconbtn${checking ? " vg-spin spin" : ""}`}
        id="healthBtn"
        title={t("check_health")}
        aria-label={t("check_health")}
        onClick={() => void checkHealth()}
      >
        <i className={`ti ${checking ? "ti-loader" : "ti-activity"}`} />
      </button>
      <VoyageToolbar
        locale={LANG}
        onLocaleChange={toggleLang}
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
