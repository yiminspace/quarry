import { create } from "zustand";

type ToastState = {
  msg: string | null;
  ok: boolean;
  /** Show a toast: ok=true is the short green confirmation (1.4s), ok=false
   * the longer red error (4s) — same styles and durations as the legacy GUI. */
  toast: (msg: string, ok: boolean) => void;
};

let timer: number | undefined;

export const useToastStore = create<ToastState>((set) => ({
  msg: null,
  ok: false,
  toast: (msg, ok) => {
    window.clearTimeout(timer);
    set({ msg, ok });
    timer = window.setTimeout(() => set({ msg: null }), ok ? 1400 : 4000);
  },
}));

export const toast = (msg: string, ok: boolean): void =>
  useToastStore.getState().toast(msg, ok);
