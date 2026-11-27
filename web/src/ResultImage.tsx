import { collectColumnImages } from "./imageDetection";
import type { QueryResult } from "./api";
import { useCallback, useEffect, useEffectEvent, useId, useRef, useState } from "react";
import { cellPreview } from "./cellValue";
import { copy } from "./clip";
import { t } from "./i18n";
import { traceImagePreview } from "./imageDiagnostics";
import { Modal } from "./Modals";

function thumbnailPosition(element: Element | null) {
  return {
    row: (element?.closest("tr")?.sectionRowIndex ?? -1) + 1,
    column: element?.closest("td")?.cellIndex ?? 0,
  };
}

function ImageLoad({ url, onOpen }: { url: string; onOpen: () => void }) {
  const thumbnail = useId();
  const host = useRef<HTMLDivElement>(null);
  const image = useRef<HTMLImageElement>(null);
  const [visible, setVisible] = useState(false);
  const [status, setStatus] = useState<"loading" | "retrying" | "ready" | "failed">("loading");
  const timerRef = useRef<number | undefined>(undefined);
  const [attempt, setAttempt] = useState(0);
  const attachImage = useCallback((element: HTMLImageElement | null) => {
    image.current = element;
    if (element) traceImagePreview("started", { thumbnail, ...thumbnailPosition(element), attempt });
  }, [thumbnail, attempt]);

  useEffect(() => {
    if (!host.current) return;
    const observer = new IntersectionObserver((entries) => {
      if (!entries.some((entry) => entry.isIntersecting)) return;
      setVisible(true);
      observer.disconnect();
    }, { root: host.current.closest(".gridwrap") });
    observer.observe(host.current);
    return () => observer.disconnect();
  }, []);

  const finish = (outcome: "loaded" | "invalid dimensions" | "browser error" | "timed out"): void => {
    window.clearTimeout(timerRef.current);
    traceImagePreview(outcome, {
      thumbnail, ...thumbnailPosition(host.current), attempt, previousStatus: status,
      ...(outcome === "timed out" ? { timeoutMs: 15_000 } : {}),
    });
    if (outcome === "loaded") {
      setStatus("ready");
      return;
    }
    setStatus(attempt === 0 && outcome !== "invalid dimensions" ? "retrying" : "failed");
  };
  const onTimeout = useEffectEvent(() => {
    // Dropping the element alone can leave an in-flight request that the retry would reuse.
    image.current?.removeAttribute("src");
    finish("timed out");
  });

  useEffect(() => {
    if (!visible) return;
    const position = thumbnailPosition(host.current);
    return () => traceImagePreview("thumbnail removed", { thumbnail, ...position });
  }, [thumbnail, visible]);

  useEffect(() => {
    if (!visible || status !== "loading") return;
    const timer = window.setTimeout(() => onTimeout(), 15_000);
    timerRef.current = timer;
    return () => window.clearTimeout(timer);
  }, [visible, status, attempt]);

  const retry = (): void => {
    setStatus("loading");
    setAttempt((n) => n + 1);
  };

  useEffect(() => {
    if (status !== "retrying") return;
    const timer = window.setTimeout(() => {
      traceImagePreview("retrying", { thumbnail, ...thumbnailPosition(host.current), mode: "automatic" });
      retry();
    }, 1000);
    return () => window.clearTimeout(timer);
  }, [thumbnail, status]);

  const busy = status === "loading" || status === "retrying";

  return (
    <div ref={host} onDoubleClick={(event) => event.stopPropagation()} className="result-image-thumb" aria-busy={visible && busy}>
      {visible && (status === "loading" || status === "ready") && (
        <img ref={attachImage} key={attempt} src={url} alt={t("image_preview")} decoding="async" referrerPolicy="no-referrer"
          onLoad={(event) => {
            finish(event.currentTarget.naturalWidth && event.currentTarget.naturalHeight ? "loaded" : "invalid dimensions");
          }} onError={() => finish("browser error")} />
      )}
      {status === "failed" ? (
        <button type="button" className="result-image-retry" title={t("image_failed")} aria-label={t("image_retry")}
          onClick={(event) => {
            event.stopPropagation();
            traceImagePreview("retrying", { thumbnail, ...thumbnailPosition(host.current), mode: "manual" });
            retry();
          }}>
          <i className="ti ti-refresh" aria-hidden="true" />
        </button>
      ) : (
        <button type="button" className="result-image-open" aria-label={t("image_enlarge")} title={t("image_enlarge")}
          disabled={status !== "ready"}
          onClick={(event) => {
            event.stopPropagation();
            onOpen();
          }}>
          {busy && <span aria-hidden="true">…</span>}
        </button>
      )}
    </div>
  );
}

export function ResultImageCell({ url, onOpen }: { url: string; onOpen: () => void }) {
  return (
    <div className="result-image-cell">
      <ImageLoad url={url} onOpen={onOpen} />
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
        <div className="result-image-large">
          <img src={url} alt={t("image_preview")} decoding="async" referrerPolicy="no-referrer" />
        </div>
        <div className="result-image-full-url">{url}</div>
      </section>
    </Modal>
  );
}

export function ResultImageColumnMenu({ result, column, enabled, onChange }: {
  result: QueryResult;
  column: string;
  enabled: boolean;
  onChange: (urls: ReadonlySet<string> | null) => void;
}) {
  const menuId = useId();
  const trigger = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const [scan, setScan] = useState<{ result: QueryResult; urls: ReadonlySet<string> }>();
  const urls = scan?.result === result ? scan.urls : undefined;
  const label = `${column} ${t("image_display")}`;
  return (
    <div className="result-image-mode" onClick={(event) => event.stopPropagation()}>
      <button ref={trigger} type="button" className="result-image-mode-trigger"
        aria-label={label} title={label} data-enabled={enabled} popoverTarget={menuId}
        onClick={(event) => {
          if (!menu.current || menu.current.matches(":popover-open")) return;
          if (!enabled && !urls) setScan({ result, urls: collectColumnImages(result.rows, column) });
          const anchor = event.currentTarget.getBoundingClientRect();
          const width = Math.min(144, window.innerWidth - 16);
          menu.current.style.left = `${Math.max(8, Math.min(anchor.right - width, window.innerWidth - width - 8))}px`;
          const above = window.innerHeight - anchor.bottom < 80 && anchor.top > 80;
          menu.current.style.top = above ? "auto" : `${anchor.bottom + 4}px`;
          menu.current.style.bottom = above ? `${window.innerHeight - anchor.top + 4}px` : "auto";
        }}>
        <i className="ti ti-dots" aria-hidden="true" />
      </button>
      <div ref={menu} id={menuId} className="result-image-mode-menu" popover="auto" role="group" aria-label={label}>
        {(enabled || urls?.size ? [false, true] : [false]).map((image) => (
          <button key={String(image)} type="button" aria-pressed={enabled === image}
            onClick={() => {
              if (enabled && !image) onChange(null);
              if (!enabled && image && urls?.size) onChange(urls);
              menu.current?.hidePopover();
              trigger.current?.focus();
            }}>
            <span>{t(image ? "image_load_column" : "image_text")}</span>
            {enabled === image && <i className="ti ti-check" aria-hidden="true" />}
          </button>
        ))}
      </div>
    </div>
  );
}
