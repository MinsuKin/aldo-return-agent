import { useState, type FormEvent, type RefObject } from "react";
import { MAX_MESSAGE_CHARS } from "../../config";
import type { ErrorInfo } from "../../lib/errors";
import { Icon } from "../Icon";

interface Props {
  inputRef: RefObject<HTMLInputElement | null>;
  busy: boolean;
  error: ErrorInfo | null;
  /** Resolves true when the message was accepted, so the box can clear. */
  onSend: (text: string) => Promise<boolean>;
}

/** The message row at the bottom of the phone: a rounded text box and a round send button. */
export function Composer({ inputRef, busy, error, onSend }: Props) {
  const [text, setText] = useState("");

  async function submit(e: FormEvent) {
    e.preventDefault();
    const value = text.trim();
    if (!value || busy) return;
    if (await onSend(value)) setText("");
  }

  return (
    <div className="composer-wrap">
      {error && (
        <p className="alert alert-error" role="alert" data-testid="send-error">
          {error.message}
        </p>
      )}
      <form className="composer" onSubmit={(e) => void submit(e)}>
        <label htmlFor="chat-input" className="sr-only">
          Message to your personal agent
        </label>
        <input
          id="chat-input"
          ref={inputRef}
          type="text"
          value={text}
          maxLength={MAX_MESSAGE_CHARS}
          placeholder="Message your personal agent"
          autoComplete="off"
          onChange={(e) => setText(e.target.value)}
          data-testid="chat-input"
        />
        <button type="submit" className="send-btn" aria-label="Send" disabled={busy || text.trim() === ""} data-testid="send">
          <Icon name="send" size={18} />
        </button>
      </form>
    </div>
  );
}
