---
name: Conciliador bancario
description: Day-to-day bank reconciliation tool (bank statements vs. the FBS ledger); shares its visual identity with the group hub
colors:
  acento: "#38bdf8"
  acento-claro: "#0a6fb5"
  texto-sobre-acento: "#06131f"
  fondo1: "#070b18"
  fondo2: "#0d1430"
  panel: "rgba(255, 255, 255, 0.055)"
  panel-2: "rgba(255, 255, 255, 0.04)"
  campo: "rgba(255, 255, 255, 0.05)"
  border: "rgba(255, 255, 255, 0.12)"
  texto: "#e8ecf8"
  texto2: "#94a0c2"
  rojo: "#ff8896"
  verde: "#34d399"
  ambar: "#fbbf24"
  violeta: "#c4b5fd"
  th-bg: "#10173a"
  velo: "rgba(4, 7, 18, 0.7)"
  dialogo: "#0e1637"
  hueco: "rgba(0, 0, 0, 0.16)"
  demo-franja: "#f59e0b"
  demo-tinta: "#1a1006"
  claro-fondo: "#e9edf4"
  claro-texto: "#17203a"
  claro-texto2: "#5b6880"
  claro-rojo: "#c0313d"
  claro-verde: "#0b7a54"
  claro-ambar: "#8a5a00"
  claro-violeta: "#6d4fc4"
  claro-th-bg: "#e9edf5"
typography:
  display:
    fontFamily: "Sora, sans-serif"
    fontSize: "16px"
    fontWeight: 700
    lineHeight: 1.2
    letterSpacing: "0.04em"
  kpi:
    fontFamily: "Sora, sans-serif"
    fontSize: "34px"
    fontWeight: 800
    lineHeight: 1.1
    letterSpacing: "normal"
  body:
    fontFamily: "Inter, system-ui, sans-serif"
    fontSize: "13px"
    fontWeight: 400
    lineHeight: 1.45
    letterSpacing: "normal"
  small:
    fontFamily: "Inter, system-ui, sans-serif"
    fontSize: "12px"
    fontWeight: 400
    lineHeight: 1.45
    letterSpacing: "normal"
  label:
    fontFamily: "Inter, system-ui, sans-serif"
    fontSize: "11px"
    fontWeight: 500
    lineHeight: 1.3
    letterSpacing: "0.08em"
rounded:
  xs: "4px"
  sm: "8px"
  md: "10px"
  lg: "12px"
  xl: "16px"
  modal: "20px"
  pill: "999px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "14px"
  lg: "22px"
  xl: "28px"
components:
  button-primary:
    backgroundColor: "{colors.acento}"
    textColor: "{colors.texto-sobre-acento}"
    typography: "{typography.body}"
    rounded: "{rounded.md}"
    padding: "12px 26px"
    height: "44px"
  button-secondary:
    backgroundColor: "{colors.panel-2}"
    textColor: "{colors.texto}"
    typography: "{typography.body}"
    rounded: "{rounded.md}"
    padding: "9px 16px"
    height: "40px"
  button-mini:
    backgroundColor: "{colors.verde}"
    textColor: "{colors.texto-sobre-acento}"
    typography: "{typography.small}"
    rounded: "{rounded.sm}"
    padding: "5px 13px"
  card:
    backgroundColor: "{colors.panel}"
    rounded: "{rounded.xl}"
    padding: "{spacing.lg}"
  input:
    backgroundColor: "{colors.campo}"
    textColor: "{colors.texto}"
    typography: "{typography.body}"
    rounded: "{rounded.sm}"
    padding: "8px 12px"
  chip:
    typography: "{typography.label}"
    rounded: "{rounded.pill}"
    padding: "1px 8px"
  table-header:
    backgroundColor: "{colors.th-bg}"
    textColor: "{colors.texto2}"
    typography: "{typography.label}"
    padding: "8px 10px"
---

## Overview

