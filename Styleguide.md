# NosPopuli Design System
*Law for the People — Visual Language Reference*

---

## Philosophy

NosPopuli looks like a serious civic institution, not a tech startup.
The aesthetic draws from editorial newspapers and legal documents —
aged paper, ink, serif typography, ruled lines. It should feel trustworthy,
readable, and permanent.

**One rule above all: if it looks like a SaaS dashboard, it's wrong.**

---

## Color Palette

```css
:root {
  --ink:          #0e0e0e;   /* Primary text, borders, buttons */
  --paper:        #f5f0e8;   /* Page background */
  --aged:         #e8e0cc;   /* Secondary backgrounds */
  --card-bg:      #faf7f2;   /* Card surfaces */
  --accent:       #8b1a1a;   /* Primary accent — deep red */
  --accent-light: #c0392b;   /* Hover states */
  --muted:        #6b6355;   /* Secondary text, labels */
  --rule:         #c8bfaa;   /* Dividers, borders */
}
```

**Usage rules:**
- `--ink` on `--paper` is the default pairing. Never reverse without purpose.
- `--accent` is for actions, links, and highlights only. Never use it decoratively.
- `--muted` is for metadata, labels, timestamps — never for primary content.
- Never introduce new colors. Extend the palette only if absolutely necessary.

---

## Typography

### Fonts
```
Display / Headings:  Playfair Display (Google Fonts)
Body / Reading:      Source Serif 4 (Google Fonts)
Metadata / Code:     IBM Plex Mono (Google Fonts)
```

### Import
```html
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:ital,wght@0,400;0,700;1,400&family=IBM+Plex+Mono:wght@400;500&family=Source+Serif+4:ital,wght@0,300;0,400;1,300&display=swap" rel="stylesheet">
```

### Type Scale
```css
/* Page title / Masthead */
font-family: 'Playfair Display'; font-size: clamp(2.5rem, 6vw, 4.5rem); font-weight: 700;

/* Section headings */
font-family: 'Playfair Display'; font-size: 1.4rem; font-weight: 700;

/* Card titles / Bill names */
font-family: 'Playfair Display'; font-size: 1.15rem; font-weight: 700;

/* Body text */
font-family: 'Source Serif 4'; font-size: 0.95rem; font-weight: 300; line-height: 1.75;

/* Labels / Tags / Metadata */
font-family: 'IBM Plex Mono'; font-size: 0.65–0.75rem; letter-spacing: 0.15–0.25em; text-transform: uppercase;

/* Bill IDs / Codes */
font-family: 'IBM Plex Mono'; font-size: 0.7rem; color: var(--accent); letter-spacing: 0.12em;
```

### Rules
- Never use bold in Source Serif 4 for body text — use weight 300 or 400 only.
- Playfair Display italic is available and should be used for emphasis in display contexts.
- IBM Plex Mono should always be uppercase with generous letter-spacing when used as a label.
- Never mix more than two fonts in a single component.

---

## Layout

### Max widths
```css
--content-width: 760px;   /* Reading content, search, results */
--wide-width:    1100px;  /* Future: charts, data views */
```

### Spacing scale
```css
0.25rem   /* Tight — between label and value */
0.5rem    /* Close — between related elements */
0.75rem   /* Default — between components */
1rem      /* Comfortable — padding inside cards */
1.5rem    /* Generous — section padding */
2.5rem    /* Spacious — between major sections */
4rem      /* Page breathing room */
```

### Page background
`body` is flat `--paper`. No texture, no ruled lines, no gradient. The paper
comes from the colour and the typography; the ruling was noise behind text.

---

## Components

### Header / Masthead
```html
<header>
  <!-- Two rules above and below, double rule at bottom -->
  <div class="masthead">Nos<span>Populi</span></div>
  <div class="tagline">Law for the People · Subtitle here</div>
</header>
```
- The `<span>` inside masthead applies italic + accent color
- Tagline uses IBM Plex Mono, muted color, small caps
- Always: solid rule top, solid rule below tagline, double rule at bottom of header

### Section Labels
```html
<div class="section-label">Label Text</div>
```
```css
.section-label {
  font-family: 'IBM Plex Mono', monospace;
  font-size: 0.6rem;
  letter-spacing: 0.2em;
  text-transform: uppercase;
  color: var(--muted);
  display: flex;
  align-items: center;
  gap: 0.5rem;
}
.section-label::after {
  content: ''; flex: 1;
  height: 1px; background: var(--rule);
}
```
Use for: labeling content sections inside cards, before grouped content.

