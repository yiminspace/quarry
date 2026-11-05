import { t } from "./i18n";
import { useState, useSyncExternalStore } from "react";
import { hostedConfig, signOut } from "./hostedAuth";
import { PLATFORM_MODE, flushPersonal, loadPersonalFromCompanion, platformSnapshot, recoveryDownload, subscribePlatform } from "./platform";

/** Connection and persistence are automatic; only conflicts require a user choice. */
export default function PlatformStatus() {
  const state = useSyncExternalStore(subscribePlatform, platformSnapshot);
  const [signOutError, setSignOutError] = useState(false);
  if (!PLATFORM_MODE) return null;
  const persistence = state.persistence === "local" ? t("platform_saved") : state.persistence === "saving" ? t("platform_saving") : state.persistence === "conflict" ? t("platform_conflict") : t("platform_recovery");
  const connected = Number(state.remote === "connected") + Number(state.local === "connected");
  const label = t(state.persistence === "conflict" ? "platform_badge_conflict" : connected === 2 ? "platform_badge_connected" : connected === 1 ? "platform_badge_partial" : "platform_badge_disconnected");
  return <details className="platform-connection">
    <summary className={`vg-badge badge keeper-control ${state.persistence === "conflict" || connected === 0 ? "err" : connected === 2 ? "ok" : ""}`} aria-label={t("platform_connection_status")} title={`${t("platform_remote_label")}${state.remote === "connected" ? t("platform_connected") : t("platform_disconnected")} · ${t("platform_local_label")}${state.local === "connected" ? t("platform_connected") : t("platform_disconnected")} · ${persistence}`}>
      <i className={`ti ${connected === 2 ? "ti-plug-connected" : "ti-plug"}`} /> {label}
    </summary>
    <div className="platform-status" data-testid="platform-status" role="region" aria-label={t("platform_connection_status")}>
      <span data-testid="remote-status">{t("platform_remote_label")}{state.remote === "connected" ? t("platform_connected") : t("platform_disconnected")}</span>
      <span data-testid="companion-status">{t("platform_local_label")}{state.local === "connected" ? t("platform_connected") : t("platform_disconnected")}</span>
      <span data-testid="personal-storage-status">{persistence}</span>
      {hostedConfig().auth.logoutPath && <button className="vg-btn btn" onClick={() => { void flushPersonal().then(signOut).catch(() => setSignOutError(true)); }}>{t("hosted_sign_out")}</button>}
      {signOutError && <span role="alert">{t("hosted_sign_out_failed")}</span>}
      {state.error && <span className="platform-error" role="status">{state.error}</span>}
      {(state.persistence === "conflict" || state.error) && <div className="platform-actions">
        <button className="vg-btn btn" onClick={recoveryDownload}>{t("platform_export")}</button>
        {state.persistence === "conflict" && state.local === "connected" && <button className="vg-btn btn" onClick={() => {
          if (window.confirm(t("platform_load_confirm"))) void loadPersonalFromCompanion().catch(() => {});
        }}>{t("platform_load_personal")}</button>}
      </div>}
    </div>
  </details>;
}
