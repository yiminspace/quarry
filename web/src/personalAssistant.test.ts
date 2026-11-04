import { describe, expect, it } from "vitest";
import type { Conversation } from "./assistantApi";
import { mergePersonalAssistantResponse } from "./personalAssistant";
const baseline: Conversation = { id: "conversation", title: "SQL conversation", db: "lab/db", env: "remote:one", workspace: "lab", sql: "select 1", revision: 1, messages: [], checkpoints: [], updatedAt: 0 };
describe("personal assistant response merge", () => {
  it("keeps later local SQL, revision and checkpoints while adding the reply", () => {
    const checkpoint = { id: "checkpoint", messageId: "earlier", beforeSql: "select 1", afterSql: "select 2", createdAt: 1 };
    const latest = { ...baseline, sql: "select 3", revision: 4, checkpoints: [checkpoint], messages: [{ id: "earlier", role: "assistant", content: "old", checkpointId: checkpoint.id }] };
    const incoming = { ...baseline, messages: [{ id: "answer", role: "assistant", content: "new", sql: "select 9" }] };
    const merged = mergePersonalAssistantResponse(baseline, incoming, latest);
    expect(merged.sql).toBe("select 3"); expect(merged.revision).toBe(4);
    expect(merged.checkpoints).toEqual([checkpoint]); expect(merged.messages.map((item) => item.id)).toEqual(["answer", "earlier"]);
  });
  it("keeps a later revision even when restore returned SQL to the baseline", () => {
    const merged = mergePersonalAssistantResponse(baseline, baseline, { ...baseline, revision: 8 });
    expect(merged.sql).toBe(baseline.sql); expect(merged.revision).toBe(8);
  });
});
