import type { ChangelogVersion } from "./api";
import { t } from "./i18n";
import { useModalEscape } from "./modalStack";

type Props = { versions: ChangelogVersion[]; onClose: () => void };

/** The header's "what changed since you last looked" panel — auto-shown once
 * per real upgrade (see useEvents.ts's checkWhatsNew), never reappearing on
 * a plain reload. Reuses the same `.modal .box` chrome as UpdatePanel (#79),
 * just with parsed CHANGELOG.md sections instead of PyPI version info. */
export default function WhatsNewPanel({ versions, onClose }: Props) {
  useModalEscape(onClose);

  return (
    <div className="vg-modal modal" onClick={(e) => e.target === e.currentTarget && onClose()}>
      <div className="vg-box box" id="whatsNewBox" style={{ width: "min(480px, 85%)" }}>
        <div className="vg-mh mh">
          <i className="ti ti-sparkles" /> {t("whats_new_title")}
        </div>
        <div id="whatsNewBody">
          {versions.map((v) => (
            <div key={v.version} style={{ marginBottom: 12 }}>
              <div className="vg-cirow cirow">
                <span className="vg-cik cik">{v.version}</span>
                <span className="vg-civ civ">{v.date}</span>
              </div>
              <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>
                {v.entries.map((entry, i) => (
                  <li key={i}>{entry}</li>
                ))}
              </ul>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
