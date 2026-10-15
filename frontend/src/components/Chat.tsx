import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { MAX_MESSAGE_CHARS } from "../config";
import type { ErrorInfo } from "../lib/errors";
import type { SuggestedPrompt } from "../lib/prompts";
import type { TranscriptEntry } from "../state/chat";

interface Props {
  /** Unique per instance: the app shows two chats at once. */
  idPrefix: "pa" | "aldo";
  /** Who the replies are from, announced to screen readers (for example "ALDO bot"). */
  speaker: string;
  title: string;
  subtitle?: string;
  inputLabel: string;
  placeholder: string;
  emptyHint: string;
  workingText: string;
  entries: TranscriptEntry[];
  suggestions: SuggestedPrompt[];
  runActive: boolean;
  sending: boolean;
  error: ErrorInfo | null;
  /** Resolves true when the message was accepted, so the composer can clear. */
  onSend: (text: string) => Promise<boolean>;
  onSuggest: (prompt: SuggestedPrompt) => void;
  onNewConversation?: () => void;
  canReset?: boolean;
}

export function Chat(props: Props) {
  const { idPrefix, speaker, title, subtitle, inputLabel, placeholder, emptyHint, workingText, entries, suggestions, runActive, sending, error } = props;
  const [text, setText] = useState("");
  const logRef = useRef<HTMLDivElement>(null);
  const stickRef = useRef(true);
  const busy = runActive || sending;
  const titleId = `${idPrefix}-chat-title`;
  const inputId = `${idPrefix}-message-input`;
  const helpId = `${idPrefix}-message-help`;

  useEffect(() => {
    const el = logRef.current;
    if (el && stickRef.current) el.scrollTop = el.scrollHeight;
  }, [entries.length, runActive]);

  function onScroll() {
    const el = logRef.current;
    if (el) stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  }

  async function submit(e?: FormEvent) {
    e?.preventDefault();
    const value = text.trim();
    if (!value || busy) return;
    if (await props.onSend(value)) setText("");
  }

  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      void submit();
    }
  }

  return (
    <section className="card chat" aria-labelledby={titleId} data-testid={`${idPrefix}-chat`}>
      <div className="card-head">
        <h3 id={titleId}>{title}</h3>
        {props.canReset && props.onNewConversation && (
          <button type="button" className="btn btn-link" onClick={props.onNewConversation} disabled={busy}>
            Start over
          </button>
        )}
      </div>
      {subtitle && <p className="muted card-sub">{subtitle}</p>}

      <div
        ref={logRef}
        onScroll={onScroll}
        className="transcript"
        role="log"
        aria-live="polite"
        aria-relevant="additions text"
        aria-label={`Conversation: ${title}`}
        tabIndex={0}
        data-testid={`${idPrefix}-transcript`}
      >
        {entries.length === 0 && <p className="muted empty">{emptyHint}</p>}
        {entries.map((e) => (
          <div key={e.key} className={`msg msg-${e.role}`} data-role={e.role}>
            <span className="sr-only">{e.role === "customer" ? "You: " : e.role === "assistant" ? `${speaker}: ` : "Action: "}</span>
            <span className="msg-text">{e.text}</span>
          </div>
        ))}
      </div>

      {runActive && (
        <p className="working" role="status" data-testid={`${idPrefix}-working`}>
          <span className="spinner" aria-hidden="true" /> {workingText}
        </p>
      )}

      <div role="group" aria-label={`Suggested prompts: ${title}`} className="suggestions">
        {suggestions.map((p) => (
          <button
            key={p.id}
            type="button"
            className="suggestion"
            disabled={busy}
            data-testid={`suggested-${p.id}`}
            onClick={() => props.onSuggest(p)}
          >
            <span className="suggestion-title">{p.title}</span>
            <span className="suggestion-text">{p.text}</span>
          </button>
        ))}
      </div>

      {error && (
        <p className="alert alert-error" role="alert" data-testid={`${idPrefix}-send-error`}>
          {error.message}
        </p>
      )}

      <form onSubmit={(e) => void submit(e)} className="composer">
        <label htmlFor={inputId}>{inputLabel}</label>
        <textarea
          id={inputId}
          value={text}
          maxLength={MAX_MESSAGE_CHARS}
          rows={2}
          placeholder={placeholder}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={onKeyDown}
          aria-describedby={helpId}
        />
        <div className="composer-row">
          <span id={helpId} className="muted">
            {text.length}/{MAX_MESSAGE_CHARS}. Enter sends, Shift+Enter adds a line.
          </span>
          <button type="submit" className="btn btn-primary" disabled={busy || text.trim() === ""}>
            Send
          </button>
        </div>
      </form>
    </section>
  );
}
