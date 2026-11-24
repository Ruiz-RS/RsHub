---
name: RsHub
description: Orbital, observatory glass edition. A deep-space control center for Windows.
colors:
  void: "#060A14"
  deep: "#0B1222"
  ink: "#E9EEF7"
  mute: "#97A3BA"
  faint: "#7683A0"
  accent: "#00F0D0"
  accent2: "#FFFFFF"
  on-accent: "#1C0A06"
  ok: "#7FE0B4"
  warn: "#F4C26B"
  bad: "#FF6B7A"
  shade: "#000000"
  scrim: "#03050B"
  paper: "#FFFFFF"
  raise: "rgba(158,190,240,.045)"
  raise2: "rgba(158,190,240,.085)"
  raise3: "rgba(158,190,240,.13)"
  line: "rgba(170,200,245,.105)"
  line2: "rgba(170,200,245,.2)"
typography:
  mono:
    fontFamily: "Cascadia Mono, Consolas, monospace"
    fontSize: "12.5px"
    fontWeight: 400
  display-zero:
    fontFamily: "UnbZero, Unbounded, system-ui, sans-serif"
    fontSize: "28px"
    fontWeight: 500
  display-hero:
    fontFamily: "Unbounded, Microsoft YaHei UI, system-ui, sans-serif"
    fontSize: "34px"
    fontWeight: 500
    lineHeight: 1.08
    letterSpacing: "-0.032em"
  display-h1:
    fontFamily: "Unbounded, Microsoft YaHei UI, system-ui, sans-serif"
    fontSize: "28px"
    fontWeight: 500
    lineHeight: 1.1
    letterSpacing: "-0.03em"
  display-h3:
    fontFamily: "Unbounded, Microsoft YaHei UI, system-ui, sans-serif"
    fontSize: "17px"
    fontWeight: 500
    letterSpacing: "-0.02em"
  display-num-1:
    fontFamily: "Unbounded, Microsoft YaHei UI, system-ui, sans-serif"
    fontSize: "24px"
    fontWeight: 500
  display-num-2:
    fontFamily: "Unbounded, Microsoft YaHei UI, system-ui, sans-serif"
    fontSize: "32px"
    fontWeight: 500
  display-num-3:
    fontFamily: "Unbounded, Microsoft YaHei UI, system-ui, sans-serif"
    fontSize: "40px"
    fontWeight: 500
  display-num-4:
    fontFamily: "Unbounded, Microsoft YaHei UI, system-ui, sans-serif"
    fontSize: "52px"
    fontWeight: 500
  display-h2:
    fontFamily: "Unbounded, Microsoft YaHei UI, system-ui, sans-serif"
    fontSize: "21px"
    fontWeight: 500
  lead:
    fontFamily: "Onest, Microsoft YaHei UI, system-ui, sans-serif"
    fontSize: "15px"
    fontWeight: 600
  body:
    fontFamily: "Onest, Microsoft YaHei UI, system-ui, sans-serif"
    fontSize: "13.5px"
    fontWeight: 400
    lineHeight: 1.45
  label:
    fontFamily: "Onest, Microsoft YaHei UI, system-ui, sans-serif"
    fontSize: "12.5px"
    fontWeight: 600
  caption:
    fontFamily: "Onest, Microsoft YaHei UI, system-ui, sans-serif"
    fontSize: "11.5px"
    fontWeight: 400
rounded:
  xs: "5px"
  s: "9px"
  m: "14px"
  l: "22px"
  pill: "999px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "14px"
  lg: "18px"
  xl: "26px"
components:
  button-primary:
    backgroundColor: "{colors.accent}"
    textColor: "{colors.on-accent}"
    rounded: "11px"
    height: "34px"
    padding: "0 15px"
  button-secondary:
    backgroundColor: "{colors.raise2}"
    textColor: "{colors.ink}"
    rounded: "11px"
    height: "34px"
  panel:
    backgroundColor: "{colors.deep}"
    rounded: "{rounded.l}"
    padding: "16px 18px"
  chip:
    backgroundColor: "{colors.raise}"
    textColor: "{colors.mute}"
    rounded: "99px"
    height: "26px"
