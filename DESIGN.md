---
name: Compressatorium
description: Safe, automatic game-image conversion for a self-hosted library.
colors:
  workbench-white: "#ffffff"
  bench-mist: "#f6f7f9"
  bench-steel: "#eceef2"
  ink-slate: "#0b1220"
  graphite: "#475569"
  pencil-gray: "#5b6779"
  hairline: "#e2e8f0"
  hairline-strong: "#cbd5e1"
  signal-blue: "#2563eb"
  signal-blue-deep: "#1d4ed8"
  verified-green: "#16a34a"
  verified-green-text: "#15703a"
  caution-amber: "#d97706"
  caution-amber-text: "#a14a08"
  fault-red: "#dc2626"
  notice-sky: "#0ea5e9"
  notice-sky-text: "#0369a1"
  night-bench: "#0b1220"
  night-panel: "#131c2e"
  night-steel: "#1f2a40"
  night-ink: "#f1f5f9"
  night-signal-blue: "#60a5fa"
typography:
  headline:
    fontFamily: "-apple-system, BlinkMacSystemFont, Inter, Segoe UI, Roboto, system-ui, sans-serif"
    fontSize: "24px"
    fontWeight: 600
    lineHeight: 1.2
  title:
    fontFamily: "-apple-system, BlinkMacSystemFont, Inter, Segoe UI, Roboto, system-ui, sans-serif"
    fontSize: "16px"
    fontWeight: 600
    lineHeight: 1.2
  body:
    fontFamily: "-apple-system, BlinkMacSystemFont, Inter, Segoe UI, Roboto, system-ui, sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.5
  body-small:
    fontFamily: "-apple-system, BlinkMacSystemFont, Inter, Segoe UI, Roboto, system-ui, sans-serif"
    fontSize: "13px"
    fontWeight: 400
    lineHeight: 1.5
  label:
    fontFamily: "-apple-system, BlinkMacSystemFont, Inter, Segoe UI, Roboto, system-ui, sans-serif"
    fontSize: "12px"
    fontWeight: 500
    letterSpacing: "0.05em"
  mono:
    fontFamily: "ui-monospace, SF Mono, JetBrains Mono, Menlo, Consolas, monospace"
    fontSize: "13px"
    fontWeight: 400
rounded:
  xs: "2px"
  sm: "4px"
  md: "8px"
  lg: "12px"
  xl: "16px"
  full: "9999px"
spacing:
  "1": "4px"
  "2": "8px"
  "3": "12px"
  "4": "16px"
  "5": "24px"
  "6": "32px"
  "7": "48px"
  "8": "64px"
components:
  button-primary:
    backgroundColor: "{colors.signal-blue}"
    textColor: "{colors.workbench-white}"
    rounded: "{rounded.md}"
    padding: "8px 14px"
  button-primary-hover:
    backgroundColor: "{colors.signal-blue-deep}"
  button-secondary:
    backgroundColor: "{colors.bench-mist}"
    textColor: "{colors.ink-slate}"
    rounded: "{rounded.md}"
    padding: "8px 14px"
  button-secondary-hover:
    backgroundColor: "{colors.bench-steel}"
  button-ghost:
    textColor: "{colors.graphite}"
    rounded: "{rounded.md}"
    padding: "8px 14px"
  button-destructive:
    backgroundColor: "{colors.fault-red}"
    textColor: "{colors.workbench-white}"
    rounded: "{rounded.md}"
    padding: "8px 14px"
  select:
    backgroundColor: "{colors.bench-mist}"
    textColor: "{colors.ink-slate}"
    rounded: "{rounded.md}"
    padding: "8px 12px"
  nav-item:
    textColor: "{colors.graphite}"
    rounded: "{rounded.md}"
    padding: "8px 12px"
  panel:
    backgroundColor: "{colors.workbench-white}"
    rounded: "{rounded.lg}"
    padding: "16px"
  badge:
    rounded: "{rounded.full}"
    padding: "2px 8px"
    typography: "{typography.label}"
---

# Design System: Compressatorium

## Overview

**Creative North Star: "The Quiet Workbench"**

Compressatorium is a tidy bench in a home server room. The surfaces are calm slate neutrals, there is one blue for the action you can take, and color is otherwise kept for status. The games and the work on them are the subject; the chrome stays out of the way. The interface is calm and precise: information is dense but readable, spacing follows a strict 4px rhythm, and nothing raises its voice unless something actually needs attention.

