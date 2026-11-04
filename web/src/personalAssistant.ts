import { LANG } from "./i18n";
import type { Conversation, ConversationSummary } from "./assistantApi";
import { personalStorage } from "./platform";
const KEY = "qy_assistant_conversations";
function read(): Record<string, Conversation> { try { return JSON.parse(personalStorage.getItem(KEY) || "{}"); } catch { return {}; } }
export function storePersonalConversation(conversation: Conversation): Conversation {
  const values = read();
  values[conversation.id] = { ...conversation, updatedAt: Date.now() };
  personalStorage.setItem(KEY, JSON.stringify(values));
  return values[conversation.id];
}
export function personalConversations(): { conversations: ConversationSummary[] } {
  return { conversations: Object.values(read()).sort((a, b) => Number(b.updatedAt) - Number(a.updatedAt)).map((conversation) => ({ ...conversation, messages: undefined, checkpoints: undefined })) };
}
export function personalConversation(id: string): Conversation {
  const conversation = read()[id];
  if (!conversation) throw new Error("此对话未保存在当前电脑；请恢复本机连接或开始新对话。");
  return conversation;
}
function checked(id: string, revision: number): Conversation {
  const conversation = personalConversation(id);
  if (conversation.revision !== revision) throw new Error("对话已更新，请重新打开后再操作。");
  return structuredClone(conversation);
}
export function personalAssistantRequest(path: string, body?: unknown): unknown {
  const url = new URL(path, "http://local");
  const data = (body ?? {}) as Record<string, unknown>;
  if (url.pathname === "/preferences") return { replyLanguage: "system", resolvedReplyLanguage: LANG, configured: true };
  if (url.pathname === "/conversations" && body === undefined) return url.searchParams.has("id") ? personalConversation(url.searchParams.get("id")!) : personalConversations();
  if (url.pathname === "/conversations") return storePersonalConversation({ id: crypto.randomUUID(), title: "SQL conversation", db: data.db as string | null, env: data.env as string | null, workspace: data.workspace as string | null, sql: String(data.sql ?? ""), revision: 0, messages: [], checkpoints: [], updatedAt: Date.now() });
  if (url.pathname === "/cancel") return { cancelled: true }; // AbortController owns the active HTTP request.
  const conversation = checked(String(data.id), Number(data.revision));
  const sql = String(data.sql ?? "");
  if (url.pathname === "/conversations/update") { if (conversation.sql !== sql) { conversation.sql = sql; conversation.revision++; } }
  else if (url.pathname === "/conversations/apply") {
    const message = conversation.messages.find((item) => item.id === data.messageId);
    if (!message || message.role !== "assistant" || message.sql !== sql || (message.status && message.status !== "complete")) throw new Error("只能应用已完成的 SQL 建议。");
    if (!data.explicit && message.baseRevision !== undefined && message.baseRevision !== conversation.revision) throw new Error("SQL 已修改，请先检查建议。");
    if (conversation.sql !== sql) {
      const checkpoint = { id: crypto.randomUUID(), messageId: message.id, beforeSql: conversation.sql, afterSql: sql, createdAt: Date.now() };
      conversation.checkpoints.push(checkpoint); message.checkpointId = checkpoint.id; conversation.sql = sql; conversation.revision++;
    }
  } else if (url.pathname === "/conversations/restore") {
    const checkpoint = conversation.checkpoints.find((item) => item.id === data.checkpointId);
    if (!checkpoint) throw new Error("找不到 SQL 恢复点。");
    if (conversation.sql !== checkpoint.beforeSql) {
      conversation.sql = checkpoint.beforeSql; conversation.revision++;
      conversation.messages.push({ id: crypto.randomUUID(), role: "system", content: "Previous SQL restored." });
    }
  } else throw new Error("此助手操作不可用。");
  return storePersonalConversation(conversation);
}

/** A model reply adds messages; it cannot roll back edits/checkpoints made while streaming. */
export function mergePersonalAssistantResponse(baseline: Conversation, incoming: Conversation, latest: Conversation): Conversation {
  const messages = incoming.messages.map((message) => ({ ...message }));
  for (const message of latest.messages) {
    const index = messages.findIndex((item) => item.id === message.id);
    if (index < 0) messages.push(message);
    else messages[index] = { ...messages[index], ...message };
  }
  const checkpoints = [...incoming.checkpoints];
  for (const checkpoint of latest.checkpoints) {
    const index = checkpoints.findIndex((item) => item.id === checkpoint.id);
    if (index < 0) checkpoints.push(checkpoint); else checkpoints[index] = checkpoint;
  }
  const changed = latest.sql !== baseline.sql || latest.revision !== baseline.revision;
  return { ...incoming, db: baseline.db, env: baseline.env, workspace: baseline.workspace, guiWorkspace: baseline.workspace,
    connectionAvailable: true, sql: changed ? latest.sql : incoming.sql, revision: changed ? latest.revision : incoming.revision, messages, checkpoints };
}
