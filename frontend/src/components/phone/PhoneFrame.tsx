import { createContext, useState, type ReactNode } from "react";
import { PHONE_H, PHONE_W } from "../../hooks/useFitScale";

/** The phone screen element: sheets and the push banner render into it so they stay inside the phone. */
export const PhoneScreenContext = createContext<HTMLElement | null>(null);

function SignalIcon() {
  return (
    <svg width="18" height="12" viewBox="0 0 18 12" fill="currentColor" aria-hidden="true" focusable="false">
      <rect x="0" y="8" width="3" height="4" rx="1" />
      <rect x="5" y="5.5" width="3" height="6.5" rx="1" />
      <rect x="10" y="3" width="3" height="9" rx="1" />
      <rect x="15" y="0" width="3" height="12" rx="1" />
    </svg>
  );
}

function WifiIcon() {
  return (
    <svg width="17" height="12" viewBox="0 0 17 12" aria-hidden="true" focusable="false">
      <circle cx="8.5" cy="10.3" r="1.4" fill="currentColor" />
      <path d="M4.7 7.3a5.4 5.4 0 017.6 0" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" />
      <path d="M1.7 4.3a9.6 9.6 0 0113.6 0" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" />
    </svg>
  );
}

function BatteryIcon() {
  return (
    <svg width="27" height="13" viewBox="0 0 27 13" aria-hidden="true" focusable="false">
      <rect x="0.5" y="0.5" width="22" height="12" rx="3.5" fill="none" stroke="currentColor" opacity="0.45" />
      <rect x="2" y="2" width="19" height="9" rx="2.2" fill="currentColor" />
      <path d="M24.2 4.4v4.2c.8-.3 1.5-1.1 1.5-2.1s-.7-1.8-1.5-2.1z" fill="currentColor" opacity="0.5" />
    </svg>
  );
}

function StatusBar() {
  return (
    <div className="statusbar" aria-hidden="true">
      <span className="statusbar-time">9:41</span>
      <span className="statusbar-island" />
      <span className="statusbar-icons">
        <SignalIcon />
        <WifiIcon />
        <BatteryIcon />
      </span>
    </div>
  );
}

interface Props {
  /** Fit-to-stage scale; the layout box shrinks with it so nothing scrolls. */
  scale: number;
  /** A sheet is open: the rest of the phone is inert. */
  inert?: boolean;
  children: ReactNode;
}

/**
 * An iPhone 17 Pro Max style frame (440 x 956 CSS px before scaling): rounded bezel, dynamic island,
 * status bar with time, signal, wifi and battery, home indicator. Drawn with CSS and inline SVG only.
 */
export function PhoneFrame({ scale, inert, children }: Props) {
  const [screen, setScreen] = useState<HTMLDivElement | null>(null);

  return (
    <div className="phone-fit" style={{ width: PHONE_W * scale, height: PHONE_H * scale }}>
      <div className="phone" style={{ transform: `scale(${scale})` }} data-testid="phone" role="group" aria-label="Personal agent on a phone">
        <span className="phone-btn phone-btn-action" aria-hidden="true" />
        <span className="phone-btn phone-btn-vol-up" aria-hidden="true" />
        <span className="phone-btn phone-btn-vol-down" aria-hidden="true" />
        <span className="phone-btn phone-btn-power" aria-hidden="true" />
        <div className="phone-bezel">
          <div className="phone-screen" ref={setScreen}>
            <StatusBar />
            <PhoneScreenContext.Provider value={screen}>
              <div className="phone-content" inert={inert}>
                {children}
              </div>
            </PhoneScreenContext.Provider>
            <div className="home-indicator" aria-hidden="true" />
          </div>
        </div>
      </div>
    </div>
  );
}