A work interface for accountants: upload statements, pull ledger E and O from FBS (or upload it), review the
matching, resolve by hand what was left pending and export. **Operate** mode: fast scanning of dense tables,
aligned amounts, states told apart by color and by text. The identity is the group hub's: night-blue background
with a faint grid, translucent panels, a single sky-blue accent, Sora for titles and figures and Inter for
everything else. A switchable light theme with the same structure.

Every token lives in the `:root` block of each page and is redefined in `body.claro`. Both files in `static/` must
keep the same token block; if one is added, it goes in both.

## Colors

- **One accent**: `--acento` for primary actions, focus, links, the active tab and "extracto" chips. Not used for
  body text.
- **Semantic**: `--verde` reconciled and zero difference, `--ambar` pending confirmation and simulation notices,
  `--rojo` unbooked, errors and destructive actions (`.peligro`), `--violeta` "FBS mayor" chips. Never color alone:
  always paired with text or a label.
- **Neutrals**: `--texto` for body, `--texto2` for secondary text, labels and table headers. `--panel` and
  `--panel-2` for surfaces, `--campo` for inputs, `--hueco` for sunken lists, `--dialogo` for the body of dialogs
  and toasts, `--border` for every border.
- **Derived transparencies** of the accent or a semantic color with
  `color-mix(in srgb, var(--token) N%, transparent)`. Never a hand-written `rgba()` of a brand color.
- **Contrast**: every text/background pair is above 4.5:1 in both themes. In the light theme the semantic colors
  are dark versions (`claro-*`), because the dark-theme ones don't reach it on white.
- **Text on accent** is dark in the dark theme and white in the light theme (`--texto-sobre-acento`,
  `--texto-sobre-verde`). The top of the gradients (`--acento-tope`, `--verde-tope`) lightens in dark and darkens
  in light so contrast isn't lost.
- **Exception**: the demo strip (`.demo-banner`) uses a fixed amber and ink in both themes because it is a warning
  signal, not part of the palette.

## Typography

Six-step scale in `--fs-*` tokens. No pixel sizes are written outside the tokens.

| Token | Size | Use |
|---|---|---|
| `--fs-xs` | 11px | uppercase labels, chips, table headers, metadata |
| `--fs-sm` | 12px | tables, list items, secondary explanatory text |
| `--fs-md` | 13px | body, secondary buttons, inputs |
| `--fs-lg` | 14px | primary button, upload zone titles |
| `--fs-xl` | 16px | h1, dialog titles |
| block figure | 34px | the big figure of "Extracto explicado" and "Falta resolver" |

Sora 700/800 for h1, card h3 and figures. Inter 400/500/600 for the rest. Uppercase labels get
`letter-spacing: 0.08em`. Amounts always with `font-variant-numeric: tabular-nums` and right-aligned. Body with
`line-height: 1.45`.

## Layout

- `main` at most 1200px wide with a 20px side margin, 16px on phones.
- Sticky header on desktop, static on phones (under 720px) so it doesn't eat the screen; its actions live in
  `.zona-derecha` and wrap into two columns.
- Two-column grids (`.grid-carga`, `.dual`) collapse to one under 760px and 900px. KPIs with
  `auto-fit, minmax(150px, 1fr)`.
- Wide tables live inside `.contenedor-tabla` with their own horizontal scroll and a sticky header; the page never
  scrolls sideways.
- Touch targets: at least 40px tall for buttons, 44px for the primary action and the tabs, and 44px for every
  control under `pointer: coarse`.

## Elevation & Depth

Depth through transparency and borders, not shadows. Panels: `--panel` over the background with a `--border`
border. Only the header (14px blur), the dialogs (`--velo` veil with 8px blur and `--sombra-modal` shadow) and the
toasts float. No colored halos: the logo has a ring of the accent at 22%, buttons have no shadow.

## Shapes

Radii: 4px for the progress bar, 8px for inputs, rectangular chips and row buttons, 10px for buttons and lists,
12px for upload zones, KPIs and bars, 16px for cards and panels, 20px for dialogs, pill for chips and counters.
Upload zones with a 1px dashed border.

