import { useEffect, useRef } from "react";
import { assistantStatusText, LANG, t } from "./i18n";
import type { AssistantMessage } from "./assistantApi";
import { shouldSubmitAssistant } from "./assistantState";
import type { AssistantDraftUpdate, AssistantSession } from "./useSqlAssistant";
import AssistantMarkdown from "./AssistantMarkdown";
import { useAssistantPreferences } from "./store/assistantPreferencesStore";

export default function SqlAssistant({ session, currentSql, onDraftChange, onSend, onApply, onRestore }: {
  session: AssistantSession;
  currentSql: string;
  onDraftChange: (update: AssistantDraftUpdate) => void;
  onSend: (prompt: string, explain?: boolean) => Promise<boolean>;
  onApply: (message: AssistantMessage) => void;
  onRestore: (checkpointId: string) => void;
}) {
  const { preferences, refresh } = useAssistantPreferences();
  const language = preferences?.resolvedReplyLanguage ?? LANG;
  const explanationPrompt = t("assistant_explain_prompt", language);
  useEffect(() => {
    void refresh();
    const onFocus = () => { void refresh(); };
    const onVisible = () => { if (document.visibilityState === "visible") void refresh(); };
    window.addEventListener("focus", onFocus);
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.removeEventListener("focus", onFocus);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [refresh]);
  const input = session.draft ?? { text: "", explain: false };
  const draft = input.text;
  const suggestExplanation = !!currentSql.trim() && !draft && !session.busy;
  const messages = useRef<HTMLDivElement>(null);
  const follow = useRef(true);
  const composing = useRef(false);
  const conversation = session.conversation;
  useEffect(() => {
    if (follow.current && messages.current) messages.current.scrollTop = messages.current.scrollHeight;
  }, [conversation, session.stream, session.prompt, session.error, session.busy]);

  const submit = async () => {
    const prompt = draft.trim();
    if (!prompt || session.busy) return;
    follow.current = true;
    const explain = input.explain;
    onDraftChange({ text: "", explain: false });
    if (!await onSend(prompt, explain)) onDraftChange((current) => current.text ? current : { text: prompt, explain });
  };
  return <>
    <div className="assistant-messages" data-testid="assistant-messages" ref={messages} role="log" aria-label={t("assistant_conversation")} aria-live="polite"
      onScroll={(event) => { const el = event.currentTarget; follow.current = el.scrollHeight - el.scrollTop - el.clientHeight < 36; }}>
      {!conversation?.messages.length && !session.busy && !session.error && <p className="assistant-empty">{t("assistant_empty")}</p>}
      {conversation?.messages.map((message) => {
        // Restore feedback belongs to the original SQL change. Legacy restore
        // records remain in history, but must not become another undo action.
        if (message.role === "system" && message.content === "Previous SQL restored.") return null;
        const checkpoint = message.role === "assistant"
          ? conversation.checkpoints.find((item) => item.id === message.checkpointId) ?? conversation.checkpoints.find((item) => item.messageId === message.id)
          : undefined;
        const unapplied = message.sql && ((!checkpoint && message.status !== "applied" && message.sql !== conversation.sql) || session.unappliedMessageIds?.includes(message.id));
        return <div key={message.id} className={`assistant-message assistant-message-${message.role}`} data-message-id={message.id}>
          {message.content && (message.role === "assistant" && message.content !== "Reply interrupted. Try again."
            ? <AssistantMarkdown>{message.content}</AssistantMarkdown>
            : <div className="assistant-prose">{message.role === "system" || message.status === "interrupted" ? assistantStatusText(message.content) : message.content}</div>)}
          {unapplied && <div className="assistant-proposal">
            <pre>{message.sql}</pre>
            <button type="button" className="assistant-text-action" data-testid="assistant-apply" disabled={session.busy} onClick={() => onApply(message)}>{t("assistant_apply")}</button>
          </div>}
          {checkpoint && !unapplied && (checkpoint.beforeSql === currentSql
            ? <span className="assistant-restore-status" data-testid="assistant-restored">{t("assistant_restored")}</span>
            : <button type="button" className="assistant-text-action assistant-restore" data-testid="assistant-restore" disabled={session.busy} onClick={() => onRestore(checkpoint.id)}>
              <i className="ti ti-history" aria-hidden="true" /> {t("assistant_restore")}
            </button>)}
        </div>;
      })}
      {session.prompt && <div className="assistant-message assistant-message-user"><div className="assistant-prose">{session.prompt}</div></div>}
      {session.busy && <div className="assistant-message assistant-message-assistant" data-testid="assistant-stream">
        {session.stream ? <AssistantMarkdown>{session.stream}</AssistantMarkdown> : <span className="assistant-thinking">{t("assistant_thinking")}</span>}
      </div>}
      {session.notice && <p className="assistant-notice" role="status">{session.notice}</p>}
      {session.error && <p className="assistant-error" role="alert">{session.error}</p>}
    </div>
    <div className="assistant-compose">
      <div className="assistant-prompt-input">
        <textarea data-testid="assistant-input" aria-label={t("assistant_input")} placeholder={suggestExplanation ? "" : t("assistant_placeholder")} value={draft} rows={2}
          onChange={(event) => onDraftChange({ text: event.target.value, explain: false })}
          onCompositionStart={() => { composing.current = true; }} onCompositionEnd={() => { composing.current = false; }}
          onKeyDown={(event) => {
            if (event.key === "Tab" && !event.shiftKey && suggestExplanation && !event.nativeEvent.isComposing && !composing.current && event.nativeEvent.keyCode !== 229) {
              event.preventDefault();
              onDraftChange({ text: explanationPrompt, explain: true });
              return;
            }
            if (shouldSubmitAssistant({ key: event.key, shiftKey: event.shiftKey, isComposing: event.nativeEvent.isComposing || composing.current, keyCode: event.nativeEvent.keyCode })) {
              event.preventDefault(); void submit();
            }
          }} />
        {suggestExplanation && <span className="assistant-ghost" data-testid="assistant-ghost" aria-hidden="true">{explanationPrompt}</span>}
      </div>
    </div>
  </>;
}
