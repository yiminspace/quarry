import { useEffect, useState } from "react";
import { fetchConnInfo, fetchHealth, localSync, localUp, type ConnInfo } from "./api";
import { copy } from "./clip";
import { t, tv } from "./i18n";
import { useModalEscape } from "./modalStack";
import { useConnStore } from "./store/connStore";
import { toast } from "./store/toastStore";

type Props = {
  db: string;
  env: string | null;
  onClose: () => void;
  /** After `POST /api/local/up` succeeds: reload connections and select the
   * fresh local env (the legacy `loadSide(); selectDb(db,'local')`). */
  onAfterLocalUp: (db: string) => void;
  /** After `POST /api/local/sync` succeeds: invalidate + refresh the local
   * table list (the swapped-in database has different tables). */
  onAfterSync: (db: string) => void;
};

/** Resolved-connection details for the current db@env — what quarry will
 * actually dial and which file that came from — plus a live reachability
 * probe and the local-env create/sync actions. Legacy `openConnInfo()` DOM:
 * `.cirow/.cik/.civ`, `#ciEye/#ciCopy`, `.cihealth`, `.ciactions`.
 *
 * The URL defaults to masked (see `_mask_url` in gui.py) and is only ever
 * fetched in the clear via an explicit reveal/copy click — this must never
 * regress into eagerly fetching the plaintext URL. */
