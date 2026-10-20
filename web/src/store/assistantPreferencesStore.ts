import { create } from "zustand";
import { getAssistantPreferences, type AssistantPreferences } from "../assistantApi";

type PreferencesState = {
  preferences?: AssistantPreferences;
  refresh: () => Promise<void>;
};

let generation = 0;

/** Read the CLI-owned preference. Never use the legacy browser override. */
export const useAssistantPreferences = create<PreferencesState>((set) => ({
  refresh: async () => {
    const request = ++generation;
    try {
      const preferences = await getAssistantPreferences();
      if (request === generation) set({ preferences });
    } catch {
      // Keep the last known value; a later focus/reopen retries the read.
    }
  },
}));
