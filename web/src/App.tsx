import Header from "./Header";
import ResultWorkbench from "./ResultWorkbench";
import { t, tv } from "./i18n";
import { useConnStore } from "./store/connStore";
import { useToastStore } from "./store/toastStore";
import { useUiStore } from "./store/uiStore";
import { useEvents } from "./useEvents";

function Toast() {
  const msg = useToastStore((s) => s.msg);
  const ok = useToastStore((s) => s.ok);
  if (msg === null) return null;
  return (
    <div
      id="toast"
      className="vg-toast toast"
      style={{
        background: ok ? "var(--ok-bg)" : "var(--red-bg)",
        color: ok ? "var(--ok)" : "var(--red-fg)",
        borderColor: ok ? "var(--ok)" : "var(--red)",
      }}
    >
      {msg}
    </div>
  );
}

/** Persistent "server was upgraded — reload" banner. Unlike a toast it stays
 * until acted on: a stale bundle talking to a newer backend is a state the
 * user must leave deliberately, not a notification to glance past. */
function UpgradeBanner() {
  const upgradedTo = useUiStore((s) => s.upgradedTo);
  if (upgradedTo === null) return null;
  return (
    <div id="upgradeBanner" className="vg-upgrade-banner upgrade-banner">
      <span>{tv("upgraded_banner", { version: upgradedTo })}</span>
      <button onClick={() => window.location.reload()}>{t("reload_page")}</button>
    </div>
  );
}

/** App shell: header bar on top, the workbench (sidebar + query section)
 * filling the rest — the legacy GUI's `<body>` layout. */
export default function App() {
  useEvents();
  const isProduction = useConnStore((s) => s.current?.production === true);
  return (
    <div className="app-shell" data-production={isProduction}>
      <UpgradeBanner />
      <Header />
      <ResultWorkbench />
      <Toast />
    </div>
  );
}
