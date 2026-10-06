import type { UpdateInfo } from "./api";
import { t } from "./i18n";
import { useModalEscape } from "./modalStack";

const UPGRADE_CMD = "pipx upgrade quarry-db";

type Props = { info: UpdateInfo; onClose: () => void };

/** The header update badge's panel: current vs. latest version, the exact
 * upgrade command, and a link to the GitHub release notes — everything
 * needed to act on "there's a new version" without leaving the page. */
export default function UpdatePanel({ info, onClose }: Props) {
  useModalEscape(onClose);
  const releaseUrl = info.latest
    ? `https://github.com/yiminspace/quarry/releases/tag/v${info.latest}`
    : null;

  return (
    <div className="vg-modal modal" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div className="vg-box box" id="updbox" style={{ width: "min(420px, 85%)" }}>
        <div className="vg-mh mh">
          <i className="ti ti-download" /> {t("update_panel_title")}
        </div>
        <div id="updbody">
          <div className="vg-cirow cirow">
            <span className="vg-cik cik">{t("current_version")}</span>
            <span className="vg-civ civ">{info.current}</span>
          </div>
          <div className="vg-cirow cirow">
            <span className="vg-cik cik">{t("latest_version")}</span>
            <span className="vg-civ civ">{info.latest}</span>
          </div>
          <div className="vg-cirow cirow">
            <span className="vg-cik cik">{t("upgrade_cmd")}</span>
            <span className="vg-civ civ" id="updCmd">
              {UPGRADE_CMD}
            </span>
          </div>
          {releaseUrl && (
            <div className="vg-cirow cirow">
              <span className="vg-cik cik">{t("release_notes")}</span>
              <span className="vg-civ civ">
                <a href={releaseUrl} target="_blank" rel="noreferrer">
                  {releaseUrl}
                </a>
              </span>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