The system is built entirely on CSS custom properties in `src/styles/tokens.css`. Light is the default and dark is a complete counterpart. System fonts keep it native on every machine it's opened from. Depth is mostly flat: tone steps and hairline borders do the separating, and shadow appears only on things that sit above the page.

**Key Characteristics:**
- Slate neutrals with a single Signal Blue for actions, selection and focus.
- Status color (green, amber, red, sky) is meaningful. It is never decoration.
- A strict 4px spacing scale and a compact 12–16px type range suited to tables and lists.
- 8px corners on controls and 12px on panels; pills only for badges.
- Light and dark at full parity, both meeting WCAG 2.2 AA.

## Colors

Cool slate neutrals carry almost every pixel. One saturated blue marks what you can do, and a small set of status hues marks what happened.

### Primary
- **Signal Blue** (`signal-blue`; dark theme `night-signal-blue`): the primary button, the active nav item (on a light blue wash), checked checkboxes, links, the focus ring and text selection. It is the only color that means "act here".

### Neutral
- **Workbench White / Bench Mist / Bench Steel** (`workbench-white`, `bench-mist`, `bench-steel`): the page, recessed areas such as inputs and secondary buttons, and hover and pressed fills, in increasing depth.
- **Ink Slate** (`ink-slate`): primary text and headings.
- **Graphite** (`graphite`): secondary text, labels and idle nav items.
- **Pencil Gray** (`pencil-gray`): muted text. It is tuned to stay at or above 4.5:1 on both white and mist.
- **Hairline / Hairline Strong** (`hairline`, `hairline-strong`): borders and dividers. The strong variant is for hover and for checkbox outlines.
- **Night Bench / Night Panel / Night Steel / Night Ink**: the dark-theme counterparts of the page, panel, raised and text tones.

### Status
- **Verified Green** (`verified-green`, text variant `verified-green-text`): success, and a checked or verified result.
- **Caution Amber** (`caution-amber`, text `caution-amber-text`): warnings and things waiting.
- **Fault Red** (`fault-red`): errors and destructive actions.
- **Notice Sky** (`notice-sky`, text `notice-sky-text`): neutral information.

Each status color also has a muted wash, about 10% in light and 14% in dark, for row and banner backgrounds. Format badges (CD, DVD, CSO, Wii U, archive, DAT match) use their own hue at 16% behind text in the same hue.

### Named Rules
**The One Voice Rule.** Signal Blue means "you can act here". It is never used for decoration, headings or status, so a person can scan for blue and find their next action.

**The Text-Variant Rule.** On light surfaces, status text uses the `-text` variant, never the fill color. The fills are too light to read as text on white. In dark mode the fills are bright enough and double as text.

## Typography

**Body Font:** the system UI stack (-apple-system, BlinkMacSystemFont, Inter, Segoe UI, Roboto, system-ui)
**Label/Mono Font:** ui-monospace, SF Mono, JetBrains Mono, Menlo, Consolas, for paths, hashes and file names

**Character:** native and invisible. The type looks like the operating system it's opened on, which keeps the attention on file names and numbers rather than on the typeface.

### Hierarchy
- **Headline** (600, 24px, 1.2): page titles such as Files, Libraries, Activity and Settings.
- **Title** (600, 16px, 1.2): section and dialog titles, and the brand name.
- **Body** (400, 14px, 1.5): the default for lists, tables, forms and messages.
- **Body Small** (400, 13px, 1.5): descriptions under controls, secondary row details and small buttons.
- **Label** (500, 12px, 0.05em tracking, uppercase): field labels and panel headings (panel headings use 14px semibold uppercase).
- **Mono** (400, 13px): paths, checksums and anything a person might copy exactly.

### Named Rules
**The Compact Range Rule.** Working text lives between 12px and 16px. Larger sizes (20–30px) are only for page titles and empty-state headlines, never for emphasis inside a list.

## Layout

A sidebar shell: a sticky 240px sidebar that collapses to 64px, a 56px top bar, and content capped at 1440px. On phones the sidebar becomes a drawer. Files uses a three-panel layout (Volumes, file table, details) that responds to its container's width rather than the viewport's, so the table keeps its columns at laptop widths.

Spacing comes only from the 4px scale (4, 8, 12, 16, 24, 32, 48, 64). Controls use 8–14px internal padding, panels 16px, and gaps between related items 8–12px. Density is high but every row keeps at least 1.5 line-height.

