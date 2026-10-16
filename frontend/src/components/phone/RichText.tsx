import type { ReactNode } from "react";
import { parseBlocks, type Inline } from "../../lib/markdown";

function renderInline(parts: Inline[]): ReactNode[] {
  return parts.map((p, i) => {
    switch (p.t) {
      case "strong":
        return <strong key={i}>{p.v}</strong>;
      case "em":
        return <em key={i}>{p.v}</em>;
      case "code":
        return (
          <code key={i} className="md-code">
            {p.v}
          </code>
        );
      default:
        return p.v;
    }
  });
}

/**
 * The personal agent's text as a small, safe markdown subset: paragraphs, bullet and numbered lists,
 * short headings, bold, italic and inline code (see lib/markdown.ts). Links, images, HTML and tables are
 * never interpreted: that text is rendered as text.
 */
export function RichText({ text }: { text: string }) {
  return (
    <>
      {parseBlocks(text).map((b, i) => {
        switch (b.t) {
          case "heading":
            return (
              <p key={i} className="md-heading">
                {renderInline(b.inline)}
              </p>
            );
          case "ul":
            return (
              <ul key={i} className="md-list">
                {b.items.map((item, j) => (
                  <li key={j}>{renderInline(item)}</li>
                ))}
              </ul>
            );
          case "ol":
            return (
              <ol key={i} start={b.start} className="md-list">
                {b.items.map((item, j) => (
                  <li key={j}>{renderInline(item)}</li>
                ))}
              </ol>
            );
          default:
            return <p key={i}>{renderInline(b.inline)}</p>;
        }
      })}
    </>
  );
}