export default function ConnInfoModal({ db, env, onClose, onAfterLocalUp, onAfterSync }: Props) {
  const groups = useConnStore((s) => s.groups);
  const setHealth = useConnStore((s) => s.setHealth);
  const [info, setInfo] = useState<ConnInfo | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [revealed, setRevealed] = useState(false);
  const [displayUrl, setDisplayUrl] = useState<string | null>(null);
  const [probe, setProbe] = useState<{ ok: boolean | null; error?: string }>({ ok: null });
  const [upBusy, setUpBusy] = useState(false);
  const [syncBusy, setSyncBusy] = useState(false);

  useModalEscape(onClose);

  useEffect(() => {
    let cancelled = false;
    fetchConnInfo(db, env)
      .then((d) => {
        if (cancelled) return;
        setInfo(d);
        setDisplayUrl(d.url);
      })
      .catch((e) => !cancelled && setError(String((e as Error).message ?? e)));
    fetchHealth(db, env, { fresh: true })
      .then((h) => {
        if (cancelled) return;
        setProbe({ ok: !!h.ok, error: h.error });
        // the sidebar dot reflects what the probe just learned
        setHealth(db, !!h.ok, h.error);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [db, env, setHealth]);

  const item = groups.flatMap((g) => g.items).find((it) => it.db === db);
  const envs = item?.envs ?? [];
  const hasLocal = envs.some((e) => e.env === "local");
  const isLocal = (info?.env ?? "").toLowerCase() === "local";
  const srcEnv =
    envs.find((e) => e.env === "dev")?.env ??
    envs.find((e) => e.env && e.env !== "local")?.env ??
    "dev";

  const toggleReveal = async (): Promise<void> => {
    const next = !revealed;
    if (next) {
      const d = await fetchConnInfo(db, env, { reveal: true });
      setDisplayUrl(d.url);
    } else {
      setDisplayUrl(info?.url ?? null);
    }
    setRevealed(next);
  };

  // copies the REAL url — that's what you paste into a service env
  const copyUrl = async (): Promise<void> => {
    const d = await fetchConnInfo(db, env, { reveal: true });
    copy(d.url);
  };

  const doLocalUp = async (): Promise<void> => {
    setUpBusy(true);
    try {
      const r = await localUp(db);
      if (r.sync_error) toast(`${t("ci_mklocal_done")} · ${r.sync_error}`, false);
      else if (r.synced_from) toast(tv("ci_mklocal_synced", { env: r.synced_from }), true);
      else toast(t("ci_mklocal_done"), true);
      onClose();
      onAfterLocalUp(db);
    } catch (e) {
      toast(String((e as Error).message ?? e), false);
      setUpBusy(false);
    }
  };

  const doSync = async (): Promise<void> => {
    if (!window.confirm(tv("ci_sync_confirm", { env: srcEnv }))) return;
    setSyncBusy(true);
    try {
      const r = await localSync(db, srcEnv);
      toast(r.prev ? tv("ci_sync_done", { prev: String(r.prev) }) : t("ci_ok"), true);
      onClose();
      onAfterSync(db);
    } catch (e) {
      toast(String((e as Error).message ?? e), false);
      setSyncBusy(false);
    }
  };

  const row = (k: string, v: unknown): React.ReactNode =>
    v == null || v === "" ? null : (
      <div className="vg-cirow cirow" key={k}>
        <span className="vg-cik cik">{k}</span>
        <span className="vg-civ civ">{String(v)}</span>
      </div>
    );

  return (
    <div className="vg-modal modal" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div className="vg-box box" id="cibox" style={{ width: "min(560px, 85%)" }}>
        <div className="vg-mh mh">
          <i className="ti ti-info-circle" /> {t("conn_info")} · {db}
          {env ? ` @ ${env}` : ""}
        </div>
        <div id="cibody">
          {error && <div style={{ color: "var(--red-fg)", fontSize: "12.5px" }}>{error}</div>}
          {!info && !error && (
            <div className="vg-empty spin">
              <i className="ti ti-loader" />
            </div>
          )}
          {info && (
            <>
              {row("key", info.key)}
              {row("engine", info.engine)}
              {row("env", info.env)}
              {row("host", info.host)}
              {row("port", info.port)}
              {row("database", info.database)}
              <div className="vg-cirow cirow">
                <span className="vg-cik cik">url</span>
                <span className="vg-civ civ" id="ciurl">
                  {displayUrl}
                </span>
                <span className="vg-ciact ciact">
                  <button
                    className="vg-iconbtn iconbtn"
                    id="ciEye"
                    title={revealed ? t("ci_hide") : t("ci_reveal")}
                    onClick={() => void toggleReveal()}
                  >
                    <i className={`ti ${revealed ? "ti-eye-off" : "ti-eye"}`} />
                  </button>
                  <button
                    className="vg-iconbtn iconbtn"
                    id="ciCopy"
                    title={t("copy")}
                    onClick={() => void copyUrl()}
                  >
                    <i className="ti ti-copy" />
                  </button>
                </span>
              </div>
              {info.tunnel &&
                row(
                  t("ci_tunnel"),
                  `${info.tunnel.user || "root"}@${info.tunnel.host}:${info.tunnel.port}` +
                    (info.tunnel.key ? ` · ${info.tunnel.key}` : ""),
                )}
              {row("group", info.group)}
              {row("notes", info.notes)}
              {row(t("ci_file"), info.file)}
              <div
                className={`vg-cihealth cihealth${probe.ok === true ? " ok" : probe.ok === false ? " down" : ""}`}
                id="cihealth"
              >
                {probe.ok === null ? (
                  <>
                    <i className="ti ti-loader" /> {t("ci_checking")}
                  </>
                ) : probe.ok ? (
                  <>
                    <i className="ti ti-circle-check" /> {t("ci_ok")}
                  </>
                ) : (
                  <>
                    <i className="ti ti-alert-circle" /> {t("ci_fail")}
                    {probe.error && <pre>{probe.error}</pre>}
                  </>
                )}
              </div>
              {!hasLocal && (info.engine === "postgres" || info.engine === "redis") && (
                <div className="vg-ciactions ciactions">
                  <button
                    className="vg-btn btn"
                    id="ciUp" title={upBusy ? t("running") : t("ci_mklocal")}
                    disabled={upBusy}
                    onClick={() => void doLocalUp()}
                  >
                    <i className={`ti ${upBusy ? "ti-loader" : "ti-server-2"}`} />{" "}
                    {upBusy ? t("running") : t("ci_mklocal")}
                  </button>
                </div>
              )}
              {hasLocal && isLocal && info.engine === "postgres" && (
                <div className="vg-ciactions ciactions">
                  <button
                    className="vg-btn btn"
                    id="ciSync" title={syncBusy ? t("ci_syncing") : tv("ci_sync", { env: srcEnv })}
                    disabled={syncBusy}
                    onClick={() => void doSync()}
                  >
                    <i className={`ti ${syncBusy ? "ti-loader" : "ti-refresh"}`} />{" "}
                    {syncBusy ? t("ci_syncing") : tv("ci_sync", { env: srcEnv })}
                  </button>
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
