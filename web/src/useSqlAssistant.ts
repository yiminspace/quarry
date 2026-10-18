import { useCallback, useEffect, useRef, useState } from "react";
import { assistantStatusText, t } from "./i18n";
import {
  AssistantApiError, applyConversation, cancelAssistant, chatAssistant, createConversation, getAssistantConfig, getConversation,
  restoreConversation, updateConversation, type AssistantMessage, type Conversation,
} from "./assistantApi";
import { canApplyAssistantSql, sameAssistantContext, type SqlBaseline } from "./assistantState";
import { useTabsStore, type Tab } from "./store/tabsStore";

export type AssistantDraft = { text: string; explain: boolean };
export type AssistantDraftUpdate = AssistantDraft | ((current: AssistantDraft) => AssistantDraft);
export type AssistantSession = {
  open?: boolean;
  conversation?: Conversation;
  busy?: boolean;
  stream?: string;
  prompt?: string;
  error?: string;
  notice?: string;
  unappliedMessageIds?: string[];
  draft?: AssistantDraft;
};
type KeepDraft = (current: string, next: string, db: string | null, env: string | null) => void;

/** Owns assistant requests independently from database execution. No query API is used here. */
export function useSqlAssistant(keepDraft: KeepDraft, beforeWrite: (tabId: string) => void) {
  const [sessions, setSessions] = useState<Record<string, AssistantSession>>({});
  const sessionsRef = useRef(sessions);
  const locks = useRef(new Map<string, Promise<unknown>>());
  const creating = useRef(new Map<string, Promise<Conversation>>());
  const requests = useRef(new Map<string, { conversationId: string; requestId: string; controller: AbortController }>());
  const tabs = useTabsStore((state) => state.tabs);

  const publish = useCallback((tabId: string, patch: Partial<AssistantSession>) => {
    const next = { ...sessionsRef.current, [tabId]: { ...sessionsRef.current[tabId], ...patch } };
    sessionsRef.current = next;
    setSessions(next);
  }, []);
  const setDraft = useCallback((tabId: string, update: AssistantDraftUpdate) => {
    const current = sessionsRef.current[tabId]?.draft ?? { text: "", explain: false };
    publish(tabId, { draft: typeof update === "function" ? update(current) : update });
  }, [publish]);
  const markUnapplied = useCallback((tabId: string, messageId: string) => {
    const pending = sessionsRef.current[tabId]?.unappliedMessageIds ?? [];
    publish(tabId, { unappliedMessageIds: [...new Set([...pending, messageId])] });
  }, [publish]);
  const reportError = useCallback(async (tabId: string, error: unknown): Promise<void> => {
    const conversation = sessionsRef.current[tabId]?.conversation;
    if (error instanceof AssistantApiError && error.status === 409 && conversation) {
      // Reload the CAS token so an explicit retry can proceed without discarding the editor draft.
      try { publish(tabId, { conversation: await getConversation(conversation.id) }); } catch { /* preserve the original failure */ }
    }
    publish(tabId, { error: error instanceof DOMException && error.name === "AbortError" ? t("assistant_stopped") : assistantStatusText(String((error as Error).message)) });
  }, [publish]);
  const locked = useCallback(async <T,>(tabId: string, action: () => Promise<T>): Promise<T> => {
    const previous = locks.current.get(tabId) ?? Promise.resolve();
    const next = previous.catch(() => {}).then(action);
    locks.current.set(tabId, next);
    try { return await next; }
    finally { if (locks.current.get(tabId) === next) locks.current.delete(tabId); }
  }, []);

  const ensureConversation = useCallback(async (tab: Tab): Promise<Conversation> => {
    const existing = sessionsRef.current[tab.id]?.conversation;
    if (existing && sameAssistantContext(tab, existing)) return existing;
    const pending = creating.current.get(tab.id);
    if (pending) {
      const completed = await pending;
      if (sameAssistantContext(tab, completed)) return completed;
      if (creating.current.get(tab.id) === pending) creating.current.delete(tab.id);
    }
    const task = (async () => {
      let conversation: Conversation | undefined;
      if (tab.conversationId) {
        conversation = await getConversation(tab.conversationId);
        if (!sameAssistantContext(tab, conversation)) conversation = undefined;
      }
      conversation ??= await createConversation({ db: tab.db, env: tab.env, workspace: tab.workspace ?? null, sql: tab.sql });
      const current = useTabsStore.getState().tabs.find((item) => item.id === tab.id);
      if (current && sameAssistantContext(current, conversation)) {
        useTabsStore.getState().bindConversation(tab.id, conversation.id);
      }
      publish(tab.id, { conversation });
      return conversation;
    })();
    creating.current.set(tab.id, task);
    try { return await task; }
    finally { if (creating.current.get(tab.id) === task) creating.current.delete(tab.id); }
  }, [publish]);

  const syncSql = useCallback(async (tab: Tab): Promise<Conversation> => {
    let conversation = await ensureConversation(tab);
    if (conversation.sql !== tab.sql) {
      conversation = await updateConversation(conversation.id, conversation.revision, tab.sql);
      publish(tab.id, { conversation });
    }
    return conversation;
  }, [ensureConversation, publish]);

  // Save edits independently of tab lifetime. Closing a tab also flushes explicitly.
  const saveTab = useCallback(async (tab: Tab): Promise<void> => {
    if (!tab.conversationId && !sessionsRef.current[tab.id]?.conversation && !creating.current.has(tab.id)) return;
    await locked(tab.id, async () => {
      try { await syncSql(useTabsStore.getState().tabs.find((item) => item.id === tab.id) ?? tab); }
      catch (error) { await reportError(tab.id, error); }
    });
  }, [locked, syncSql, reportError]);
  useEffect(() => {
    const changed = tabs.filter((tab) => {
      const conversation = sessionsRef.current[tab.id]?.conversation;
      return conversation && sameAssistantContext(tab, conversation) && conversation.sql !== tab.sql;
    });
    const timer = window.setTimeout(() => { for (const tab of changed) void saveTab(tab); }, 600);
    return () => window.clearTimeout(timer);
  }, [tabs, saveTab]);

  const commitSql = useCallback((baseline: SqlBaseline, conversation: Conversation, nextSql: string): string | null => {
    const state = useTabsStore.getState();
    const tab = state.tabs.find((item) => item.id === baseline.id);
    if (!canApplyAssistantSql(tab, baseline)) return null;
    beforeWrite(tab!.id);
    keepDraft(tab!.sql, nextSql, tab!.db, tab!.env);
    let targetId = tab!.id;
    if (tab!.savedQueryId) {
      // Saved templates remain immutable; their edited copy owns the conversation.
      const previousActive = state.activeId;
      state.addTab({ db: tab!.db, env: tab!.env });
      targetId = useTabsStore.getState().activeId;
      state.bindConversation(tab!.id, undefined);
      if (previousActive !== tab!.id) state.switchTab(previousActive);
      publish(tab!.id, { conversation: undefined, stream: undefined, prompt: undefined, busy: false });
    }
    state.updateTab(targetId, { sql: nextSql });
    state.bindConversation(targetId, conversation.id);
    publish(targetId, { conversation, open: targetId === tab!.id ? sessionsRef.current[targetId]?.open : true, notice: undefined, error: undefined, unappliedMessageIds: [] });
    return targetId;
  }, [keepDraft, publish, beforeWrite]);

  const send = useCallback(async (tab: Tab, prompt: string, explain = false): Promise<boolean> => {
    if (sessionsRef.current[tab.id]?.busy) return false;
    publish(tab.id, { busy: true, error: undefined, notice: undefined, stream: "", prompt });
    try {
      const config = await getAssistantConfig();
      if (!config.configured) throw new Error(t("assistant_not_configured"));
      if (!useTabsStore.getState().tabs.some((item) => item.id === tab.id)) return false;
      await locked(tab.id, async () => {
        const conversation = await syncSql(tab);
        if (!useTabsStore.getState().tabs.some((item) => item.id === tab.id)) return;
        const baseline: SqlBaseline = { ...tab };
        const requestId = crypto.randomUUID();
        const controller = new AbortController();
        requests.current.set(tab.id, { conversationId: conversation.id, requestId, controller });
        let completed: Conversation | undefined;
        let proposal: { sql: string; messageId: string } | undefined;
        await chatAssistant({ conversationId: conversation.id, revision: conversation.revision, sql: tab.sql, prompt, explain, requestId }, (event) => {
          if (event.type === "text_delta") publish(tab.id, { stream: (sessionsRef.current[tab.id]?.stream ?? "") + (event.delta ?? "") });
          if (event.type === "sql_proposal" && event.sql && event.messageId) proposal = { sql: event.sql, messageId: event.messageId };
          if (event.type === "done" && event.conversation) completed = event.conversation;
        }, controller.signal);
        if (!completed) throw new Error(t("assistant_interrupted"));
        publish(tab.id, { conversation: completed, stream: undefined, prompt: undefined });
        if (proposal && !explain) {
          const current = useTabsStore.getState().tabs.find((item) => item.id === tab.id);
          if (canApplyAssistantSql(current, baseline)) {
            // Persistence can succeed even if its HTTP response is lost. Keep
            // the proposal actionable until the editor itself accepts the SQL.
            markUnapplied(tab.id, proposal.messageId);
            const applied = await applyConversation(completed.id, completed.revision, proposal.sql, proposal.messageId);
            publish(tab.id, { conversation: applied });
            if (!commitSql(baseline, applied, proposal.sql)) {
              publish(tab.id, { notice: t("assistant_changed") });
            }
          } else if (current) {
            publish(tab.id, { notice: t("assistant_changed") });
          }
        }
      });
      return true;
    } catch (error) {
      publish(tab.id, { stream: undefined, prompt: undefined });
      await reportError(tab.id, error);
      return false;
    } finally { requests.current.delete(tab.id); publish(tab.id, { busy: false }); }
  }, [publish, locked, syncSql, commitSql, reportError, markUnapplied]);

  const open = useCallback(async (tab: Tab): Promise<void> => {
    publish(tab.id, { open: true });
    const stillNeedsHistory = () => {
      const current = useTabsStore.getState().tabs.find((item) => item.id === tab.id);
      return current?.conversationId === tab.conversationId && current &&
        sameAssistantContext(current, { ...tab, workspace: tab.workspace ?? null }) &&
        !sessionsRef.current[tab.id]?.conversation;
    };
    try {
      // Reading local history does not require a configured model.
      if (tab.conversationId && !sessionsRef.current[tab.id]?.conversation) {
        const conversation = await getConversation(tab.conversationId);
        // A concurrent send may already have loaded and advanced this history.
        if (stillNeedsHistory()) publish(tab.id, { conversation });
      }
    } catch (error) {
      if (stillNeedsHistory() && !sessionsRef.current[tab.id]?.busy && !sessionsRef.current[tab.id]?.error) await reportError(tab.id, error);
    }
  }, [publish, reportError]);

  const apply = useCallback(async (tab: Tab, message: AssistantMessage): Promise<void> => {
    if (!message.sql || sessionsRef.current[tab.id]?.busy) return;
    const baseline = { ...tab };
    markUnapplied(tab.id, message.id);
    publish(tab.id, { busy: true, error: undefined });
    try {
      await locked(tab.id, async () => {
        const conversation = await syncSql(tab);
        const applied = await applyConversation(conversation.id, conversation.revision, message.sql!, message.id, true);
        publish(tab.id, { conversation: applied });
        if (!commitSql(baseline, applied, message.sql!)) publish(tab.id, { notice: t("assistant_changed_again") });
      });
    } catch (error) { await reportError(tab.id, error); }
    finally { publish(tab.id, { busy: false }); }
  }, [locked, syncSql, publish, commitSql, reportError, markUnapplied]);

  const restore = useCallback(async (tab: Tab, checkpointId: string): Promise<void> => {
    if (sessionsRef.current[tab.id]?.busy) return;
    const baseline = { ...tab };
    publish(tab.id, { busy: true, error: undefined });
    try {
      await locked(tab.id, async () => {
        const conversation = await syncSql(tab);
        if (conversation.checkpoints.find((checkpoint) => checkpoint.id === checkpointId)?.beforeSql === tab.sql) return;
        const restored = await restoreConversation(conversation.id, conversation.revision, checkpointId);
        publish(tab.id, { conversation: restored });
        if (!commitSql(baseline, restored, restored.sql)) publish(tab.id, { notice: t("assistant_changed_restore") });
      });
    } catch (error) { await reportError(tab.id, error); }
    finally { publish(tab.id, { busy: false }); }
  }, [locked, syncSql, publish, commitSql, reportError]);

  const attach = useCallback((tabId: string, conversation: Conversation) => {
    const current = sessionsRef.current[tabId];
    if (current?.busy || current?.conversation?.id === conversation.id) {
      // History reuses a live tab. Its GET can predate an in-flight reply or
      // SQL update, so reopening must not replace that tab's current session.
      publish(tabId, { open: true });
      return;
    }
    useTabsStore.getState().bindConversation(tabId, conversation.id);
    publish(tabId, { open: true, conversation, busy: false, error: undefined, notice: undefined });
  }, [publish]);
  const close = useCallback((tabId: string) => publish(tabId, { open: false }), [publish]);
  const closeTab = useCallback((tab: Tab) => {
    const request = requests.current.get(tab.id);
    if (request) {
      void cancelAssistant(request.conversationId, request.requestId).catch(() => {});
      request.controller.abort();
    }
    void saveTab(tab);
  }, [saveTab]);
  return { sessions, open, close, send, setDraft, apply, restore, attach, saveTab, closeTab };
}