---

# RsHub design system ("Orbital", observatory glass edition)

## Overview
A deep-space control center. The sky is alive (animated starfield, nebula wash, shooting stars, a warp camera between
Home and the modules); the work sits on dark, lit glass in front of it. Operate surfaces are precise and calm; the
brand lives in the details: the planet-with-orbit mark, coral and ice light, the wide Unbounded display face.
Source of truth: `rshub/ui/css/base.css` (`:root` tokens) and `docs/UI_CONTRACT.md`.

## Colors
- Ground: `--void` (window) and `--deep` (glass). Three translucent raises for rows, hovers and pressed states.
- Text tiers: `--ink` content, `--mute` secondary (~7:1), `--faint` metadata (>= 4.5:1 on a panel). Never lower.
- Accents: coral `--accent` for the primary action, the current selection and "needs you"; ice `--accent2` for live
  data, focus and information. Derived: `--acc-soft`, `--acc-line`, `--ice-soft`, `--ice-line`. Users can swap the
  pair; never hard-code the hex in a component.
- States: `--ok`, `--warn`, `--bad`, only where they carry meaning.

## Typography
- Unbounded (500) only from 15 px up: the brand, page and section titles, hero numbers. The zero is swapped for a
  slashed glyph (`UnbZero`) so 0 never reads as O.
- Onest for everything below 15 px: labels and panel titles 600, body 400, data with tabular figures.
- Roles: `--fs-cap` 11.5, `--fs-sm` 12.5, `--fs-body` 13.5, `--fs-lg` 15, `--fs-h3` 17, `--fs-h2` 21, `--fs-h1` 28.
  Display numerals (tile and ring figures, results): `--fs-n1` 24, `--fs-n2` 32, `--fs-n3` 40, `--fs-n4` 52.
  Module CSS uses these tokens, never a literal size.
- Sentence case everywhere; no ALL-CAPS labels, no eyebrows above headings.

## Layout
- Shell: top bar (62 px), content between it and the dock (`--dockspace`), modules inset 30 px.
- Module: one header everywhere (`.mhead`: title and one-line description on the left, actions on the right), the tab bar
  (`.subnav`) 18 px below it, then panels. Panels fill the width and the height: grids use `repeat(auto-fit, minmax(...))`,
  every column reaches the dock and lines up with its neighbours (lists scroll inside), empty and idle states fill
  their panel with the content centred; nothing hangs in a corner with empty space beside or under it. Gaps 10-26 px; tight inside a group,
  generous between groups. At 1100 x 700 CSS px (the size the layouts are designed for) nothing may wrap or overlap.
- Compact layout (`html[data-layout=scroll]`, below 1100 x 700 CSS px: handhelds at 150 %, small windows): nothing is
  squeezed or clipped. Each view lays out at least as tall as in a 760 px window and scrolls as one page (wheel,
  touch, keyboard, controller); lists keep scrolling inside their panels and hand the wheel on to the page at their
  ends; the dock floats over the page and the page ends with its room free; narrow windows reflow columns
  (`max-width:1099px`). Above it the fit-to-window design is unchanged.
- Interface size Automatic (`js/zoom.js`): bigger only where there is room to spare (1440 x 820 left); 100 % when the
  layouts fit; on a smaller or high-scaling screen 90 % or 80 % so they fit (never more than 100 % there). The
  window's minimum size always fits the screen's work area.
- Density "compact" and corner presets (soft / mid / sharp) remap the same pieces.

## Elevation & Depth
One glass recipe for every frame (panel, sheet, palette, dock, toast, menu, dialog):
- fill: `--sheen` (a faint ice light from above, 140 px) over a deep tint whose opacity follows "Solid panels"
  (`--pa`);
