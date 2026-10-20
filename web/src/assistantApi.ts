import { assistantStatusText, t, tv } from "./i18n";

export type AssistantMessage = {
  id: string;
  role: string;
  content: string;
  sql?: string;
  status?: string;
  checkpointId?: string;
  baseRevision?: number;
};
export type Checkpoint = {
  id: string;
  messageId: string;
  beforeSql: string;
  afterSql: string;
  createdAt: number | string;
};
export type Conversation = {
  id: string;
  title: string;
  db: string | null;
  env: string | null;
  workspace: string | null;
  guiWorkspace?: string | null;
  connectionAvailable?: boolean;
  sql: string;
  revision: number;
  messages: AssistantMessage[];
  checkpoints: Checkpoint[];
  updatedAt: number | string;
};
export type ConversationSummary = Omit<Conversation, "messages" | "checkpoints">;
export type AssistantConfig = { configured: boolean; model?: string };
export type ReplyLanguage = "zh" | "en" | "system";
export type AssistantPreferences = { replyLanguage: ReplyLanguage; resolvedReplyLanguage: "zh" | "en"; configured: boolean };
export type AssistantEvent = {
  type: string;
  requestId: string;
  conversationId: string;
  delta?: string;
  sql?: string;
  messageId?: string;
  conversation?: Conversation;
  error?: string;
};
const base = "/api/assistant";
export class AssistantApiError extends Error {
  status: number;
  constructor(status: number, message: string) { super(message); this.status = status; }
}

async function json<T>(path: string, body?: unknown): Promise<T> {
  const response = await fetch(base + path, body === undefined ? undefined : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new AssistantApiError(response.status, payload.error ? assistantStatusText(payload.error) : tv("assistant_http_failed", { status: response.status }));
  }
  return response.json() as Promise<T>;
}
export const getAssistantConfig = () => json<AssistantConfig>("/config");
export const getAssistantPreferences = () => json<AssistantPreferences>("/preferences");
export const listConversations = () => json<{ conversations: ConversationSummary[] }>("/conversations");
export const getConversation = (id: string) => json<Conversation>(`/conversations?id=${encodeURIComponent(id)}`);
export const createConversation = (context: { db: string | null; env: string | null; workspace: string | null; sql: string }) =>
  json<Conversation>("/conversations", context);
export const updateConversation = (id: string, revision: number, sql: string) =>
  json<Conversation>("/conversations/update", { id, revision, sql });
export const applyConversation = (id: string, revision: number, sql: string, messageId: string, explicit = false) =>
  json<Conversation>("/conversations/apply", { id, revision, sql, messageId, explicit });
export const restoreConversation = (id: string, revision: number, checkpointId: string) =>
  json<Conversation>("/conversations/restore", { id, revision, checkpointId });
export const cancelAssistant = (conversationId: string, requestId: string) =>
  json<{ cancelled: boolean }>("/cancel", { conversationId, requestId });

/** Parse complete NDJSON records, including UTF-8 and records split across chunks. */
export async function readAssistantStream(
  body: ReadableStream<Uint8Array>, onEvent: (event: AssistantEvent) => void,
): Promise<void> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let pending = "";
  const consume = (line: string) => {
    if (line.trim()) onEvent(JSON.parse(line) as AssistantEvent);
  };
  try {
    for (;;) {
      const { value, done } = await reader.read();
      pending += done ? decoder.decode() : decoder.decode(value, { stream: true });
      let newline: number;
      while ((newline = pending.indexOf("\n")) !== -1) {
        consume(pending.slice(0, newline));
        pending = pending.slice(newline + 1);
      }
      if (done) break;
    }
    consume(pending);
  } catch (error) {
    await reader.cancel().catch(() => {});
    throw error;
  } finally { reader.releaseLock(); }
}

export async function chatAssistant(
  request: { conversationId: string; revision: number; sql: string; prompt: string; explain?: boolean; requestId: string },
  onEvent: (event: AssistantEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch(base + "/chat", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(request), signal,
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new AssistantApiError(response.status, payload.error ? assistantStatusText(payload.error) : tv("assistant_http_failed", { status: response.status }));
  }
  if (!response.body) throw new Error(t("assistant_empty_response"));
  let completed = false;
  await readAssistantStream(response.body, (event) => {
    if (event.requestId !== request.requestId || event.conversationId !== request.conversationId) return;
    if (event.type === "error") throw new Error(event.error ? assistantStatusText(event.error) : t("assistant_request_failed"));
    if (event.type === "done") completed = true;
    onEvent(event);
  });
  if (!completed) throw new Error(t("assistant_interrupted"));
}
