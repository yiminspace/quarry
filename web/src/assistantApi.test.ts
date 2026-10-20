import { afterEach, describe, expect, it, vi } from "vitest";
import { chatAssistant, readAssistantStream, type AssistantEvent } from "./assistantApi";
import { t } from "./i18n";
function stream(bytes: Uint8Array, breaks: number[]) {
  return new ReadableStream<Uint8Array>({ start(controller) {
    let previous = 0;
    for (const end of [...breaks, bytes.length]) { controller.enqueue(bytes.slice(previous, end)); previous = end; }
    controller.close();
  } });
}
afterEach(() => vi.unstubAllGlobals());
describe("assistant response stream", () => {
  it("decodes split UTF-8, split records, blank lines and an unterminated final record", async () => {
    const line = '{"type":"text_delta","delta":"查询"}';
    const bytes = new TextEncoder().encode(`${line}\n\n{"type":"done"}`);
    const events: AssistantEvent[] = [];
    await readAssistantStream(stream(bytes, [3, 30, 31, 32, 36]), (event) => events.push(event));
    expect(events).toEqual([{ type: "text_delta", delta: "查询" }, { type: "done" }]);
  });
  it("does not accept another request's done event as completion", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response('{"type":"done","requestId":"other","conversationId":"c1"}\n')));
    const observed: AssistantEvent[] = [];
    await expect(chatAssistant({ conversationId: "c1", requestId: "r1", revision: 0, sql: "select 1", prompt: "explain" }, (event) => observed.push(event))).rejects.toThrow(t("assistant_interrupted"));
    expect(observed).toEqual([]);
  });
  it("surfaces a model error after partial text without treating it as success", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response([
      { type: "text_delta", requestId: "r1", conversationId: "c1", delta: "Partial" },
      { type: "error", requestId: "r1", conversationId: "c1", error: "Model unavailable" },
    ].map((event) => JSON.stringify(event)).join("\n"))));
    const observed: AssistantEvent[] = [];
    await expect(chatAssistant({ conversationId: "c1", requestId: "r1", revision: 0, sql: "select 1", prompt: "explain" }, (event) => observed.push(event))).rejects.toThrow("Model unavailable");
    expect(observed).toHaveLength(1);
  });
});
