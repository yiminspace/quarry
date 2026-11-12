import { useState, useSyncExternalStore } from "react";
import { copy } from "./clip";
import { t, tv } from "./i18n";
import { PLATFORM_MODE, platformJSON, platformSnapshot, refreshPlatform, resourceFor, subscribePlatform, type PlatformResource } from "./platform";
import { useConnStore } from "./store/connStore";
import { toast } from "./store/toastStore";

type Ticket = { ticket: string; engine: string; database?: string | null; user?: string | null; password?: string | null };

/** Hosted-only cloud Local controls inside the connection details dialog.
 * Nothing here runs SQL; every action is an explicit click. */
export default function CloudLocalPanel({ db, env, onChanged }: { db: string; env: string | null; onChanged: () => void }) {
  const platform = useSyncExternalStore(subscribePlatform, platformSnapshot);
  const [busy, setBusy] = useState<string | null>(null);
  const [image, setImage] = useState("");
  const [ticket, setTicket] = useState<Ticket | null>(null);
  if (!PLATFORM_MODE || !platform.cloudLocal) return null;
  let resource: PlatformResource;
  try { resource = resourceFor(db, env); } catch { return null; }
  if (resource.engine !== "postgres" && resource.engine !== "redis") return null;

  const run = async (name: string, action: () => Promise<void>): Promise<void> => {
    setBusy(name);
    try { await action(); }
    catch (error) { toast(String((error as Error).message ?? error), false); }
    finally { setBusy(null); }
  };
  const changed = async (): Promise<void> => {
    await refreshPlatform();
    useConnStore.getState().requestReload();
    onChanged();
  };
  const button = (id: string, icon: string, label: string, action: () => Promise<void>, danger = false) => (
    <button key={id} className="vg-btn btn" data-testid={id} disabled={busy !== null} style={danger ? { color: "var(--red-fg)" } : undefined} onClick={() => void run(id, action)}>
      <i className={`ti ${busy === id ? "ti-loader" : icon}`} /> {label}
    </button>
  );

  if (resource.placement === "remote") {
    const existing = platform.resources.find((item) => item.placement === "cloud" && item.sourceResourceId === resource.id);
    return (
      <div className="vg-ciactions ciactions" data-testid="cloud-local-panel" style={{ flexDirection: "column", alignItems: "stretch", gap: "6px" }}>
        <strong>{t("cloud_local_title")}</strong>
        <span style={{ color: "var(--fg3)", fontSize: "12px" }}>{t("cloud_local_hint")}</span>
        {existing ? (
          <span data-testid="cloud-local-exists">{tv("cloud_local_exists", { name: existing.db })}</span>
        ) : (
          <div style={{ display: "flex", gap: "6px" }}>
            <input
              className="vg-input"
              data-testid="cloud-local-image"
              aria-label={t("cloud_local_image")}
              placeholder={`${t("cloud_local_image")} · ${resource.engine === "redis" ? "redis:7-alpine" : "postgres:16-alpine"}`}
              value={image}
              onChange={(event) => setImage(event.target.value.trim())}
              style={{ flex: 1 }}
            />
            {button("cloud-local-create", "ti-cloud-plus", busy === "cloud-local-create" ? t("cloud_local_creating") : t("cloud_local_create"), async () => {
              await platformJSON("/api/local/up", { sourceResourceId: resource.id, engine: resource.engine, ...(image ? { image } : {}) });
              toast(t("cloud_local_created"), true);
              await changed();
            })}
          </div>
        )}
      </div>
    );
  }

  const server = `${location.origin}${location.pathname.replace(/[^/]*$/, "")}`;
  const port = resource.engine === "redis" ? 6380 : 5433;
  const command = ticket ? `qy local connect ${server} --port ${port} --ticket ${ticket.ticket}` : "";
  const url = ticket
    ? ticket.engine === "redis"
      ? `redis://127.0.0.1:${port}/0`
      : `postgresql://${ticket.user}:${ticket.password}@127.0.0.1:${port}/${ticket.database}`
    : "";
  const line = (label: string, value: string, id: string) => (
    <div className="vg-cirow cirow">
      <span className="vg-cik cik">{label}</span>
      <span className="vg-civ civ" data-testid={id} style={{ wordBreak: "break-all" }}>{value}</span>
      <span className="vg-ciact ciact">
        <button className="vg-iconbtn iconbtn" title={t("copy")} onClick={() => copy(value)}><i className="ti ti-copy" /></button>
      </span>
    </div>
  );
  return (
    <div className="vg-ciactions ciactions" data-testid="cloud-local-panel" style={{ flexDirection: "column", alignItems: "stretch", gap: "6px" }}>
      <strong>{t("cloud_local_title")}{resource.image ? ` · ${resource.image}` : ""}</strong>
      <div style={{ display: "flex", gap: "6px", flexWrap: "wrap" }}>
        {button("cloud-local-connect", "ti-plug-connected", t("cloud_local_connect"), async () => {
          setTicket(await platformJSON<Ticket>("/api/local/ticket", { resourceId: resource.id }));
        })}
        {resource.engine === "postgres" && resource.sourceResourceId && button("cloud-local-sync", "ti-refresh", t("cloud_local_sync"), async () => {
          if (!window.confirm(t("cloud_local_sync_confirm"))) return;
          await platformJSON("/api/schema/import", { resourceId: resource.id, sourceResourceId: resource.sourceResourceId });
          useConnStore.getState().requestReload();
          toast(t("cloud_local_done"), true);
        })}
        {button("cloud-local-start", "ti-player-play", t("cloud_local_start"), async () => {
          await platformJSON("/api/local/up", { engine: resource.engine, name: resource.db });
          toast(t("cloud_local_done"), true);
        })}
        {button("cloud-local-stop", "ti-player-stop", t("cloud_local_stop"), async () => {
          await platformJSON("/api/local/down", { resourceId: resource.id });
          toast(t("cloud_local_done"), true);
        })}
        {button("cloud-local-delete", "ti-trash", t("cloud_local_delete"), async () => {
          if (!window.confirm(t("cloud_local_delete_confirm"))) return;
          await platformJSON("/api/local/down", { resourceId: resource.id, purge: true });
          toast(t("cloud_local_done"), true);
          await changed();
        }, true)}
      </div>
      {ticket && (
        <>
          <span style={{ color: "var(--fg3)", fontSize: "12px" }}>{t("cloud_local_connect_hint")}</span>
          {line(t("cloud_local_command"), command, "cloud-local-command")}
          {line(t("cloud_local_url"), url, "cloud-local-url")}
        </>
      )}
    </div>
  );
}
