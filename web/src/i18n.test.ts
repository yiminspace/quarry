import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

beforeEach(() => { localStorage.clear(); vi.resetModules(); });
afterEach(() => { vi.unstubAllGlobals(); localStorage.clear(); });

describe("GUI language preference", () => {
  it("defaults to Chinese without replacing an existing stored choice", async () => {
    const { LANG, t } = await import("./i18n");
    expect(LANG).toBe("zh");
    expect(t("assistant_name")).toBe("SQL 助手");
    expect(t("assistant_explain_prompt")).toBe("解读当前 SQL");
    expect(localStorage.getItem("qy_lang")).toBeNull();
  });
  it("honors an explicit English preference across all new assistant chrome", async () => {
    localStorage.setItem("qy_lang", "en");
    const { LANG, t } = await import("./i18n");
    expect(LANG).toBe("en");
    expect(t("assistant_name")).toBe("SQL Assistant");
    expect(t("assistant_restore")).toBe("Restore previous SQL");
    expect(t("history_conversations")).toBe("Conversations");
    expect(t("assistant_explain_prompt")).toBe("Explain this SQL");
  });
  it("toggles the existing preference and reloads instead of adding a new setting", async () => {
    const reload = vi.fn();
    vi.stubGlobal("location", { reload });
    const { toggleLang } = await import("./i18n");
    toggleLang();
    expect(localStorage.getItem("qy_lang")).toBe("en");
    expect(reload).toHaveBeenCalledOnce();
  });
  it("localizes known service status messages while retaining unknown diagnostic details", async () => {
    const { assistantStatusText } = await import("./i18n");
    expect(assistantStatusText("Previous SQL restored.")).toBe("已恢复之前的 SQL。");
    expect(assistantStatusText("Reply interrupted. Try again.")).toBe("回复已中断，请重试。");
    expect(assistantStatusText("Upstream returned error: field xyz is invalid")).toBe("Upstream returned error: field xyz is invalid");
  });
});
