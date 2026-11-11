import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { cellPreview } from "./cellValue";
import { copy } from "./clip";
import { t } from "./i18n";
import { ResultImageColumn, type ImageMode } from "./imageDetection";
import { Modal } from "./Modals";

const emptyColumn = new ResultImageColumn();

export function ResultImageModeSelect({ column = emptyColumn, mode, label, onChange }: {
  column?: ResultImageColumn;
  mode: ImageMode;
  label: string;
  onChange: (mode: ImageMode) => void;
}) {
  const confirmed = useSyncExternalStore(column.subscribe, column.getSnapshot);
  if (!confirmed && mode === "auto") return null;
  return (
    <select className="result-image-mode" aria-label={label} value={mode}
      onClick={(event) => event.stopPropagation()}
      onChange={(event) => onChange(event.target.value as ImageMode)}>
      <option value="auto">{t("image_auto")}</option>
      <option value="image">{t("image_mode")}</option>
      <option value="text">{t("image_text")}</option>
    </select>
  );
}

function ImageLoad({ url, large = false, onOpen, onConfirmed }: { url: string; large?: boolean; onOpen?: () => void; onConfirmed?: () => void }) {
  const host = useRef<HTMLDivElement>(null);
  const [visible, setVisible] = useState(large);
  const [status, setStatus] = useState<"loading" | "ready" | "failed">("loading");
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    if (large || !host.current) return;
    const observer = new IntersectionObserver((entries) => {
      if (!entries.some((entry) => entry.isIntersecting)) return;
      setVisible(true);
      observer.disconnect();
    }, { root: host.current.closest(".gridwrap") });
    observer.observe(host.current);
    return () => observer.disconnect();
  }, [large]);

  useEffect(() => {
    if (!visible || status !== "loading") return;
    const timer = window.setTimeout(() => setStatus("failed"), 15_000);
    return () => window.clearTimeout(timer);
  }, [visible, status, attempt]);

  return (
    <div ref={host} className={large ? "result-image-large" : "result-image-thumb"} aria-busy={visible && status === "loading"}>
      {visible && status !== "failed" && (
        <img key={attempt} src={url} alt={t("image_preview")} decoding="async" referrerPolicy="no-referrer"
          onLoad={(event) => {
            if (!event.currentTarget.naturalWidth || !event.currentTarget.naturalHeight) { setStatus("failed"); return; }
            setStatus("ready");
            onConfirmed?.();
          }} onError={() => setStatus("failed")} />
      )}
      {status === "failed" ? (
        <button type="button" className="result-image-retry" title={t("image_failed")} aria-label={t("image_retry")}
          onClick={(event) => {
            event.stopPropagation();
            setStatus("loading");
            setAttempt((n) => n + 1);
          }}>
          {large ? t("image_retry") : <i className="ti ti-refresh" aria-hidden="true" />}
        </button>
      ) : onOpen ? (
        <button type="button" className="result-image-open" aria-label={t("image_enlarge")} title={t("image_enlarge")}
          onClick={(event) => { event.stopPropagation(); onOpen(); }}>
          {status === "loading" && <span aria-hidden="true">…</span>}
        </button>
      ) : status === "loading" && <span>{t("image_loading")}</span>}
    </div>
  );
}

export function ResultImageCell({ url, onOpen, onConfirmed }: { url: string; onOpen: () => void; onConfirmed: () => void }) {
  return (
    <div className="result-image-cell" onDoubleClick={(event) => event.stopPropagation()}>
      <ImageLoad url={url} onOpen={onOpen} onConfirmed={onConfirmed} />
      <span className="result-image-url">{cellPreview(url).text}</span>
    </div>
  );
}

export function ResultImageModal({ url, onClose }: { url: string; onClose: () => void }) {
  const closeButton = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    closeButton.current?.focus();
    return () => previous?.focus();
  }, []);
  return (
    <Modal onClose={onClose} boxId="imagePreview">
      <section role="dialog" aria-modal="true" aria-label={t("image_preview")}
        onKeyDown={(event) => {
          if (event.key !== "Tab") return;
          const controls = event.currentTarget.querySelectorAll<HTMLElement>("button, a[href]");
          const first = controls[0];
          const last = controls[controls.length - 1];
          if (event.shiftKey && document.activeElement === first) {
            event.preventDefault();
            last?.focus();
          } else if (!event.shiftKey && document.activeElement === last) {
            event.preventDefault();
            first?.focus();
          }
        }}>
        <div className="result-image-actions">
          <strong>{t("image_preview")}</strong>
          <button className="vg-btn btn" onClick={() => copy(url)}>{t("image_copy_url")}</button>
          <a className="vg-btn btn" href={url} target="_blank" rel="noopener noreferrer" referrerPolicy="no-referrer">{t("image_original")}</a>
          <button ref={closeButton} className="vg-btn btn" onClick={onClose}>{t("image_close")}</button>
        </div>
        <ImageLoad key={url} url={url} large />
        <div className="result-image-full-url">{url}</div>
      </section>
    </Modal>
  );
}
