import { t } from "./i18n";
import { useState, useSyncExternalStore } from "react";
import { hostedConfig, signOut } from "./hostedAuth";
import { NATIVE_MODE, exportNativeRecovery, loadSavedNativeRecords, nativeRecordsError, subscribeNativeRecords } from "./nativeRecords";
import { cloudRouteVisible, federatedRoute, useConnStore } from "./store/connStore";
import { PLATFORM_MODE, platformSnapshot, recoveryDownload, subscribePlatform } from "./platform";

function NativeRecoveryStatus() {
  const error = useSyncExternalStore(subscribeNativeRecords, nativeRecordsError);
  if (!error) return null;
  return <details className="platform-connection">
    <summary className="vg-badge badge keeper-control err" data-testid="native-records-status" aria-label={t("native_records_warning")}>
      <i className="ti ti-alert-triangle" /> {t("native_records_warning")}
    </summary>
    <div className="platform-status" role="region" aria-label={t("native_records_warning")}>
      <span className="platform-error" role="status">{error}</span>
      <div className="platform-actions">
        <button className="vg-btn btn" onClick={() => exportNativeRecovery((env) =>
          federatedRoute(env) && cloudRouteVisible(useConnStore.getState().groups, env))}>{t("platform_export")}</button>
        <button className="vg-btn btn" onClick={() => {
          if (window.confirm(t("native_records_confirm_reload"))) void loadSavedNativeRecords();
        }}>{t("native_records_reload")}</button>
      </div>
    </div>
  </details>;
}

export default function PlatformStatus() {
  const state = useSyncExternalStore(subscribePlatform, platformSnapshot);
  const [signOutError, setSignOutError] = useState(false);
  if (NATIVE_MODE) return <NativeRecoveryStatus />;
  if (!PLATFORM_MODE) return null;
  const connected = state.remote === "connected";
  const persistence = t("platform_browser_records");
  const connection = t("platform_remote_label") + t(connected ? "platform_connected" : "platform_disconnected");
  return <details className="platform-connection">
    <summary className={`vg-badge badge keeper-control ${connected ? "ok" : "err"}`} aria-label={t("platform_connection_status")} title={`${connection} · ${persistence}`}>
      <i className={`ti ${connected ? "ti-plug-connected" : "ti-plug"}`} /> {t(connected ? "platform_badge_connected" : "platform_badge_disconnected")}
    </summary>
    <div className="platform-status" data-testid="platform-status" role="region" aria-label={t("platform_connection_status")}>
      <span data-testid="remote-status">{connection}</span>
      <span data-testid="personal-storage-status">{persistence}</span>
      {hostedConfig().auth.logoutPath && <button className="vg-btn btn" onClick={() => { void signOut().catch(() => setSignOutError(true)); }}>{t("hosted_sign_out")}</button>}
      {signOutError && <span role="alert">{t("hosted_sign_out_failed")}</span>}
      {state.error && <span className="platform-error" role="status">{state.error}</span>}
      {state.storageError && <div className="platform-actions">
        <span className="platform-error" role="status">{state.storageError}</span>
        <button className="vg-btn btn" onClick={recoveryDownload}>{t("platform_export")}</button>
      </div>}
    </div>
  </details>;
}