## Components

- **Upload zone** (`.zona` with `.zona-cta` inside): the title-plus-text block is the `role="button"` with
  `tabindex="0"`; Enter or Space open the file picker. Nested controls (FBS account and dates in the monthly page)
  stay outside the role. `arrastrando` state and hover with the accent's border and background.
- **Primary button** (`button.principal`): short vertical gradient of the accent, `--texto-sobre-acento` text.
  Disabled: gray with `--texto2`.
- **Secondary button** (`button.secundario`, `a.secundario`, `a.volver`): `--panel-2` background, `--border`
  border. Variants `.btn-chico` (34px) and `.btn-fila` (32px) for toolbars and cells. `.peligro` for destructive
  actions: red at 10% with red text.
- **Mini button** (`button.mini`): green, to confirm inside a panel or dialog.
- **Dialogs** (`.modal` + `.modal-card`, `.eq-card` or `.tarjeta`): `role="dialog"`, `aria-modal`,
  `aria-labelledby` pointing to the title. Opened with `openModal(id, focus)` and closed with `closeModal(id)`;
  Escape presses the button marked `data-cerrar`, Tab is trapped and focus returns to the trigger. The progress
  overlay is a dialog that can't be closed until it finishes or fails.
- **Confirmation** (`askConfirm({ titulo, texto, ok, peligro })`): returns a promise with `true` or `false`;
  initial focus on Cancelar; with `peligro` the action button is red. Replaces `confirm()`.
- **Toasts** (`notify(msg, 'error' | 'info')`): stack at the bottom center; errors have `role="alert"` and stay
  until closed, info toasts go away by themselves. They replace `alert()` and the clipboard `prompt()`.
- **Tabs** (`nav.tabs`): `role="tablist"`, `role="tab"` buttons with `aria-selected` and roving focus; arrows and
  Home/End switch tabs. Panels are `role="tabpanel"`.
- **Result summary** (`.resumen` with three `.bloque`): Extracto explicado, Falta resolver (the protagonist, with a
  red or green border depending on whether anything is pending, and the "Resolver a mano" button) and Esta corrida.
  One big figure per block; the rest as a name/value list. The informative notes of the run are folded in
  `details.notas-corrida`; only errors and brand warnings stay visible.
- **Tables**: uppercase `th scope="col"` at `--fs-xs`, 12px rows with a 6% separator. Numeric cells `.num`.
- **Chips** (`.chip`, `.metodo`, `.pill-an`, `.badge`): pill with the color at 12% as background and a 35–40%
  border.
- **Glossary** (`details.glosario`): a visible explanation of columns and states under each daily-mode table, with
  a CSS-drawn chevron. The headers' `title` is a reinforcement, not the only source.
- **Live regions**: `#estado`, `#estadoCarga`, `#estadoConciliar`, `#fbsSqlMsj`, `#fbsConfMsj`, `#ovFase` with
  `role="status"`; errors with `role="alert"`.

## Do's and Don'ts

- Use the tokens; if a value doesn't exist, add it to the `:root` of both pages and to this file.
- Every piece of text coming from the server goes through `esc()` before entering `innerHTML`; arguments of inline
  `onclick="..."` handlers go through `jsArg()`.
- Status messages are written to the live regions; errors go through `notify()`, not in status gray.
- Dialogs go through `openModal` and `closeModal`, never through `classList` by hand.
- Don't use `prompt()`, `confirm()` or `alert()`: there are `askConfirm()` and `notify()`.
- Don't animate `width` or `height`; the progress bar scales with `transform`.
- Don't add tooltips as the only place for an explanation: what matters is visible, the `title` is a
  reinforcement.
- Don't introduce colored shadows, gradient text or `backdrop-filter` on static cards.
- The theme is decided before the first paint by the script at the start of `body`; don't move it to the end.
- No Unicode glyphs as icons: the glossary chevron is drawn with borders, download links say "Descargar".
