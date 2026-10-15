import { useContext } from "react";
import { createPortal } from "react-dom";
import type { PushNote } from "../../hooks/useConversation";
import { Icon } from "../Icon";
import { BotAvatar } from "./Avatars";
import { PhoneScreenContext } from "./PhoneFrame";

/**
 * An iOS-style push notification that slides down inside the phone (for example "Delivered: ..."). It
 * lives in a polite status region, and the same message also lands in the chat, so nothing is lost
 * when it disappears.
 */
export function PushBanner({ note, onDismiss }: { note: PushNote | null; onDismiss: () => void }) {
  const screen = useContext(PhoneScreenContext);
  if (!screen) return null;
  return createPortal(
    <div className="push-region" role="status" aria-live="polite">
      {note && (
        <div key={note.id} className="push" data-testid="push-banner">
          <BotAvatar size={36} />
          <div className="push-text">
            <p className="push-title">
              <strong>{note.title}</strong>
              <span>now</span>
            </p>
            <p className="push-body">{note.body}</p>
          </div>
          <button type="button" className="icon-btn push-dismiss" onClick={onDismiss} aria-label="Dismiss notification">
            <Icon name="close" size={16} />
          </button>
        </div>
      )}
    </div>,
    screen,
  );
}
