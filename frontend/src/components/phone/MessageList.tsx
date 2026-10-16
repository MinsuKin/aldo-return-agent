import { useLayoutEffect, useRef, type ReactNode } from "react";
import { useStickToEnd } from "../../hooks/useStickToEnd";
import type { CardRef, Entry } from "../../state/conversation";
import { RichText } from "./RichText";

interface Props {
  entries: Entry[];
  /** Shown as the first personal agent bubble; written by the client, not by the server. */
  greeting: string;
  /** Label of the typing indicator while the personal agent is working, or null when it is idle. */
  typing: string | null;
  renderCard: (card: CardRef, isLatest: boolean) => ReactNode;
  /** Changes whenever the owner acts (sends a message, confirms): the transcript then follows to the end again. */
  followKey: number;
}

const cardKind = (c: CardRef): string => c.type;

/** Sitting at the top of a tall card we scrolled to counts as following too. */
function atTopOfLastCard(el: HTMLElement): boolean {
  const last = el.querySelector<HTMLElement>('[data-last="true"]');
  return !!last && last.dataset.kind === "card" && Math.abs(el.scrollTop - (last.offsetTop - 8)) < 40;
}

/**
 * The transcript: the owner's messages on the right, the personal agent's replies and inline cards on the left.
 * It is the polite live region of the phone, so new replies are announced; cards hide their ticking
 * timers from assistive technology.
 */
export function MessageList({ entries, greeting, typing, renderCard, followKey }: Props) {
  const logRef = useRef<HTMLDivElement>(null);
  const innerRef = useRef<HTMLDivElement>(null);
  const { stick, scrollTo, onScroll } = useStickToEnd(logRef, 80, atTopOfLastCard);

  const latest = new Map<string, number>();
  entries.forEach((e, i) => {
    if (e.kind === "card") latest.set(cardKind(e.card), i);
  });
  const lastIndex = entries.length - 1;

  const follow = () => {
    const el = logRef.current;
    if (!el || !stick.current) return;
    const last = el.querySelector<HTMLElement>('[data-last="true"]');
    if (last && last.dataset.kind === "card" && last.offsetHeight > el.clientHeight - 24) scrollTo(last.offsetTop - 8);
    else scrollTo();
  };

  useLayoutEffect(follow, [entries.length, typing]);
  // The owner acted (even from a card higher up): show what happens next.
  useLayoutEffect(() => {
    stick.current = true;
    follow();
  }, [followKey]);
  useLayoutEffect(() => {
    const inner = innerRef.current;
    if (!inner) return;
    const observer = new ResizeObserver(follow);
    observer.observe(inner);
    return () => observer.disconnect();
  }, []);

  return (
    <div
      ref={logRef}
      className="chat-log"
      role="log"
      aria-live="polite"
      aria-relevant="additions text"
      aria-label="Conversation with your personal agent"
      tabIndex={0}
      onScroll={() => void onScroll()}
      data-testid="chat-log"
    >
      <div ref={innerRef} className="chat-log-inner">
        <div className="row row-bot" data-testid="greeting">
          <div className="bubble bubble-bot">
            <span className="sr-only">Personal agent: </span>
            <RichText text={greeting} />
          </div>
        </div>

        {entries.map((e, i) => {
          const last = !typing && i === lastIndex;
          const marks = { "data-last": last ? "true" : undefined, "data-kind": e.kind === "card" ? "card" : e.kind };
          switch (e.kind) {
            case "user":
              return (
                <div key={e.key} className="row row-user" {...marks} data-testid="bubble-user">
                  <div className="bubble bubble-user">
                    <span className="sr-only">You: </span>
                    {e.text}
                  </div>
                </div>
              );
            case "bot":
              return (
                <div key={e.key} className="row row-bot" {...marks} data-testid="bubble-bot">
                  <div className="bubble bubble-bot">
                    <span className="sr-only">Personal agent: </span>
                    <RichText text={e.text} />
                  </div>
                </div>
              );
            case "notice":
              return (
                <div key={e.key} className="row row-notice" {...marks} data-testid="notice">
                  <p className="notice">{e.text}</p>
                </div>
              );
            case "card":
              return (
                <div key={e.key} className="row row-card" {...marks}>
                  {renderCard(e.card, latest.get(cardKind(e.card)) === i)}
                </div>
              );
          }
        })}

        {typing && (
          <div className="row row-bot" data-last="true" data-kind="typing" data-testid="typing">
            <div className="typing" role="status">
              <span className="thinking-text" aria-hidden="true">
                Thinking
              </span>
              <span className="sr-only">{typing}</span>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
