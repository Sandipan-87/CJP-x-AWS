# Design: Engram Live Ops Console

A brutalist, high-density DevOps engineering console. The interface disappears into the
task (Operate mode, per PRODUCT.md) — familiarity and legibility outrank expression. Every
value below is authoritative; update `globals.css`/component source and this file together
when either changes.

## 1. Palette

One canvas tone, one surface tone, one border tone. No tint ramp, no gradient, no glow.

| Role | Value | Tailwind equiv | CSS var |
|---|---|---|---|
| Canvas (page ground) | `#09090b` | zinc-950 | `--background` |
| Surface (every panel/card/popover) | `#18181b` | zinc-900 | `--card`, `--popover`, `--secondary`, `--muted`, `--accent`, `--sidebar` |
| Border (the one hairline everywhere) | `#27272a` | zinc-800 | `--border`, `--input`, `--sidebar-border` |
| Text, high-contrast | `#f4f4f5` | zinc-100 | `--foreground`, `--card-foreground`, `--popover-foreground`, `--accent-foreground` |
| Text, muted (labels, meta, timestamps) | `#71717a` | zinc-500 | `--muted-foreground` |
| Accent — active / live / stream / approve | `#34d399` | emerald-400 | `--success` (new token, §6) |
| Reject / failure | `#ef4444` text on `#450a0a`/`#7f1d1d` fill | red-400 on red-950/red-900 | `--destructive` + the `destructive` button variant's own flat fill (unchanged from the prior pass) |

**Rule:** exactly one surface tone. Do not reintroduce a second "one step up" shade — the
prior `--card: #0f0f11` (a shade between canvas and zinc-900) is retired; every raised
surface now uses the same zinc-900, so nothing in this app reads as "a card on a card."

**Bans, unchanged from every earlier pass in this project:** zero glassmorphism, zero drop
shadows, zero glowing borders/rings (focus states are a hard `outline`, never a `ring` —
a ring is a box-shadow and reads as a blurred halo), zero gradients.

## 2. Spatial grid

- **8px base unit.** Every gap/padding/margin in this app is a multiple of `--spacing(1)`
  (4px) or `--spacing(2)` (8px) — no arbitrary values like the old `p-2.5` (10px).
- **`--radius: 0rem`.** Every corner in the app is sharp. This one CSS variable drives every
  `rounded-*` utility via the `@theme inline` mapping in `globals.css` — never override radius
  per-component.
- **Zero card-nesting.** A bordered, padded row inside an already-bordered panel is banned.
  Rows inside a feed panel are `divide-y` hairline rows, never boxed `li` elements. A grid of
  same-size stat tiles is one outer border with `divide-x`/`divide-y` between cells, never N
  separately-boxed cards.

## 3. Typography

Two families, split strictly by content type, not by visual weight:

- **Sans (Geist Sans, `--font-sans`):** every structural UI label — panel titles, button
  text, status words, prose, empty states. Nothing numeric or code-shaped ever renders here.
- **Mono (JetBrains Mono, `--font-mono`):** every telemetry value, metric digit, timestamp,
  query fingerprint / row ID, and SQL or log block. `tabular-nums` is set globally on
  `.font-mono`/`code`/`pre` so digit columns never jitter as values update.

No display face, no third family. Product UI doesn't need one (per PRODUCT.md's Operate
mode).

## 4. Viewport

**Fixed single-screen cockpit.** The root layout is `h-screen overflow-hidden` — there is no
page-level scrollbar, ever, at any viewport this app is expected to run at (a demo screen,
not a phone). Individual feed panels may scroll their own content internally (each already
wraps its list in a `ScrollArea`); the page chrome around them never does.

## 5. Components

- **Buttons:** flat fills, 1px border, sharp corners, hover is a flat opacity drop only —
  never a lift, never a glow. Three semantic variants in this app: `default` (brutalist
  white/black, for a generic primary action), `success` (emerald-400 fill / near-black text,
  for Approve — see §6), `destructive` (flat dark red, for Reject — unchanged).
- **Status dots** (the small "connected" indicator on every panel header): `emerald-400`,
  not `emerald-500` — matches the accent value in §1 exactly.
- **Cards:** one 1px border, zinc-900 fill, 8px internal padding (`--card-spacing`), sharp
  corners. A card is the outermost grouping container for one feed; nothing inside it is
  ever another bordered box.

## 6. The `success` token and button variant

`--success: #34d399` (emerald-400) / `--success-foreground: #022c22` (emerald-950, for
~13:1 contrast). This is deliberately its own token, not a repurposing of `--primary` — the
brutalist white/black `default` variant stays available for any future action that isn't
semantically "approve/live," so the two meanings never collide under one name.

## 7. Do / Do not

**Do:** one canvas tone, one surface tone, one accent color used only for state (live,
approve), sharp corners everywhere, mono only for things that are literally numbers, IDs, or
code, a fixed 100vh frame.

**Do not:** add a second surface shade "for depth," add a shadow "for polish," round a
corner "to soften it," use the emerald accent decoratively (it means "live" or "approve" —
nothing else), let any panel force the page itself to scroll.
