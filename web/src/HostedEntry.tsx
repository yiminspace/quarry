import { useState, type ReactNode } from "react";
import { hostedConfig, hostedConfigurationError, signIn } from "./hostedAuth";
import { PLATFORM_MODE, platformSnapshot } from "./platform";
import { t } from "./i18n";

/** Authentication is served by the image; the database workbench stays shared. */
export default function HostedEntry({ children }: { children: ReactNode }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  if (!PLATFORM_MODE) return children;
  const configurationError = hostedConfigurationError();
  if (!configurationError && platformSnapshot().account) return children;
  const auth = hostedConfig().auth;
  return <main className="hosted-entry">
    <section className="hosted-login" aria-labelledby="hosted-login-title">
      <div className="vg-logo logo">Q</div>
      <h1 id="hosted-login-title">{t("hosted_sign_in")}</h1>
      {platformSnapshot().error && <p role="alert">{platformSnapshot().error}</p>}
      {configurationError ? <>
        <p role="alert">{t("hosted_unavailable")}</p>
        <button className="vg-btn btn" onClick={() => location.reload()}>{t("hosted_retry")}</button>
      </> : auth.mode === "builtin" ? <form onSubmit={(event) => {
        event.preventDefault(); setBusy(true); setError(null);
        void signIn(username, password).catch(() => { setError(t("hosted_sign_in_failed")); setPassword(""); }).finally(() => setBusy(false));
      }}>
        <label>{t("hosted_username")}<input aria-label={t("hosted_username")} autoComplete="username" value={username} onChange={(event) => setUsername(event.target.value)} required autoFocus /></label>
        <label>{t("hosted_password")}<input aria-label={t("hosted_password")} type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} required /></label>
        {error && <p role="alert">{error}</p>}
        <button className="vg-btn btn" disabled={busy}>{busy ? t("hosted_signing_in") : t("hosted_sign_in")}</button>
      </form> : <>
        <p>{t("hosted_platform_sign_in")}</p>
        {auth.loginUrl && <a className="vg-btn btn" href={auth.loginUrl}>{t("hosted_continue")}</a>}
      </>}
    </section>
  </main>;
}
