import { beforeEach, describe, expect, it, vi } from "vitest";
import { getAssistantPreferences, type AssistantPreferences } from "../assistantApi";
import { useAssistantPreferences } from "./assistantPreferencesStore";

vi.mock("../assistantApi", () => ({ getAssistantPreferences: vi.fn() }));

const chinese: AssistantPreferences = { replyLanguage: "zh", resolvedReplyLanguage: "zh", configured: true };
const english: AssistantPreferences = { replyLanguage: "system", resolvedReplyLanguage: "en", configured: false };

beforeEach(() => {
  vi.resetAllMocks();
  localStorage.clear();
  useAssistantPreferences.setState({ preferences: undefined });
});

describe("CLI-owned GUI reply language", () => {
  it("uses server preferences instead of the old browser choice", async () => {
    localStorage.setItem("qy_assistant_language", "en");
    vi.mocked(getAssistantPreferences).mockResolvedValue(chinese);
    await useAssistantPreferences.getState().refresh();
    expect(useAssistantPreferences.getState().preferences).toEqual(chinese);
    expect(localStorage.getItem("qy_assistant_language")).toBe("en");
  });

  it("ignores an old read after a newer refresh sees a CLI change", async () => {
    let finishRead!: (preferences: AssistantPreferences) => void;
    vi.mocked(getAssistantPreferences)
      .mockImplementationOnce(() => new Promise((resolve) => { finishRead = resolve; }))
      .mockResolvedValueOnce(chinese);
    const reading = useAssistantPreferences.getState().refresh();
    await useAssistantPreferences.getState().refresh();
    finishRead(english);
    await reading;
    expect(useAssistantPreferences.getState().preferences).toEqual(chinese);
  });

  it("retains the known preference on a failed read and refreshes later", async () => {
    useAssistantPreferences.setState({ preferences: chinese });
    vi.mocked(getAssistantPreferences).mockRejectedValueOnce(new Error("offline")).mockResolvedValueOnce(english);
    await useAssistantPreferences.getState().refresh();
    expect(useAssistantPreferences.getState().preferences).toEqual(chinese);
    await useAssistantPreferences.getState().refresh();
    expect(useAssistantPreferences.getState().preferences).toEqual(english);
  });
});
