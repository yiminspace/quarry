import { useEffect, useRef, useState } from "react";
import { t } from "./i18n";
import { sameTabGroup, tabTitle, useTabsStore, type Tab } from "./store/tabsStore";

export type TabBarProps = {
  /** Called instead of the store's own switch so the caller can re-point the
   * connection/editor to the target tab's db/env first. */
  onSwitch: (tab: Tab) => void;
  /** Called instead of the store's own close so the caller can stash the
   * dying tab's SQL into History first (never silently lost). */
  onClose: (tab: Tab) => void;
};

/** The editor tab bar — legacy DOM: `.tabs > .tab[data-i] > .lbl/.x` plus the
 * dashed `#tabAdd` button; double-click renames in place, drag reorders,
 * middle-click closes. */
export default function TabBar({ onSwitch, onClose }: TabBarProps) {
  const allTabs = useTabsStore((s) => s.tabs);
  const activeId = useTabsStore((s) => s.activeId);
  const active = allTabs.find((tab) => tab.id === activeId);
  const tabs = active ? allTabs.filter((tab) => sameTabGroup(tab, active)) : [];
  const addTab = useTabsStore((s) => s.addTab);
  const renameTab = useTabsStore((s) => s.renameTab);
  const reorderTab = useTabsStore((s) => s.reorderTab);

  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [draftTitle, setDraftTitle] = useState("");
  const [dragId, setDragId] = useState<string | null>(null);
  const [dragOverId, setDragOverId] = useState<string | null>(null);
  const renameCommittedRef = useRef(false);
  const stripRef = useRef<HTMLDivElement>(null);
  const menuRef = useRef<HTMLDetailsElement>(null);
  const [search, setSearch] = useState("");
  const [overflow, setOverflow] = useState({ left: false, right: false });
  useEffect(() => {
    const strip = stripRef.current;
    if (!strip) return;
    const measure = (): void => {
      const left = strip.scrollLeft > 1;
      const right = strip.scrollWidth - strip.clientWidth - strip.scrollLeft > 1;
      setOverflow((previous) => previous.left === left && previous.right === right ? previous : { left, right });
    };
    const observer = new ResizeObserver(measure);
    observer.observe(strip);
    strip.addEventListener("scroll", measure, { passive: true });
    measure();
    return () => { observer.disconnect(); strip.removeEventListener("scroll", measure); };
  }, [allTabs, activeId]);
  const scrollTabs = (direction: number): void => {
    const strip = stripRef.current;
    strip?.scrollBy({ left: direction * strip.clientWidth * 0.7, behavior: "instant" });
  };
  useEffect(() => {
    stripRef.current?.querySelector<HTMLElement>('[aria-selected="true"]')?.scrollIntoView({ block: "nearest", inline: "nearest" });
    if (menuRef.current) menuRef.current.open = false;
  }, [activeId, allTabs.length]);
  useEffect(() => {
    const strip = stripRef.current;
    if (!strip) return;
    const reveal = (): void => { strip.querySelector<HTMLElement>('[aria-selected="true"]')?.scrollIntoView({ block: "nearest", inline: "nearest" }); };
    const observer = new ResizeObserver(reveal);
    observer.observe(strip);
    reveal();
    return () => observer.disconnect();
  }, [active?.sql, active?.title]);
  useEffect(() => {
    const dismiss = (event: PointerEvent | KeyboardEvent): void => {
      if (event instanceof KeyboardEvent ? event.key === "Escape" : !menuRef.current?.contains(event.target as Node)) {
        if (menuRef.current) menuRef.current.open = false;
      }
    };
    document.addEventListener("pointerdown", dismiss);
    document.addEventListener("keydown", dismiss);
    return () => {
      document.removeEventListener("pointerdown", dismiss);
      document.removeEventListener("keydown", dismiss);
    };
  }, []);
  const closeMany = (targets: Tab[]): void => {
    targets.forEach(onClose);
    if (menuRef.current) menuRef.current.open = false;
  };

  const startRename = (tab: Tab): void => {
    renameCommittedRef.current = false;
    setDraftTitle(tabTitle(tab));
    setRenamingId(tab.id);
  };

  const commitRename = (revert: boolean): void => {
    if (renameCommittedRef.current || renamingId === null) return;
    renameCommittedRef.current = true;
    // An empty name reverts the tab to its automatic db@env / SQL title.
    if (!revert) renameTab(renamingId, draftTitle.trim() || null);
    setRenamingId(null);
  };

  return (
    <div className="tab-navigation">
    <div className="vg-tabs tabs" id="tabs" role="tablist" aria-label={t("query_tabs")} ref={stripRef}>
      {tabs.map((tab, i) => (
        <span
          key={tab.id}
          className={`vg-tab tab${tab.id === activeId ? " on" : ""}${renamingId === tab.id ? " renaming" : ""}${dragId === tab.id ? " dragging" : ""}${dragOverId === tab.id ? " dragover" : ""}`}
          data-i={i}
          role="tab"
          aria-selected={tab.id === activeId}
          tabIndex={tab.id === activeId ? 0 : -1}
          onKeyDown={(e) => {
            if (renamingId) return;
            let next: Tab | undefined;
            if (e.key === "ArrowRight") next = tabs[(i + 1) % tabs.length];
            if (e.key === "ArrowLeft") next = tabs[(i - 1 + tabs.length) % tabs.length];
            if (e.key === "Home") next = tabs[0];
            if (e.key === "End") next = tabs[tabs.length - 1];
            if (next) {
              e.preventDefault();
              onSwitch(next);
              const index = tabs.indexOf(next);
              stripRef.current?.querySelector<HTMLElement>(`[data-i="${index}"]`)?.focus();
            }
          }}
          title={`${tabTitle(tab)} · ${tab.db ?? ""}${tab.env ? `@${tab.env}` : ""}\n${tab.sql.slice(0, 300)}`}
          draggable
          onClick={() => {
            if (renamingId === tab.id) return;
            if (tab.id !== activeId) onSwitch(tab);
          }}
          onDoubleClick={(e) => {
            e.stopPropagation();
            startRename(tab);
          }}
          onMouseDown={(e) => {
            if (e.button === 1) e.preventDefault(); // no browser middle-click autoscroll
          }}
          onAuxClick={(e) => {
            if (e.button === 1) {
              e.preventDefault();
              onClose(tab);
            }
          }}
          onDragStart={(e) => {
            e.dataTransfer.effectAllowed = "move";
            e.dataTransfer.setData("text/plain", tab.id);
            setDragId(tab.id);
          }}
          onDragEnd={() => {
            setDragId(null);
            setDragOverId(null);
          }}
          onDragOver={(e) => {
            e.preventDefault();
            e.dataTransfer.dropEffect = "move";
            setDragOverId(tab.id);
          }}
          onDragLeave={() => setDragOverId((v) => (v === tab.id ? null : v))}
          onDrop={(e) => {
            e.preventDefault();
            setDragOverId(null);
            const fromId = e.dataTransfer.getData("text/plain");
            if (fromId) reorderTab(fromId, tab.id);
          }}
        >
          {renamingId === tab.id ? (
            <input
              className="vg-rn rn"
              autoFocus
              maxLength={60}
              value={draftTitle}
              onClick={(e) => e.stopPropagation()}
              onMouseDown={(e) => e.stopPropagation()}
              onChange={(e) => setDraftTitle(e.target.value)}
              onKeyDown={(e) => {
                e.stopPropagation();
                if (e.key === "Enter") commitRename(false);
                else if (e.key === "Escape") commitRename(true);
              }}
              onBlur={() => commitRename(false)}
            />
          ) : (
            <span className="vg-lbl lbl">{tabTitle(tab)}</span>
          )}
          {(
            <button
              type="button"
              aria-label={`${t("close_tab")}: ${tabTitle(tab)}`}
              className="vg-x x"
              data-x={i}
              title={t("close_tab")}
              onClick={(e) => {
                e.stopPropagation();
                onClose(tab);
              }}
            >
              ×
            </button>
          )}
        </span>
      ))}
    </div>
    <div className="tab-controls">
      {(overflow.left || overflow.right) && <div className="tab-scroll-controls">
        <button className="vg-iconbtn" id="tabScrollLeft" title={t("scroll_tabs_left")} aria-label={t("scroll_tabs_left")} disabled={!overflow.left} onClick={() => scrollTabs(-1)}><i className="ti ti-chevron-left" /></button>
        <button className="vg-iconbtn" id="tabScrollRight" title={t("scroll_tabs_right")} aria-label={t("scroll_tabs_right")} disabled={!overflow.right} onClick={() => scrollTabs(1)}><i className="ti ti-chevron-right" /></button>
      </div>}
      <button className="vg-iconbtn" id="tabAdd" title={t("new_tab")} aria-label={t("new_tab")} onClick={() => addTab()}>+</button>
      <details ref={menuRef} className="tab-menu" onToggle={(e) => { if (e.currentTarget.open) e.currentTarget.querySelector("input")?.focus(); }}>
        <summary id="tabList" onClick={() => { if (!menuRef.current?.open) setSearch(""); }} title={t("all_tabs")} aria-label={t("all_tabs")}><i className="ti ti-chevron-down" /><span>{tabs.length}</span></summary>
        <div className="tab-menu-panel">
          <input aria-label={t("find_tab")} placeholder={t("find_tab")} value={search} onChange={(e) => setSearch(e.target.value)} />
          <div className="tab-menu-items">
            {tabs.filter((tab) => `${tabTitle(tab)} ${tab.sql}`.toLowerCase().includes(search.toLowerCase())).map((tab) => (
              <button key={tab.id} className={tab.id === activeId ? "on" : ""} onClick={() => {
                onSwitch(tab);
                if (menuRef.current) menuRef.current.open = false;
              }} title={tabTitle(tab)}>{tab.id === activeId ? "✓ " : ""}{tabTitle(tab)}</button>
            ))}
            {!tabs.some((tab) => `${tabTitle(tab)} ${tab.sql}`.toLowerCase().includes(search.toLowerCase())) && <div className="tab-menu-empty">{t("no_tabs_found")}</div>}
          </div>
          <div className="tab-menu-actions">
            <button id="closeAllTabs" disabled={!tabs.length} onClick={() => closeMany(tabs)}>{t("close_all_tabs")}</button>
            <button id="closeOtherTabs" disabled={tabs.length < 2} onClick={() => closeMany(tabs.filter((tab) => tab.id !== activeId))}>{t("close_other_tabs")}</button>
            <button id="closeRightTabs" disabled={!tabs.length || tabs[tabs.length - 1]?.id === activeId} onClick={() => closeMany(tabs.slice(tabs.findIndex((tab) => tab.id === activeId) + 1))}>{t("close_right_tabs")}</button>
          </div>
        </div>
      </details>
    </div>
    </div>
  );
}
