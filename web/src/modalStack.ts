import { useEffect } from "react";

/** Escape closes the TOPMOST modal only — a stack of close callbacks, pushed
 * per mounted modal, popped on unmount; one document-level listener fires the
 * top entry (mirrors the legacy GUI's `$$('.modal')` last-element behavior). */
const stack: Array<() => void> = [];
let listening = false;

function onKeyDown(e: KeyboardEvent): void {
  if (e.key !== "Escape" || stack.length === 0) return;
  e.stopPropagation();
  stack[stack.length - 1]();
}

export function anyModalOpen(): boolean {
  return stack.length > 0;
}

export function useModalEscape(close: () => void): void {
  useEffect(() => {
    stack.push(close);
    if (!listening) {
      listening = true;
      document.addEventListener("keydown", onKeyDown);
    }
    return () => {
      const i = stack.lastIndexOf(close);
      if (i >= 0) stack.splice(i, 1);
    };
  }, [close]);
}