**The 4px Rhythm Rule.** Every margin, gap and padding is a step on the spacing scale. A value off the scale is a bug.

## Elevation & Depth

Mostly flat. Tone steps (white to mist to steel) and 1px hairlines separate regions. Shadows exist only to lift something above the page: a resting panel gets the faintest lift, and menus, popovers and dialogs get progressively more. Dark mode uses deeper, more opaque shadows so lifted surfaces still read against a dark page.

### Shadow Vocabulary
- **Rest** (`--elev-1`): panels sitting on the page, barely there.
- **Raised** (`--elev-2`): dropdowns, popovers and toasts.
- **Overlay** (`--elev-3`): dialogs and drawers above the dimmed page (`--surface-overlay`).

### Named Rules
**The Lift-Only Rule.** A shadow means "this sits above the page". Never add one for decoration or to make a flat element look like a button.

## Shapes

Gently rounded and consistent. Controls (buttons, selects, inputs, nav items) use 8px corners. Panels and cards use 12px. Checkboxes and small chips use 4px. Badges are full pills. Borders are always 1px hairlines (1.5px for checkbox outlines). There are no decorative shapes, gradients or clipped silhouettes.

## Components

### Buttons
Tactile but quiet: a 1px press-down on click, 120ms color transitions.
- **Shape:** gently rounded (8px).
- **Primary:** Signal Blue fill, white text, 8px × 14px padding, medium weight. Deepens on hover.
- **Secondary:** Bench Mist fill with a hairline border. It steps to Bench Steel and a stronger border on hover.
- **Ghost:** transparent, Graphite text, gaining a mist fill on hover. Used for toolbar and row actions.
- **Destructive:** Fault Red fill and white text, brightening slightly on hover. Used only on the confirm step of a delete.
- **Sizes:** small (6 × 10px, 13px text), medium (8 × 14px, 14px) and large (10 × 18px, 16px).
- **Disabled:** 55% opacity with a not-allowed cursor.

### Badges
- **Style:** full pills, 12px semibold. Each badge uses its own hue at 16% behind same-hue text.
- **Use:** format (CD, DVD, CSO, Wii U, archive), DAT match, verified, and converting.

### Panels
- **Corner Style:** 12px.
- **Background:** Workbench White with a hairline border and the Rest shadow.
- **Internal Padding:** 16px.
- **Header:** 14px semibold uppercase title with 0.05em tracking; actions sit on the right.

### Inputs / Fields
- **Style:** Bench Mist fill, hairline border, 8px corners, 8 × 12px padding. Labels are 12px uppercase Graphite above the field.
- **Focus:** the border turns Signal Blue and gains the 3px focus ring.
- **Disabled:** 55% opacity.
- **Checkbox:** an 18px box with 4px corners and a strong hairline outline. It fills Signal Blue with a white check when on, and takes the focus ring on keyboard focus.

### Navigation
- **Sidebar items:** 8 × 12px padding, 8px corners, Graphite text. On hover they take a mist fill and Ink Slate text.
- **Active item:** Signal Blue text on a light blue wash (`--accent-muted`), medium weight.
- **Badge dot:** an 8px status dot on the icon, ringed in the surface color.
- **Mobile:** the sidebar becomes a drawer.

### Focus Ring
Every focusable element shows a 3px Signal Blue ring at 40% opacity (45% in dark) on keyboard focus, with no outline. Focus is never hidden.

## Do's and Don'ts

### Do:
- **Do** take every color, size, radius, space and shadow from `src/styles/tokens.css`. No raw hex or pixel values in components.
- **Do** keep Signal Blue for the one action that matters on a surface, and use secondary or ghost buttons for the rest.
- **Do** use the `-text` status variants for status text on light surfaces, and check 4.5:1 in both themes.
- **Do** show the keyboard focus ring on everything focusable.
- **Do** keep busy indicators visible under reduced motion: they fade instead of moving (`reduced-busy`).
- **Do** design light and dark together. A component isn't done until both pass.

### Don't:
- **Don't** use Signal Blue or any status hue for decoration, headings or emphasis.
- **Don't** add shadows to flat elements. Shadow means lifted, never decoration.
- **Don't** step outside the 4px spacing scale or the 12–16px working type range.
- **Don't** add gradients, decorative shapes, marquee motion or striped fills. None of these belong to this bench.
- **Don't** use a destructive red button anywhere but the confirming step of a delete.
