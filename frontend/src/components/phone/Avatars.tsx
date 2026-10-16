import aldoBot from "../../assets/aldo-bot.png";

/** The personal agent's avatar: the ALDO bot mascot. Decorative; the "Personal agent" text beside it names it. */
export function BotAvatar({ size = 40 }: { size?: number }) {
  return (
    <span className="bot-avatar" style={{ width: size, height: size }} aria-hidden="true">
      <img src={aldoBot} alt="" width={size} height={size} draggable={false} />
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
