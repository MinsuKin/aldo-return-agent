import type { ReactNode } from "react";

const ITEM_RE = /^\s*(\d{1,2})[.)]\s+(.*)$/;

/**
 * Plain text from the personal agent with paragraphs and numbered lists (for example the ten items of an order).
 * Never interprets HTML or markdown: text is rendered as text.
 */
export function RichText({ text }: { text: string }) {
  const blocks: ReactNode[] = [];
  let items: string[] = [];
  let start = 1;

  const flush = () => {
    if (items.length === 0) return;
    blocks.push(
      <ol key={`ol-${blocks.length}`} start={start} className="rich-list">
        {items.map((item, i) => (
          <li key={i}>{item}</li>
        ))}
      </ol>,
    );
    items = [];
  };

  for (const line of text.split(/\r?\n/)) {
    const m = ITEM_RE.exec(line);
    if (m) {
      if (items.length === 0) start = Number(m[1]);
      items.push(m[2]);
    } else {
      flush();
      if (line.trim()) blocks.push(<p key={`p-${blocks.length}`}>{line}</p>);
    }
  }
  flush();
  return <>{blocks}</>;
}
