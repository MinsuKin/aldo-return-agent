import type { QuickReply } from "../../lib/script";

interface Props {
  replies: QuickReply[];
  disabled: boolean;
  onPick: (reply: QuickReply) => void;
}

/** The next line(s) of the demo script as tappable chips (addendum 11.2). The presenter can still type anything. */
export function QuickReplies({ replies, disabled, onPick }: Props) {
  if (replies.length === 0) return null;
  return (
    <div role="group" aria-label="Suggested replies" className="quick" data-testid="quick-replies">
      {replies.map((r) => (
        <button key={r.id} type="button" className="quick-chip" disabled={disabled} onClick={() => onPick(r)} data-testid={`quick-${r.id}`}>
          <span className="quick-title">{r.title}</span>
          <span className="quick-text">{r.text}</span>
        </button>
      ))}
    </div>
  );
}