### Cards
```css
.card {
  background: var(--card-bg);
  border: 1px solid var(--rule);
  /* No border-radius — this is a civic tool, not an app */
}
.card-header {
  padding: 1.25rem 1.5rem;
  border-bottom: 1px solid var(--rule);
}
.card-body {
  padding: 1.5rem;
}
```
- Never use border-radius on cards
- Never use box-shadow — use border instead
- Card header always has a bottom border

### Buttons — Primary
```css
.btn-primary {
  background: var(--ink);
  color: var(--paper);
  border: none;
  font-family: 'IBM Plex Mono', monospace;
  font-size: 0.75rem;
  letter-spacing: 0.15em;
  text-transform: uppercase;
  padding: 1rem 1.75rem;
  cursor: pointer;
}
.btn-primary:hover { background: var(--accent); }
.btn-primary:disabled { background: var(--muted); }
```

### Buttons — Ghost / Text
```css
.btn-ghost {
  background: none;
  border: 1px solid var(--rule);
  color: var(--accent);
  font-family: 'IBM Plex Mono', monospace;
  font-size: 0.65rem;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  padding: 0.4rem 0.75rem;
  cursor: pointer;
}
.btn-ghost:hover {
  border-color: var(--accent);
  background: var(--accent);
  color: white;
}
```

### Search Bar
```css
.search-row {
  display: flex;
  border: 1.5px solid var(--ink);
  background: white;
}
/* Input: Source Serif 4, italic placeholder, no border */
/* Button: attached right, ink background, mono font */
```
Always: no border-radius, button flush to input, 1.5px border weight.

### Tags / Pills
```css
.tag {
  font-family: 'IBM Plex Mono', monospace;
  font-size: 0.6rem;
  letter-spacing: 0.08em;
  color: var(--muted);
  border: 1px solid var(--rule);
  padding: 0.15rem 0.5rem;
  text-transform: uppercase;
}
```

### Status / Progress indicators
```css
.status-inner {
  border-left: 2px solid var(--accent);
  padding: 0.5rem 1rem;
  font-family: 'IBM Plex Mono', monospace;
  font-size: 0.75rem;
  color: var(--accent);
  letter-spacing: 0.05em;
}
```
Lines animate in with opacity + translateY transition.

### Dividers
```css
/* Single rule */
border-top: 1px solid var(--rule);

/* Double rule — for major section breaks */
border-top: 3px double var(--ink);

/* Thick rule — header top */
border-top: 3px solid var(--ink);
```
Never use `<hr>` directly — always apply border-top to a container.

---

## Animation

### Card entrance
```css
@keyframes cardIn {
  from { opacity: 0; transform: translateY(12px); }
  to   { opacity: 1; transform: translateY(0); }
}
.card { animation: cardIn 0.4s forwards; }
.card:nth-child(2) { animation-delay: 0.1s; }
.card:nth-child(3) { animation-delay: 0.2s; }
```

### Status line reveal
```css
.status-step {
  opacity: 0;
  transform: translateY(4px);
  transition: all 0.3s;
}
.status-step.visible { opacity: 1; transform: translateY(0); }
```

### Rules
- Animations should feel like print, not like software — subtle, intentional
- Never animate color changes except on hover
- Transition duration: 0.15s for hovers, 0.3–0.4s for reveals
- Never use bounce, spring, or elastic easing

---

## Voice & Copy

- Labels: SHORT. UPPERCASE. NO PUNCTUATION.
- Body: plain English, no jargon, short sentences
- Error states: direct, never apologetic ("No bills found" not "Sorry, we couldn't find anything")
- Taglines: lowercase or small caps, · as separator
- Bill IDs: always uppercase with space — "HR 1234" not "hr1234"

---

## What Never To Do

- No border-radius on cards, inputs, or buttons
- No box-shadow
- No purple, blue, or green in the palette
- No Inter, Roboto, or system fonts
- No gradients, and no background texture or ruled-line overlay
- No icons except text characters (→, ▶, ▼, ·)
- No full-bleed images
- No dark mode (the paper aesthetic IS the theme)
- No rounded anything

---

## Future: Charts & Data Visualization

When adding charts (vote breakdowns, timelines, sponsor maps):
- Use D3.js or Recharts
- Colors: `--ink` for primary data, `--accent` for highlighted data, `--muted` for secondary
- No rounded bars — square edges only
- No tooltips with rounded corners
- Axis labels in IBM Plex Mono
- Chart titles in Playfair Display
- Never use pie charts — use bar or donut with square legend

---

*This guide is the source of truth for all NosPopuli interfaces.*
*When in doubt: would this look at home in a 1940s legal newspaper? If yes, proceed.*