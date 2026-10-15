/**
 * One small neutral strip outside the phone, always visible (user decision, addendum 12.2). It replaces
 * the yellow banner; the README and responsible-AI notes carry the full disclosure.
 */
export function Banner() {
  return (
    <div className="proto-strip" role="note" data-testid="proto-banner">
      Prototype with simulated data
    </div>
  );
}
