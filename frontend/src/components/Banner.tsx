import { BotAvatar } from "./phone/Avatars";

/**
 * The top bar: the mascot mark, the product name "Personal agent", and the prototype note shown as a
 * quiet pill with a small green dot. The pill keeps data-testid=proto-banner, role=note and the exact
 * text "Prototype with simulated data" (the layout and naming specs assert all three). The README and
 * responsible-AI notes carry the full disclosure.
 */
export function Banner() {
  return (
    <header className="topbar">
      <span className="topbar-brand">
        <BotAvatar size={28} />
        <span className="topbar-name">Personal agent</span>
      </span>
      <span className="proto-strip" role="note" data-testid="proto-banner">
        <span className="proto-dot" aria-hidden="true" />
        Prototype with simulated data
      </span>
    </header>
  );
}
