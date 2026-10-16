# UI upgrade brief: personal agent dark workspace

Goal: take the look and feel of the team designer's prototype (`demo-dashboard/`) into the product UI (`frontend/`). **Design only**: no behaviour change. Five agents restyle disjoint files in parallel; this brief is the shared contract.

## Reference
- Designer's prototype: `demo-dashboard/styles.css` and `index.html` (static page). Screenshots: `docs/eval/screenshots/v2/design/ref/01-initial.png` (idle) and `04-return-end.png` (a finished run). Look at them with the read tool in Image mode.
- Product CSS lives in `frontend/src/styles/*.css`, imported in this order by `src/main.tsx`: `base, shell, components, phone, picker, chat, cards, sheets, trace, cases`.
- **Tokens: `frontend/src/styles/base.css`. Read it first.** Use only those tokens (`var(--panel)`, `var(--fg-muted)`, `var(--brand)` ...). The only hard-coded colours allowed are device-chrome gradients and shadows. Legacy light-theme names (`--surface`, `--text`, `--muted`, `--ink` ...) are aliases kept so unconverted rules never render undefined; do not use them in code you write, and convert every old light value in your files (`#fff`, `#f1f2f4`, `#e4e7eb`, ...).

## Design language to adopt
- Near-black surfaces on an elevation ramp (bg, panel, card, surface-2..5), hairline borders (`--border`, `--border-soft`), 10px radius panels, subtle shadow, header bars (surface-3) with an icon and a title.
- Geist (`var(--sans)`) and Geist Mono (`var(--mono)`, ids, numbers, chips, latency), 14px base, heading letter-spacing -0.01em, tabular numbers. Fonts are self-hosted (already imported).
- ONE primary action per screen/card in ALDO yellow (`background: var(--brand); color: var(--brand-ink)`); everything else is a neutral surface-3 button or a ghost button.
- Actor hues: Personal agent = violet, ALDO agent = orange, ALDO Data = blue, ALDO Store Desk = amber, System = neutral. Status: green done, cyan in progress, red error.
- User message = right bubble (`--surface-4`, 18px radius); personal agent message = plain flowing text (no bubble) like ChatGPT. "Thinking" = shimmering text.
- Motion only for entering and progress (keyframes `fadeUp popIn slideIn shimmer pulse` are in base.css). A global `prefers-reduced-motion` rule exists.

## Hard constraints (all tested)
1. Never remove or rename a `data-testid`, `role`, `aria-*` attribute or visible text that the Playwright specs assert. Read `frontend/e2e/demo.spec.ts`, `a11y.spec.ts`, `helpers.ts` for your area before editing.
2. Layout contracts: `data-testid=phone` keeps a 440:956 aspect ratio (tolerance 0.01) and is scaled to fit with NO page scroll at 1440x900; the phone is left of `data-testid=trace-panel`; the trace panel is taller than 0.9 x the phone; below 1100px they stack (`data-layout` on `data-testid=stage`); `data-testid=proto-banner` stays visible, `role=note`, text exactly `Prototype with simulated data`. The phone scale comes from `src/hooks/useFitScale.ts` (do not edit hooks).
3. Branding: never show `Returns Agent`, `Muse`, `Simulated data` (capital S) or `synthetic` in the phone or the trace. Actor names stay: `Personal agent`, `ALDO agent`, `ALDO Data`, `ALDO Store Desk`, `System`.
4. Accessibility: axe serious/critical = 0 (colour contrast!). `--fg-dim` is only for text on bg/panel/card/surface-2/surface-3; on surface-4/5 use `--fg-muted`. Visible yellow focus ring, targets at least 32px tall, keyboard path unchanged. An `aria-label` must START with the visible text of the control.
5. No new dependencies, no external requests (no CDN fonts or icons), no images. Icons: `src/components/Icon.tsx` or inline SVG.
6. TSX edits minimal and presentational (wrappers, class names, icons). Do not touch `src/hooks`, `src/lib`, `src/api`, `src/state`, `package.json`.
7. Edit ONLY the files your task assigns.

## Verify (every agent runs its OWN mock and dev server on its own ports)
```
cd frontend
(MOCK_PORT=<mock> MOCK_QUIET=1 MOCK_TIME_SCALE=0.25 nohup node mock/server.ts > "$KIROCREW_SCRATCH/mock<mock>.log" 2>&1 &)
(API_PROXY_TARGET=http://127.0.0.1:<mock> nohup npx vite --port <vite> --host 127.0.0.1 --strictPort > "$KIROCREW_SCRATCH/vite<vite>.log" 2>&1 &)
E2E_BASE_URL=http://127.0.0.1:<vite> MOCK_URL=http://127.0.0.1:<mock> \
  E2E_SHOT_DIR=/Users/minsu/Desktop/aldo-return-agent/docs/eval/screenshots/v2/design/<area> \
  npx playwright test e2e/demo.spec.ts e2e/a11y.spec.ts --output="$KIROCREW_SCRATCH/pw-<area>"
npx tsc -b
```
`a11y.spec.ts` saves a screenshot of every key state into `E2E_SHOT_DIR` and runs axe on 14 screens. ALL tests must pass with zero axe serious/critical (the David test's soft `li == 10` assertion is only for the live site; on the mock it passes). Read the screenshots with the read tool (Image mode) and iterate until your area matches the designer's feel. If node reports `EPERM lstat` on the scratch root, prefix the command with `TMPDIR=/Users/minsu/Desktop/aldo-return-agent/infra/build/cdk-tmp`. At the end stop ONLY the servers you started.

## Defects seen in the OLD UI (visual inspection of the deployed site, `docs/eval/screenshots/v2/visual/REPORT.md`): fix them in your area
- CHAT agent: David's chat-header sub line `David · Platinum member` is cut to `Platinum mem…` at every viewport (John's `Silver member` fits). Make the full text visible at the 440px design width (shrink gaps/padding, let the sub line use the remaining width, or wrap to two lines). Do not change the text.
- FRAME agent: at a 390x844 viewport the layout is stacked; check that the phone scales to the viewport width, the page scrolls normally and nothing is cut off. The old UI had no dark theme at all, so a `prefers-color-scheme` variant is not needed: the new theme is dark only.

## Rules
No git write operations of any kind. Never open or print `new.txt`, `aldo.txt`, `demo_plan.txt`. No secrets. Do not deploy. Do not call the deployed CloudFront site. Final message at most 25 lines: changes per file, tests run with counts, screenshots viewed, residual issues.
