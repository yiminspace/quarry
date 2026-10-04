import { useEffect, useRef, useState } from "react";
import { t, tv } from "./i18n";

type RunControlsProps = {
  autoRun: boolean;
  disabled: boolean;
  scope: string;
  onRun: () => void;
  onModeChange: (enabled: boolean) => void;
};

/** The action and its remembered execution mode share one control. The parent
 * keys this component by connection/tab so an open menu never changes targets. */
export default function RunControls({ autoRun, disabled, scope, onRun, onModeChange }: RunControlsProps) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    menuRef.current?.querySelector<HTMLElement>('[aria-checked="true"]')?.focus();
    const dismiss = (event: PointerEvent): void => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", dismiss);
    return () => document.removeEventListener("pointerdown", dismiss);
  }, [open]);

  const close = (): void => {
    setOpen(false);
    triggerRef.current?.focus();
  };

  return (
    <div className="run-controls" ref={rootRef} onKeyDown={(event) => {
      // Preserve app shortcuts while isolating button/menu navigation from the grid.
      if (!event.metaKey && !event.ctrlKey && !event.altKey &&
          ["Enter", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"].includes(event.key)) {
        event.stopPropagation();
      }
    }} onBlur={(event) => {
      if (!event.currentTarget.contains(event.relatedTarget)) setOpen(false);
    }}>
      <div className="run-split" role="group" aria-label={t("execution_controls")}>
        <button type="button" className="vg-btn btn" id="runBtn" title={t("run")} onClick={() => {
          setOpen(false);
          onRun();
        }}>
          <i className="ti ti-player-play" aria-hidden="true" /> <span id="runLbl">{t("run")}</span>
        </button>
        <button type="button" id="runModeBtn" ref={triggerRef} disabled={disabled}
          data-auto={autoRun} aria-haspopup="menu" aria-expanded={open} aria-controls={open ? "runModeMenu" : undefined}
          aria-label={`${t("execution_mode")}: ${t(autoRun ? "run_auto" : "run_manual")}`}
          title={t("execution_mode")} onClick={() => setOpen(!open)}
          onKeyDown={(event) => {
            if (event.key === "ArrowDown" || event.key === "ArrowUp") {
              event.preventDefault();
              event.stopPropagation();
              setOpen(true);
            }
          }}>
          <span>{t(autoRun ? "run_auto" : "run_manual")}</span>
          <i className="ti ti-chevron-down" aria-hidden="true" />
        </button>
      </div>
      {open && <div className="run-mode-menu" id="runModeMenu" role="menu" aria-label={t("execution_mode")} ref={menuRef}
        onKeyDown={(event) => {
          // Menu navigation must not also move or inspect the selected grid cell.
          event.stopPropagation();
          if (event.key === "Escape") { event.preventDefault(); close(); }
          const items = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="menuitemradio"]'));
          const index = items.indexOf(document.activeElement as HTMLButtonElement);
          const next = event.key === "Home" ? 0 : event.key === "End" ? items.length - 1
            : event.key === "ArrowDown" ? (index + 1) % items.length
            : event.key === "ArrowUp" ? (index - 1 + items.length) % items.length : null;
          if (next !== null) { event.preventDefault(); items[next]?.focus(); }
          if (event.key === "Tab") {
            // Return to the trigger before native Tab moves to the adjacent action.
            close();
          }
        }}>
        {[false, true].map((enabled) => <button type="button" key={String(enabled)}
          id={enabled ? "runModeAuto" : "runModeManual"} role="menuitemradio" aria-checked={autoRun === enabled}
          aria-labelledby={enabled ? "runAutoLabel" : "runManualLabel"}
          aria-describedby={enabled ? "runAutoHint" : "runManualHint"} tabIndex={-1}
          onClick={() => { onModeChange(enabled); close(); }}>
          <i className="ti ti-check" aria-hidden="true" />
          <span><strong id={enabled ? "runAutoLabel" : "runManualLabel"}>{t(enabled ? "run_auto" : "run_manual")}</strong>
            <small id={enabled ? "runAutoHint" : "runManualHint"}>{t(enabled ? "run_auto_hint" : "run_manual_hint")}</small></span>
        </button>)}
        <div className="run-mode-scope">{tv("run_mode_scope", { scope })}</div>
      </div>}
    </div>
  );
}
