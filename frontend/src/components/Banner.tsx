/**
 * The top bar: a rounded brand mark, the product name "Personal agent", and the prototype note shown as a
 * quiet pill with a small green dot. The pill keeps data-testid=proto-banner, role=note and the exact
 * text "Prototype with simulated data" (the layout and naming specs assert all three). The README and
 * responsible-AI notes carry the full disclosure.
 */
export function Banner() {
  return (
    <header className="topbar">
      <span className="topbar-brand">
        <span className="topbar-mark" aria-hidden="true">
          <svg viewBox="0 0 24 24" width="17" height="17" fill="none" focusable="false">
            <path
              d="M5 6h11a3 3 0 013 3v6a3 3 0 01-3 3h-5l-4 3v-3H5a3 3 0 01-3-3V9a3 3 0 013-3z"
              fill="currentColor"
            />
            <circle cx="9" cy="12" r="1.3" fill="var(--brand)" />
            <circle cx="14" cy="12" r="1.3" fill="var(--brand)" />
          </svg>
        </span>
        <span className="topbar-name">Personal agent</span>
      </span>
      <span className="proto-strip" role="note" data-testid="proto-banner">
        <span className="proto-dot" aria-hidden="true" />
        Prototype with simulated data
      </span>
    </header>
  );
}
