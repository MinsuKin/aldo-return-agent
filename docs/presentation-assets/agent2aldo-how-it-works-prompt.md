# Agent2ALDO diagram generation

Created with the built-in image-generation tool. Final asset: `agent2aldo-how-it-works.png`.

## Final prompt

Use case: infographic-diagram.
Create a polished, high-resolution 16:9 landscape diagram for a final hackathon presentation, suitable for placing directly in Figma Slides. Brand/project name is Agent2ALDO. This is a NEW image. Use the earlier white slide screenshots and the text-tree diagram only as conceptual style references, do not recreate a screenshot or code block.

Design: immaculate white background, near-black bold modern sans-serif typography similar to Geist, strong generous whitespace, sharp professional editorial diagram design, restrained vivid ALDO yellow (#FFE000), thin precise dark connectors with clear arrowheads. Tiny refined monochrome robot/assistant and database icons are welcome, but avoid cute large mascots or decorative illustrations. Excellent readable typography at projector distance, no tiny text. Flat frontal composition, not isometric. No shadows, gradients, dashboard widgets or colorful rainbow cards. White/black/yellow only. Keep the entire figure within generous margins. Render all supplied words accurately.

TITLE at top left: "How Agent2ALDO works"
SMALL SUBTITLE: "Fit advice at purchase. Learning after returns."

CONTENT: Clearly distinguish TWO reasoning agents and one non-agent gateway. Use a left-to-right composition with a large Personal Shopping Agent on the left, and an ALDO-controlled area on the right. Do not invent a third sales agent. The right-side ALDO area has two vertically arranged distinct destinations: a purchase-time gateway on top and a returns agent underneath.
LEFT: heading "Personal Shopping Agent", small robot outline, sublabel "Shopper memory + preferences". Under that, a small clearly labeled memory cylinder "Fit memory".
RIGHT AREA: modest heading "Agent2ALDO", and small overline "ALDO-controlled services".
RIGHT TOP NODE, highlighted vivid yellow: "ALDO Gateway". Sublabel "Fit evidence + stock". A smaller plain label below says "Data and business rules, not an agent".
RIGHT LOWER NODE, white with a crisp dark outline, robot outline: "ALDO Returns Agent". Sublabel "Policy-compliant resolutions".
Separate the purchase interaction and return interaction with whitespace and two short black section captions, not a complicated tangle.

PURCHASE CONNECTION in upper half:
A clean arrow from Personal Shopping Agent to ALDO Gateway labeled "1. Request fit advice".
A separate parallel return arrow from ALDO Gateway to Personal Shopping Agent labeled "2. Fit evidence + available options".
Under the arrows, a short text line "Shopping agent recommends a size".
A small distinct human-check icon and legible label "Customer confirms purchase" should conclude this upper interaction. Make clear that a size recommendation does not automatically place the order.

RETURN CONNECTION in lower half:
An arrow from the Personal Shopping Agent to the ALDO Returns Agent labeled "3. Hand off return request".
Near the Returns Agent a short human-check label "Customer confirms resolution".
A prominent clean yellow return loop with a dark arrowhead goes from ALDO Returns Agent along the lower part of the diagram back to the Fit memory cylinder on the left. Label the loop "4. Confirmed outcome updates memory". This means confirmed fit or preference outcome, not always a corrected size. No forced exchange: the resolution can be exchange or refund.

BOTTOM single clear statement in black: "Customer approves commitments. ALDO controls policy."
SMALL FOOTNOTE, still readable: "Prototype: internal workflows over HTTP. External personal-agent integration planned."

Accuracy constraints: only Personal Shopping Agent and ALDO Returns Agent are AI reasoning agents. ALDO Gateway is deterministic tools and services. Purchase-time reasoning happens in the shopping agent. Returns involve the second agent. Human approval is required before purchase and resolution. Memory write-back completes the loop. No Meta Muse logo, no PAP compliance badge, no claim of production Shopify integration. Avoid extra words, extra system nodes, fake metrics and arrows that cross through labels.
