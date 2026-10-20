import { useEffect, useState } from "react";
import { addWorkspace, fetchWorkspaces, removeWorkspace, type WorkspacesResponse } from "./api";
import { t, tv } from "./i18n";
import { useModalEscape } from "./modalStack";
import { useConnStore } from "./store/connStore";
import { useTabsStore } from "./store/tabsStore";
import { toast } from "./store/toastStore";

type Props = { onClose: () => void };

/** config.toml-registered workspace dirs: list (with missing-dir /
 * no-connections warnings), add, and confirm-gated remove — legacy
 * `openWorkspaces()` DOM (`.wslist/.wsrow/.wspath/.wswarn/.wsadd/.wsconfig`).
 * Every change refreshes the connection tree immediately. */
export default function WorkspaceModal({ onClose }: Props) {
  const [data, setData] = useState<WorkspacesResponse | null>(null);
  const [input, setInput] = useState("");

  useModalEscape(onClose);

  useEffect(() => {
    fetchWorkspaces()
      .then(setData)
      .catch((e) => toast(String((e as Error).message ?? e), false));
  }, []);

  const doAdd = async (): Promise<void> => {
    const dir = input.trim();
    if (!dir) return;
    try {
      const next = await addWorkspace(dir);
      setData(next);
      setInput("");
      toast(t("ws_added"), true);
      // A newly registered workspace may bring its own connections.
      useConnStore.getState().requestReload();
    } catch (e) {
      toast(String((e as Error).message ?? e), false);
    }
  };

  const doRemove = async (dir: string): Promise<void> => {
    if (!window.confirm(tv("ws_remove_confirm", { dir }))) return;
    try {
      const next = await removeWorkspace(dir);
      setData(next);
      const tabs = useTabsStore.getState();
      const active = tabs.tabs.find((tab) => tab.id === tabs.activeId);
      if (active?.workspace === dir) {
        tabs.updateActiveTab({ db: null, env: null });
        const conn = useConnStore.getState();
        conn.setCurrent(null);
        conn.setCurrentTable(null);
      }
      toast(t("ws_removed"), true);
      // The removed workspace's connections must disappear immediately —
      // including unbinding the active connection if it belonged to it.
      useConnStore.getState().requestReload();
    } catch (e) {
      toast(String((e as Error).message ?? e), false);
    }
  };

  return (
    <div className="vg-modal modal" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div className="vg-box box" id="wsbox" style={{ width: "min(520px, 85%)" }}>
        <div className="vg-mh mh">
          <i className="ti ti-stack-2" /> {t("ws_title")}
        </div>
        <div id="wsbody">
          {!data && (
            <div className="vg-empty spin">
              <i className="ti ti-loader" />
            </div>
          )}
          {data && (
            <>
              {data.items.length === 0 ? (
                <p style={{ color: "var(--fg3)", fontSize: "12.5px", margin: "0 0 8px" }}>
                  {t("ws_empty")}
                </p>
              ) : (
                <div className="vg-wslist wslist">
                  {data.items.map((it) => {
                    const warn = !it.exists
                      ? t("ws_missing")
                      : !it.hasConnections
                        ? t("ws_no_conn")
                        : "";
                    const proxyLabel = !it.proxyEnabled
                      ? t("ws_proxy_off")
                      : data.proxyDiscovered
                        ? tv("ws_proxy_on_addr", { addr: `${data.proxyDiscovered.host}:${data.proxyDiscovered.port}` })
                        : t("ws_proxy_on_none");
                    return (
                      <div className="vg-wsrow wsrow" key={it.dir}>
                        <span className="vg-wspath wspath" title={it.dir}>
                          {it.display}
                        </span>
                        {warn && <span className="vg-wswarn wswarn">{warn}</span>}
                        <span
                          className={`vg-wsproxy wsproxy${it.proxyEnabled ? " on" : ""}`}
                          data-testid="ws-proxy-status"
                          data-dir={it.dir}
                        >
                          {proxyLabel}
                        </span>
                        <button
                          className="vg-iconbtn iconbtn wsdel"
                          data-dir={it.dir}
                          title={t("ws_remove")}
                          onClick={() => void doRemove(it.dir)}
                        >
                          <i className="ti ti-trash" />
                        </button>
                      </div>
                    );
                  })}
                </div>
              )}
              <div className="vg-wsadd wsadd">
                <input
                  className="vg-input"
                  id="wsInput"
                  placeholder={t("ws_add_ph")}
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && void doAdd()}
                />
                <button className="vg-btn btn" id="wsAddBtn" title={t("ws_add")} onClick={() => void doAdd()}>
                  <i className="ti ti-plus" /> {t("ws_add")}
                </button>
              </div>
              <div className="vg-wsconfig wsconfig">
                {t("ws_config")}: {data.config}
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
