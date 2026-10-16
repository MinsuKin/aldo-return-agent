/**
 * A deliberately small markdown subset for the personal agent's chat text.
 *
 * Supported: paragraphs, "-", "*" and bullet-dot lists, numbered lists, "#" headings (all shown at one
 * small size), **bold**, *italic* and `code`.
 *
 * Not supported on purpose: links, images, raw HTML and tables. The text comes from a model that reads
 * third-party content (product reviews), so a link or image in a reply could be an exfiltration or
 * phishing vector. Those characters simply stay as literal text. The parser returns plain data and the
 * renderer builds React elements from it, so nothing is ever injected as HTML.
 */

export type Inline = { t: "text" | "strong" | "em" | "code"; v: string };

export type Block =
  | { t: "p"; inline: Inline[] }
  | { t: "heading"; inline: Inline[] }
  | { t: "ul"; items: Inline[][] }
  | { t: "ol"; start: number; items: Inline[][] };

const BULLET_RE = /^\s*[-*\u2022]\s+(.*)$/;
const ORDERED_RE = /^\s*(\d{1,2})[.)]\s+(.*)$/;
const HEADING_RE = /^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$/;
const RULE_RE = /^\s*([-*_])(?:\s*\1){2,}\s*$/;
// **bold**, *italic*, `code`. The text inside must not start or end with a space, so "5 * 3 * 2" is left alone.
const INLINE_RE = /\*\*([^*\s](?:[^*]*[^*\s])?)\*\*|\*([^*\s](?:[^*]*[^*\s])?)\*|`([^`]+)`/g;

export function parseInline(text: string): Inline[] {
  const out: Inline[] = [];
  let last = 0;
  for (const m of text.matchAll(INLINE_RE)) {
    const at = m.index ?? 0;
    if (at > last) out.push({ t: "text", v: text.slice(last, at) });
    if (m[1] !== undefined) out.push({ t: "strong", v: m[1] });
    else if (m[2] !== undefined) out.push({ t: "em", v: m[2] });
    else out.push({ t: "code", v: m[3] ?? "" });
    last = at + m[0].length;
  }
  if (last < text.length) out.push({ t: "text", v: text.slice(last) });
  return out;
}

/** Each non-empty line outside a list is its own paragraph, as the agents write one sentence group per line. */
export function parseBlocks(text: string): Block[] {
  const blocks: Block[] = [];
  let list: { kind: "ul" | "ol"; start: number; items: Inline[][] } | null = null;

  const flush = () => {
    if (!list) return;
    blocks.push(list.kind === "ul" ? { t: "ul", items: list.items } : { t: "ol", start: list.start, items: list.items });
    list = null;
  };

  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trimEnd();
    if (!line.trim()) continue; // a blank line inside a list does not end it
    if (RULE_RE.test(line)) {
      flush();
      continue;
    }
    const heading = HEADING_RE.exec(line);
    if (heading) {
      flush();
      if (heading[1]) blocks.push({ t: "heading", inline: parseInline(heading[1]) });
      continue;
    }
    const ordered = ORDERED_RE.exec(line);
    if (ordered) {
      if (!list || list.kind !== "ol") {
        flush();
        list = { kind: "ol", start: Number(ordered[1]), items: [] };
      }
      list.items.push(parseInline(ordered[2] ?? ""));
      continue;
    }
    const bullet = BULLET_RE.exec(line);
    if (bullet) {
      if (!list || list.kind !== "ul") {
        flush();
        list = { kind: "ul", start: 1, items: [] };
      }
      list.items.push(parseInline(bullet[1] ?? ""));
      continue;
    }
    flush();
    blocks.push({ t: "p", inline: parseInline(line.trim()) });
  }
  flush();
  return blocks;
}
