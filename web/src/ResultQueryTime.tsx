import { useEffect, useState } from "react";
import { LANG, t } from "./i18n";

const minuteMs = 60_000;
const minutesPerDay = 24 * 60;
const relativeTime = new Intl.RelativeTimeFormat(LANG);
const absoluteTime = new Intl.DateTimeFormat(LANG, {
  dateStyle: "medium",
  timeStyle: "medium",
});

export default function ResultQueryTime({ queriedAt }: { queriedAt: number }) {
  const [now, setNow] = useState(Date.now);
  useEffect(() => {
    let timeout: number | undefined;
    const tick = () => {
      window.clearTimeout(timeout);
      const current = Date.now();
      setNow(current);
      const age = Math.max(0, current - queriedAt);
      if (age < minutesPerDay * minuteMs) {
        timeout = window.setTimeout(tick, minuteMs - (age % minuteMs));
      }
    };
    tick();
    document.addEventListener("visibilitychange", tick);
    return () => {
      window.clearTimeout(timeout);
      document.removeEventListener("visibilitychange", tick);
    };
  }, [queriedAt]);

  const minutes = Math.max(0, Math.floor((now - queriedAt) / minuteMs));
  const fullTime = absoluteTime.format(queriedAt);
  const label = minutes === 0 ? t("result_queried_now")
    : minutes < 60 ? relativeTime.format(-minutes, "minute")
    : minutes < minutesPerDay ? relativeTime.format(-Math.floor(minutes / 60), "hour")
    : fullTime;

  return (
    <time id="statusQueriedAt" dateTime={new Date(queriedAt).toISOString()}
      title={`${t("result_queried_at")} ${fullTime}`}>
      {t("result_queried_at")} <span className="query-time-value">{label}</span>
    </time>
  );
}
