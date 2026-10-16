# Visual inspection of the redesigned UI (deployed site, static screens)

Site: https://d38111d70jquli.cloudfront.net (bundle `index-52TPgxIl.js` / `index-CwwmClWm.css`). Spec: `frontend/e2e/visual.spec.ts`, load-only (no message sent, nothing confirmed, no reset).
These 20 PNGs are the **after** set. The same screens of the old light UI are in `../design/before/` (its report: `../design/before/REPORT.md`). The designer's reference: `../design/ref/`.

Matrix: 5 screens (persona picker, John chat, David chat, admin picker `?admin=1`, Cases) x 1440x900 light, 1440x900 dark, 1000x900 (stacked), 390x844 (phone). The theme is dark only, so every `*-dark.png` is identical to its `*-light.png`: the matrix stays so the file names match the old set.

## What was looked at
Opened and read in full: `persona-picker-1440x900-light`, `john-chat-1440x900-light`, `david-chat-390x844-light`, `cases-390x844-light`, `admin-picker-390x844-light`. The other variants come from the same code and were checked in the mock run (`../design/after/visual/`): persona picker 390, cases 1440, admin picker 1440, John chat 1000. The flow screens (`../a11y-*.png`, 9 states incl. the ten-item list, the quote with other options, the confirmed bundle and the delivered push banner) were read too.

## Result
| Screen | Seen | Verdict |
| --- | --- | --- |
| Persona picker 1440 | ALDO bot mark in the top bar, striped panel, titanium phone, John/David rows with tier pills, A2A trace panel "Waiting" | OK |
| John chat 1440 | yellow avatar, violet Purchase pill, plain bot text, quick-reply card with a 2-line clamp, pill composer with the dimmed send button, trace rows with neutral background and coloured actor chips | OK |
| David chat 390 | sub line `David · Platinum member` is shown in full (the old UI cut it to `Platinum mem…`), phone fills the width | OK |
| Cases 390 / 1440 | dark card, link, muted copy, empty state | OK |
| Admin picker 390 / 1440 | regression customers and the amber operator panel, reset buttons readable | OK |
| Stacked 1000 | phone panel on top, trace below, page scrolls | OK |

## Defects found while reviewing, all fixed before the deploy
1. Trace rows were tinted in the actor colour and the chip sat on a second tint (the `actor-*` hue rules also matched the row): axe `color-contrast` 4.35:1 on the Purchase chip and on the latency text. The rules are now scoped to `.actor-chip`; 15 axe scans give 0 serious/critical and 0 moderate/minor.
2. The recommended option had a muddy olive fill (14% yellow): now a 6% warm tint with the yellow ring.
3. The first-screen avatar was violet while the top bar, chat header and push icon are ALDO yellow: now one brand colour.
4. The push banner let the header text ghost through in a browser without backdrop blur: opacity .82 -> .92.
5. (from the old UI) David's header sub line was truncated: fixed by the chat restyle.

## Observations, not changed (outside a design-only change)
- Signing in again as the same customer shows the previous run's events in the trace (status "Done") next to a fresh chat, until the demo is reset. This is the existing behaviour (the A2A stream belongs to the customer); reset before every demo.
- The 390px layout is stacked, so the trace is below the phone and needs a scroll.
