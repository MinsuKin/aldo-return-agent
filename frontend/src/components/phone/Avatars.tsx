/** ALDO bot's avatar: a small chat-bubble glyph, drawn here (no logos or external assets). */
export function BotAvatar({ size = 40 }: { size?: number }) {
  return (
    <span className="bot-avatar" style={{ width: size, height: size }} aria-hidden="true">
      <svg viewBox="0 0 40 40" width={size * 0.62} height={size * 0.62} focusable="false">
        <path d="M10 10h20a4 4 0 014 4v10a4 4 0 01-4 4H20l-7 6v-6h-3a4 4 0 01-4-4V14a4 4 0 014-4z" fill="#ffffff" />
        <circle cx="16" cy="19" r="1.9" fill="#15181c" />
        <circle cx="24" cy="19" r="1.9" fill="#15181c" />
      </svg>
    </span>
  );
}

/** A customer's initial in a quiet circle. */
export function PersonaAvatar({ name, size = 44 }: { name: string; size?: number }) {
  return (
    <span className="persona-avatar" style={{ width: size, height: size, fontSize: size * 0.4 }} aria-hidden="true">
      {name.trim().charAt(0).toUpperCase()}
    </span>
  );
}
