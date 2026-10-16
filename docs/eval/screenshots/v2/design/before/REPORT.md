> **Old UI (light theme, before the redesign).** Kept for the before/after comparison. The current report is `../../visual/REPORT.md`. Defects 1 and 2 below are fixed (the theme is dark only, David's sub line is shown in full); defect 3 (content below the fold at 390px) is the stacked layout, which scrolls by design.

# Visual inspection — deployed site static screens (v2)

Source: https://d38111d70jquli.cloudfront.net (load-only; no messages sent, nothing confirmed, demo never reset).
Spec: `frontend/e2e/visual.spec.ts`. 20 PNGs in this folder: 5 screens × {1440×900 light, 1440×900 dark, 1000×900 light (stacked), 390×844 light (phone)}.
Every PNG below was opened and read in image mode.

## What each screenshot shows

### persona-picker
- **1440×900-light / 1440×900-dark** — Two-pane stage: phone on the left with "Who is using ALDO bot?", John (Silver, Downtown Montreal) and David (Platinum, Rural New Brunswick) cards; A2A trace on the right in the "Waiting" state with the actor legend and the "public fields only" note. Dark is identical to light.
- **1000×900-light** — Stacked layout: only the phone top is in view (trace is below the fold); picker renders cleanly, larger type.
- **390×844-light** — Phone-only, both persona cards fit; a faint sliver of a card peeks at the very bottom edge of the phone frame.

### john-chat (signed in, empty)
- **1440×900-light / dark** — Header "ALDO bot / John · Silver member", "Purchase" workflow pill, DB icon; greeting bubble; a quick-reply suggestion ("Buy the Men 001 in my size") and the "Message ALDO bot" composer. Trace panel shows "Idle / No events yet". Dark identical to light.
- **1000×900-light** — Stacked; phone header + greeting visible, trace below the fold.
- **390×844-light** — Greeting, suggestion card and composer fit; faint sliver peeks below the phone frame bottom.

### david-chat (signed in, empty)
- **1440×900-light / dark** — Same as John but header subtitle reads "David · Platinum mem…" (truncated). Greeting addressed to David; suggestion "Return 3 of the 10 items". Dark identical to light.
- **1000×900-light** — Stacked; subtitle "David · Platinum mem…" truncated in the phone header.
- **390×844-light** — Subtitle truncated to "Platinum mem…"; content otherwise fits; sliver below the frame.

### admin-picker (`/?admin=1`)
- **1440×900-light / dark** — John and David plus the "REGRESSION CUSTOMERS" group (Ava, Ben, Chloe) and the yellow "Demo controls (admin)" panel with the Admin-secret field and the three Reset buttons, inside the phone's own scroll area. Dark identical to light.
- **1000×900-light** — Stacked; the regression list scrolls within the phone, admin panel partly below the phone fold.
- **390×844-light** — All rows + admin panel fit by scrolling the phone; faint sliver below the frame.

### cases (`/#/cases`)
- **1440×900-light / dark** — Full-width card: "Back to ALDO bot" link, "Cases" heading, read-only description, "No cases yet" empty state. Dark identical to light.
- **1000×900-light** — Same, slightly narrower card.
- **390×844-light** — Card reflows; description and empty-state wrap cleanly, no overflow.

## Visual defects

1. **Dark mode is not implemented** — `*-1440x900-dark.png` for ALL five screens is pixel-identical to the light screenshot (verified across persona-picker, john-chat, david-chat, admin-picker, cases). The app does not respond to `prefers-color-scheme: dark`. On a dark-themed projector/browser the demo will show as full light. *Element:* whole app theme. *Fix area:* add a dark theme (CSS custom-property overrides under `@media (prefers-color-scheme: dark)` in `frontend/src/stage.css`), or state explicitly that light-only is intended.

2. **David's chat-header subtitle is truncated** — `david-chat-1440x900-light.png`, `-1440x900-dark.png`, `-1000x900-light.png`, `-390x844-light.png`: the header subtitle shows "David · Platinum mem…" with an ellipsis; "Platinum member" is cut off. John's shorter "Silver member" fits, so the clip is length-driven, not viewport-driven (present even at 1440px). *Element:* chat header subtitle (`chat-sub`). *Fix area:* the chat-header subtitle rule — widen its max-width / allow it to use the header's spare width, or reduce the trailing icon/pill spacing, in the chat-header component CSS or `frontend/src/stage.css`.

3. **Phone frame overflows ~a few px below the 390×844 viewport (minor)** — `persona-picker-390x844-light.png`, `john-chat-390x844-light.png`, `david-chat-390x844-light.png`, `admin-picker-390x844-light.png`: a faint partial card/edge is visible at the very bottom of the phone frame, i.e. the phone bezel's bottom radius sits just past the 844px fold. Cosmetic — not a content clip — but on a 390-tall phone the frame bottom is not fully shown. *Element:* phone frame / stage wrapper min-height at narrow heights. *Fix area:* the phone-frame sizing in `frontend/src/stage.css` (clamp phone height to the viewport at phone sizes).

## No defect seen
- **persona-picker** at 1440/1000/390 (light): correct, readable, tier badges legible — no defect.
- **admin-picker** at 1440/1000/390 (light): regression group, admin panel, secret field and Reset buttons all legible and correctly contained in the phone scroll area; the Admin-secret field is empty (no secret shown) — no defect.
- **cases** at all four settings: layout reflows cleanly down to 390px, text wraps, "Back to ALDO bot" link contrast adequate — no defect.
- **john-chat** at all settings: greeting, pill, suggestion card and composer all correct — no defect (John's subtitle fits).
- **Stacked layout (1000×900)**: behaves as designed — phone on top, trace below the fold; no overlap or broken layout.
