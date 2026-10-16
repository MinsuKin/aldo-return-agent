import aldoBot from "../../assets/aldo-bot.png";
import johnPhoto from "../../assets/john.jpg";
import davidPhoto from "../../assets/david.jpg";

/** The personal agent's avatar: the mascot. Decorative; the "Personal agent" text beside it names it. */
export function BotAvatar({ size = 40 }: { size?: number }) {
  return (
    <span className="bot-avatar" style={{ width: size, height: size }} aria-hidden="true">
      <img src={aldoBot} alt="" width={size} height={size} draggable={false} />
    </span>
  );
}

/** Profile photos for specific demo customers, keyed by customer id. Others fall back to the initial. */
const PERSONA_PHOTOS: Record<string, string> = {
  "DEMO-CUST-JOHN": johnPhoto,
  "DEMO-CUST-DAVID": davidPhoto,
};

/** A customer's profile photo when we have one, otherwise their initial in a quiet circle. */
export function PersonaAvatar({ name, customerId, size = 44 }: { name: string; customerId?: string; size?: number }) {
  const photo = customerId ? PERSONA_PHOTOS[customerId] : undefined;
  if (photo) {
    return (
      <span className="persona-avatar persona-avatar-photo" style={{ width: size, height: size }} aria-hidden="true">
        <img src={photo} alt="" width={size} height={size} draggable={false} />
      </span>
    );
  }
  return (
    <span className="persona-avatar" style={{ width: size, height: size, fontSize: size * 0.4 }} aria-hidden="true">
      {name.trim().charAt(0).toUpperCase()}
    </span>
  );
}