- edge: 1 px `--line`, plus `--rim` (an inset 1 px highlight on the top edge);
- shadow: `--lift` (panels) or `--lift-hi` (floating layers), always offset downward with a soft blur.
No zero-offset coloured glows: a coloured dot gets a ring (`0 0 0 3px` soft) instead. No backdrop-filter on large
areas. Lite mode (`html[data-lite="1"]`) drops to short flat shadows and no blur.

## Shapes
Radii `--r-xs` 5 (bars, keys, tiny tiles), `--r-s` 9, `--r-m` 14, `--r-l` 22, `--r-pill` (search, chips, toggles).
Module CSS uses the tokens, so the corner presets (soft, sharp) reach every module. Spacing: `--sp-xs` 4, `--sp-sm` 8,
`--sp-md` 14, `--sp-lg` 18, `--sp-xl` 26. Icons are 24-unit stroke SVGs
(`SH.ic`, class `i`), no fill, one 1.4 px line at every size (`--ic-w`, non-scaling stroke; `--ic-w-bold` 1.9 px only
for 12-14 px marks inside filled badges). Seven sizes: `--ic-xs` 12, `--ic-s` 14, `--ic-m` 16 (buttons, rows),
`--ic-l` 18 (default), `--ic-xl` 20, `--ic-2xl` 24 (dock), `--ic-3xl` 28.

## Components
- Buttons: primary coral (top-lit gradient, inner highlight, coral-tinted offset shadow), secondary glass, ghost,
  danger; `sm` and `lg`.
- Tabs (`.subnav`) and segmented controls (`.seg`): a sliding glass indicator moved by a spring.
- Dock: the active module is marked by a small coral planet on an ice orbit, the brand mark in miniature.
- Toasts: dark glass with a small planet as the signifier, action in coral. Their own lane, bottom right, never over
  content, the dock, the hint bar or the version tag, never in the top bar: beside the dock, the newest centred on the
  dock bar (never lower), older ones stacking upward only within the dock's band; a narrow window gives them the width
  there is (at least 200 px) and the text wraps. Only with no room at all beside the dock (a compact handheld layout):
  one slim bar centred just above the dock, in a strip the page then keeps free.
- Hover: every clickable element answers calmly (a raised fill, a lighter edge or ink colour, the pointer cursor); the
  primary ones (dock, buttons, tiles, button rows) also tick with `hover`, the rest with the quieter `hover_soft`.
- Live indicators: steady dots with a ring; only indeterminate progress sweeps loop.

## Empty states
One component, `SH.emptyState({ art, title, text, action, small })`: a small orbit scene in the app's line style (a planet
with a check for "all done", an orbit missing its moon for "nothing found", a lone star in its orbit for "nothing yet",
a planet off its orbit for "could not read"), a one-line title, a short explanation and, where there is a next step, one
button. Never a bare sentence in a blank panel.

## Motion
- Curves: `--e-out` `cubic-bezier(.16,1,.3,1)` for arrivals, `--e-snap` `cubic-bezier(.22,1,.36,1)` for controls,
  `--e-io` for travel. No bounce, no elastic, no overshoot in CSS.
- JS springs (`SH.EASE.spring/soft/snappy`, `SH.Spring`) carry the orbital character on the dock, the camera, the
  3D view and the indicators, damped to about 0.85 so they settle.
- Durations scale with `--f` (intensity and speed setting); 83-250 ms for controls, <= 500 ms for layers. At
  intensity `min` nothing loops; hover moves a child, never the hovered box.

## Do's and Don'ts
- Do keep stars in the gutters and glass over the content.
- Do use coral for one thing per view: the primary action or the item that needs the user.
- Don't add side stripes, gradient text, glows, stripe textures, blinking dots or a second display face.
- Don't use Unbounded under 15 px or for running text.
