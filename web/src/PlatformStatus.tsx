import { t, tv } from "./i18n";
import { useState, useSyncExternalStore } from "react";
import { PLATFORM_MODE, flushPersonal, loadPersonalFromCompanion, pairCompanion, platformJSON, platformSnapshot, reconnectLocal, reconnectRemote, recoveryDownload, resourceLabel, subscribePlatform, unpairCompanion } from "./platform";
import { useModalEscape } from "./modalStack";

function ConnectionPanel({ onClose }: { onClose: () => void }) {
  const state = useSyncExternalStore(subscribePlatform, platformSnapshot);
  const [url, setUrl] = useState("http://127.0.0.1:8766");
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [source, setSource] = useState("");
  const [target, setTarget] = useState("");
  useModalEscape(onClose);
  const perform = async (action: () => Promise<unknown>, success: string) => {
    setBusy(true); setNotice(null);
    try { await action(); setNotice(success); }
    catch (error) { setNotice((error as Error).message); }
    finally { setBusy(false); }
  };
  const remote = state.resources.filter((item) => item.placement === "remote");
  const local = state.resources.filter((item) => item.placement === "local");
  return <div className="vg-modal modal" onClick={(event) => event.target === event.currentTarget && onClose()}>
    <section className="vg-box box platform-dialog" role="dialog" aria-modal="true" aria-label={t("platform_pair_title")}>
      <h2>{t("platform_pair_title")}</h2>
      <p>{t("platform_pair_instructions")}</p>
      <form onSubmit={(event) => { event.preventDefault(); void perform(() => pairCompanion(url, code), t("platform_paired_notice")); }}>
        <label>{t("platform_address")}<input aria-label={t("platform_address")} value={url} onChange={(event) => setUrl(event.target.value)} required /></label>
        <label>{t("platform_pair_code")}<input aria-label={t("platform_pair_code")} type="password" value={code} onChange={(event) => setCode(event.target.value)} autoComplete="off" required /></label>
        <div className="platform-actions"><button className="vg-btn btn" disabled={busy || !state.account}>{t("platform_pair")}</button>
          <button className="vg-btn btn" type="button" disabled={busy} onClick={() => void perform(() => reconnectLocal(), t("platform_reconnected_notice"))}>{t("platform_reconnect_local")}</button>
          <button className="vg-btn btn" type="button" disabled={busy || state.local !== "connected"} onClick={() => void perform(() => unpairCompanion(), t("platform_revoked_notice"))}>{t("platform_revoke")}</button></div>
      </form>
      <p>{t("platform_disconnected_help")}</p>
      {state.local === "connected" && <section className="platform-local-actions" aria-label={t("platform_local_management")}>
        <button className="vg-btn btn" disabled={busy} onClick={() => {
          if (window.confirm(t("platform_load_confirm"))) void perform(loadPersonalFromCompanion, t("platform_loading"));
        }}>{t("platform_load_personal")}</button>
        <h3>{t("platform_local_databases")}</h3>
        <label>{t("platform_source")}<select aria-label={t("platform_source")} value={source} onChange={(event) => setSource(event.target.value)}><option value="">{t("platform_select_source")}</option>{remote.map((item) => <option key={item.id} value={item.id}>{item.project}/{item.db} · {resourceLabel(item)}</option>)}</select></label>
        <div className="platform-actions"><button className="vg-btn btn" disabled={busy || !remote.some((item) => item.id === source && ["postgres", "redis", "neptune"].includes(item.engine))} onClick={() => void perform(async () => {
          const item = remote.find((item) => item.id === source)!;
          await platformJSON("/api/local/up", { db: item.db, engine: item.engine, project: item.project, sourceResourceId: item.id }, true);
          await reconnectLocal();
        }, t("platform_created_notice"))}>{t("platform_create_local")}</button></div>
        <p>{t("platform_engines_help")}</p>
        <label>{t("platform_target")}<select aria-label={t("platform_target")} value={target} onChange={(event) => setTarget(event.target.value)}><option value="">{t("platform_select_local")}</option>{local.map((item) => <option key={item.id} value={item.id}>{item.project}/{item.db} · {resourceLabel(item)}</option>)}</select></label>
        <div className="platform-actions"><button className="vg-btn btn" disabled={busy || !remote.some((item) => item.id === source && item.engine === "postgres") || !local.some((item) => item.id === target && item.engine === "postgres")} onClick={() => {
          if (window.confirm(t("platform_sync_confirm"))) void perform(async () => { await platformJSON("/api/schema/import", { sourceResourceId: source, resourceId: target }, true); await reconnectLocal(); }, t("platform_synced_notice"));
        }}>{t("platform_sync")}</button>
        <button className="vg-btn btn" disabled={busy || !target} onClick={() => { const item = local.find((item) => item.id === target)!; if (window.confirm(tv("platform_stop_confirm", { engine: item.engine }))) void perform(async () => { await platformJSON("/api/local/down", { engine: item.engine, resourceId: item.id }, true); await reconnectLocal(); }, t("platform_stopped_notice")); }}>{t("platform_stop")}</button></div>
      </section>}
      {notice && <p role="status">{notice}</p>}
      <div className="platform-actions"><button className="vg-btn btn" onClick={recoveryDownload}>{t("platform_export")}</button><button className="vg-btn btn" onClick={onClose}>{t("platform_close")}</button></div>
    </section>
  </div>;
}
export default function PlatformStatus() {
  const state = useSyncExternalStore(subscribePlatform, platformSnapshot);
  const [open, setOpen] = useState(false);
  if (!PLATFORM_MODE) return null;
  const persistence = state.persistence === "local" ? t("platform_saved") : state.persistence === "saving" ? t("platform_saving") : state.persistence === "conflict" ? t("platform_conflict") : t("platform_recovery");
  return <>
    <div className="platform-status" data-testid="platform-status" role="region" aria-label={t("platform_connection_status")}>
      <a href="/databases">YiminLab</a><span data-testid="remote-status">{t("platform_remote_label")}{state.remote === "connected" ? t("platform_connected") : t("platform_disconnected")}</span>
      <button className="vg-btn btn" onClick={() => void reconnectRemote().catch(() => {})}>{t("platform_reconnect_remote")}</button>
      <span data-testid="companion-status">{t("platform_local_label")}{state.local === "connected" ? t("platform_connected") : t("platform_disconnected")}</span>
      <button className="vg-btn btn" onClick={() => setOpen(true)}>{t("platform_connect_local")}</button>
      <span data-testid="personal-storage-status">{persistence}</span>
      {state.persistence === "saving" && <button className="vg-btn btn" onClick={() => void flushPersonal()}>{t("platform_save_now")}</button>}
      {state.error && <span className="platform-error" role="status">{state.error}</span>}
    </div>
    {open && <ConnectionPanel onClose={() => setOpen(false)} />}
  </>;
}
