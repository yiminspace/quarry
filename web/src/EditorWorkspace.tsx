import { useEffect, useLayoutEffect, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { t } from "./i18n";
import { EDITOR_MIN, useUiStore } from "./store/uiStore";
const COLLAPSED_HEIGHT = Math.max(EDITOR_MIN, 88);
// Leave room for the chat's maximum-size composer and a visible message area.
const EXPANDED_MIN_HEIGHT = 200;

export default function EditorWorkspace({ open, onOpen, onClose, children, assistant, toolbar }: {
  open: boolean; onOpen: () => void; onClose: () => void; children: ReactNode; assistant: ReactNode; toolbar: ReactNode;
}) {
  // The persisted height always describes SQL input only. An expanded assistant
  // stretches to the bottom of the SQL column's resize handle and query toolbar.
  const editorHeight = useUiStore((state) => state.editorHeight);
  const setEditorHeight = useUiStore((state) => state.setEditorHeight);
  const expandedHeight = useRef(Math.max(360, editorHeight));
  const wasOpen = useRef(false);
  const wrapper = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(38);
  const [maxHeight, setMaxHeight] = useState(window.innerHeight);
  // Resizing the window changes the visible height, not the saved preference.
  const height = Math.min(editorHeight, maxHeight);

  useLayoutEffect(() => {
    const region = wrapper.current;
    const section = region?.parentElement;
    if (!region || !section) return;
    const toolbarElement = section.querySelector<HTMLElement>(".toolbar");
    const handle = section.querySelector<HTMLElement>("#edresizer");
    const status = section.querySelector<HTMLElement>("#status");
    const measure = () => {
      const bottom = Math.min(section.getBoundingClientRect().bottom, window.innerHeight);
      const limit = Math.max(0, Math.floor(bottom - region.getBoundingClientRect().top
        - (toolbarElement?.getBoundingClientRect().height ?? 0)
        - (handle?.getBoundingClientRect().height ?? 0)
        - (status?.getBoundingClientRect().height ?? 0)));
      setMaxHeight((previous) => previous === limit ? previous : limit);
    };
    const observer = new ResizeObserver(measure);
    // Header wrapping, toolbar wrapping and status visibility all change the
    // room available to the editor. The result grid may shrink all the way down.
    for (const element of [section, toolbarElement, handle, status, ...section.children]) {
      if (element && element !== region && element.id !== "grid") observer.observe(element);
    }
    window.addEventListener("resize", measure);
    measure();
    return () => { observer.disconnect(); window.removeEventListener("resize", measure); };
  }, [open]);

  useEffect(() => {
    if (open === wasOpen.current) return;
    wasOpen.current = open;
    if (open) setEditorHeight(expandedHeight.current);
    else {
      expandedHeight.current = Math.max(EXPANDED_MIN_HEIGHT, editorHeight);
      setEditorHeight(COLLAPSED_HEIGHT);
    }
  }, [open, editorHeight, setEditorHeight]);

  const drag = (event: React.PointerEvent, direction: "horizontal" | "vertical") => {
    event.preventDefault();
    const startX = event.clientX, startY = event.clientY, initialWidth = width, initialHeight = height;
    const available = wrapper.current?.clientWidth ?? 1;
    const move = (next: PointerEvent) => {
      if (direction === "horizontal") setWidth(Math.max(28, Math.min(60, initialWidth + (next.clientX - startX) / available * 100)));
      else setEditorHeight(Math.min(maxHeight, Math.max(open ? EXPANDED_MIN_HEIGHT : COLLAPSED_HEIGHT, initialHeight + next.clientY - startY)));
    };
    const end = () => { window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", end); };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", end, { once: true });
  };
  const resizer = <div className="vg-hresizer hresizer" id="edresizer" role="separator" aria-label={t("editor_resize")} aria-orientation="horizontal" title={t("editor_drag")}
    onPointerDown={(event) => drag(event, "vertical")} />;

  return <>
    <div className={`editor-workspace${open ? " assistant-expanded" : ""}`} data-testid="editor-workspace" ref={wrapper}
      style={{ height: open ? "auto" : height, "--assistant-width": `${width}%` } as CSSProperties}>
      {open ? <>
        <aside className="assistant-pane" data-testid="assistant-panel" aria-label={t("assistant_panel")}>
          {assistant}
        </aside>
        <div className="assistant-divider-slot">
          <div className="vg-hresizer assistant-divider" data-testid="assistant-divider" role="separator" aria-label={t("assistant_resize")} aria-orientation="vertical"
            tabIndex={0} aria-valuenow={width} aria-valuemin={28} aria-valuemax={60}
            onPointerDown={(event) => drag(event, "horizontal")}
            onKeyDown={(event) => {
              if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
                event.preventDefault(); setWidth((value) => Math.min(60, Math.max(28, value + (event.key === "ArrowRight" ? 2 : -2))));
              }
            }} />
          <button type="button" className="assistant-close" data-testid="assistant-close" title={t("assistant_hide")} aria-label={t("assistant_hide")} onClick={onClose}>
            <i className="ti ti-chevrons-left" aria-hidden="true" />
          </button>
        </div>
      </> : <button type="button" className="assistant-rail" data-testid="assistant-open" onClick={onOpen} aria-label={t("assistant_open")} title={t("assistant_open")}>
        <span className="assistant-rail-name">{t("assistant_rail_label")}</span>
      </button>}
      <div className="editor-document">
        <div className="editor-input" style={{ height }}>{children}</div>
        {open && <>{resizer}{toolbar}</>}
      </div>
    </div>
    {!open && <>{resizer}{toolbar}</>}
  </>;
}
