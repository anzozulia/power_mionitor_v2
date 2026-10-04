# Phase 6 frontend stack (KD8)

**Date:** 2026-10-04 · **Status:** reference for `/gsd-plan-phase 6`, the executor and the verifier

- This document is the detail behind **`PHASE-6-BRIEF.md` §8 (KD8 "Admin frontend stack")**. If the two disagree, the brief wins; maintainer decisions D6-01…D6-07 win over both.
- **Versions, release dates, sizes and the two Tailwind checksums below were verified on 2026-10-04** against the npm registry, PyPI, the GitHub API and the vendors' own docs.
- **They are not final pins.** At planning time, re-check every version, re-pin it with its sha256 in the vendor manifest (§7), and pass every new binary and file through the **INV-26 supply-chain legitimacy checkpoint** (the same gate Pillow and Inter went through in Phase 3) before it enters the repo or the Dockerfile.
- Every asset is self-hosted. Download URLs below are used once, at pin time; no template, CSS file or header ever names a third-party origin (R5, UI-13, `docs/v1-lessons.md:333`).

---

## 0. Stack at a glance

| Layer | Pick (pinned) | Released | Licence | Why |
|---|---|---|---|---|
| CSS engine | **Tailwind CSS v4.3.3 standalone CLI** (single binary, no Node) | 2026-07-16 | MIT | Pinned by sha256 and run only in a Docker build stage and a dev watcher. The binary already includes `@tailwindcss/forms` 0.5.11 and `@tailwindcss/typography` 0.5.20 |
| Look and design tokens | **TailAdmin free HTML 2.4.0** as the visual reference: copy its `@theme` palette, shadows, type scale and layout patterns; rewrite the markup | 2026-09-13 (README log) | MIT, © 2023 TailAdmin | The look the maintainer asked for, and legal to adapt (§1). Free edition only, never Pro |
| Interactivity | **Native `<dialog>`, invoker commands and the Popover API**, plus **`@alpinejs/csp` 3.17.4** and one first-party `admin.js` | Alpine 2026-09-21 | MIT | All of it works under a self-only CSP with no `unsafe-eval`. The Alpine CSP build is about 72 KB minified, 23.8 KB gzipped |
| Font | **Inter Variable** woff2 subsets from `@fontsource-variable/inter` 5.3.0 | 2026-07-19 | OFL-1.1 | Covers Cyrillic and matches the chart's Inter 4.1 (`docs/chart-spec.md` §4). TailAdmin's Outfit has no Cyrillic |
| Icons | **Lucide** (`lucide-static` 1.52.0); copy in only the ~30 SVGs used and render them inline with an `{% icon %}` tag | 2026-10-04 | ISC | ~2,130 icons, actively maintained |
| Forms | Django's own form templates (`TemplatesSetting` renderer) plus the existing `LocationBoundField` (`powermon/web/forms.py:115`) | Django 5.2 | — | Keeps Tailwind classes in templates, where the scanner finds them. Keeps the `aria-describedby` / `aria-invalid` wiring. No crispy |
| Static files | WhiteNoise 6.12.0 + `CompressedManifestStaticFilesStorage`, unchanged (`powermon/settings.py:138-143`) | 2026-02-27 | MIT | Hashed filenames are cached forever, so no manual cache busting |
| New Python runtime deps | **None required** | | | None planned (each would need the INV-26 checkpoint). Test-only parser deps are a `TEST-STRATEGY.md` question (brief §12 Q3) |

### Considered and rejected

| Candidate | Verdict |
|---|---|
| **htmx** (2.0.11 `latest`, 2026-09-22, 0BSD; 4.0.0 under the `next` tag, 2026-08-28) | **Rejected.** See the note below |
| Standard Alpine build (`alpinejs`) | Rejected: it compiles attribute expressions with `Function`, so it needs `'unsafe-eval'` (§3) |
| `@alpinejs/persist` | Not needed: the `sidebar` component reads and writes `localStorage` itself (§3). One fewer vendored file |
| django-tailwind-cli 4.8.1 | Rejected: downloads an unpinned, unverified binary at run time (§2) |
| ApexCharts (TailAdmin's chart library) | Rejected: no longer MIT, and JS chart libraries are out of scope (brief §3). UI-06 shows the existing weekly PNG |
| Outfit (TailAdmin's font, from Google Fonts) | Rejected: third-party runtime asset and no Cyrillic |
| crispy-tailwind | Rejected: stale (last release 1.0.3, 2024-02), Tailwind v3 era (§6 pitfall 12) |

**Why htmx is rejected:**
1. **One JS library is enough.** Alpine (CSP build) plus native `<dialog>` and popovers cover every UI-01…UI-13 interaction. UI-05 polling is a small Alpine `poll` component over a JSON endpoint (brief §12 Q6), and the modal loader is a short `fetch` (§3).
2. **History snapshots could leak secrets.** htmx 2 stores page snapshots in `sessionStorage` (`htmx.js:3221-3250`). That would copy the revealed device key (`powermon/web/templates/web/location_setup.html:34-37`), which is served `no-store` (`powermon/web/views.py:305-309`), into browser storage. R4 forbids that.
3. **htmx 4 is too new.** 4.0.0 shipped on 2026-08-28 under the `next` tag and becomes `latest` in early 2027. Brief §3 rules out htmx in any version.

## 1. TailAdmin (visual reference)

**What it is.** A Tailwind admin dashboard template by TailAdmin (tailadmin.com). The free edition ships for HTML (with Alpine.js), React, Next.js, Vue, Angular and Laravel. There is no official Django port.

**Repository.** github.com/TailAdmin/tailadmin-free-tailwind-dashboard-template: 2,342 stars, last pushed 2026-09-15, no GitHub "Releases". Versions are recorded only in `package.json` and the README log.
- 2.4.0 (2026-09-13): i18n, RTL, a yearly calendar view, accessibility fixes, an AGENTS.md, updated packages.
- 2.3.x (April–May 2026): added Pro pages.

**Licence (free edition).** MIT, "Copyright (c) 2023 TailAdmin". The only condition is that the copyright and permission notice ship with "all copies or substantial portions". There is no credit-link or attribution-UI requirement.

**Can we copy and adapt it into Django templates? Yes** (D6-02). Ship its `LICENSE` text as `powermon/web/static/web/vendor/LICENSES/tailadmin-MIT.txt` (§8). Two limits:
- Do not copy from the Pro demo (demo.tailadmin.com) or the Pro Figma file. Pro is commercial: Starter $59 (1 seat, 3 projects, not for SaaS), Business $119, Extended $299 (SaaS and redistribution), per tailadmin.com/pricing.
- Do not use TailAdmin's name or logo as our branding.

**Tech stack** (from `package.json` 2.4.0):
- Runtime dependencies: `alpinejs`/`@alpinejs/persist` ^3.17.2, `apexcharts` ^7.3.0, `flatpickr` ^4.6.13, `dropzone` ^6.2.1, `fullcalendar` ^7.1.0, `jsvectormap` ^1.7.0, `swiper` ^14.2.0, `i18next` ^26.4.2.
- Build: `tailwindcss` and `@tailwindcss/postcss` ^4.3.3, `@tailwindcss/forms` ^0.5.9, webpack 5, babel 8, `prettier-plugin-tailwindcss`.
- Page fragments are assembled with webpack `<include src>` tags. These map directly onto `{% include %}`.

**Fonts.** `src/css/style.css:1` loads **Outfit from Google Fonts** with `@import url("https://fonts.googleapis.com/...")`. That is a third-party runtime asset and is dropped. Outfit covers only `latin` and `latin-ext` (Fontsource API), so it has no Cyrillic.

**Design language** (from `src/css/style.css`):
- `@theme` defines:
  - brand blue: `--color-brand-500 #465fff`, `-600 #3641f5`
  - grays: `gray-50 #f9fafb` … `gray-900 #101828`, plus `gray-dark #1a2231`
  - status scales: `success`, `error`, `warning`, `orange`, `blue-light`, each 25–950
  - shadows: `--shadow-theme-xs…xl`, `--shadow-focus-ring: 0 0 0 4px rgba(70,95,255,.12)`
  - type scale: `--text-title-*`, `--text-theme-*`
  - extra breakpoints: `2xsm` 375px, `xsm` 425px, `3xl` 2000px
- `@utility` rules cover menu items (`menu-item-active`/`-inactive`, dropdown items, badges).
- Layout shell (`src/index.html`):
  - a full-height flex page
  - a fixed left sidebar that collapses to an icon rail (`xl:w-22.5`) and slides in as a drawer on mobile, with an overlay
  - a sticky header with a hamburger, search, dark-mode toggle, notifications and a user dropdown
  - content inside `max-w-(--breakpoint-2xl) p-4 md:p-6` with a 12-column grid of white `rounded-2xl border border-gray-200` cards (dark: `bg-white/[0.03] border-gray-800`)
- Tables are borderless with row dividers. Badges are rounded-full pills, e.g. `bg-success-50 text-success-600 dark:bg-success-500/15`. Alerts come in 4 tones. Modals are Alpine `x-show` overlays.
- Dark mode is a `.dark` class driven by `@custom-variant dark (&:is(.dark *))`.

Our status colours do not come from TailAdmin: they reuse the chart's hues with darker text shades for AA (brief §8 "Tokens", `DESIGN-DIRECTION.md` §5). Search and notifications in TailAdmin's header are out of scope (brief §3).

**Free-edition pages:** `index` (eCommerce dashboard), `calendar`, `profile`, `form-elements`, `multiple-select`, `basic-tables`, `alerts`, `avatars`, `badge`, `buttons`, `videos`, `bar-chart`, `line-chart`, `blank`, `404`, `signin`, `signup`.

**Partials:** `sidebar`, `header`, `breadcrumb`, `overlay`, `preloader`, alerts ×4, badges ×6, buttons ×6, avatars, `metric-group-01`, `table-01`/`table-06`, `chart-01..03`, `map-01`, profile modals, `datepicker`, `language-dropdown`.

**What not to copy (verified in the source):**
- (a) The viewport line `user-scalable=no, maximum-scale=1.0` (`src/index.html:5-8`). It blocks pinch-zoom and fails WCAG 1.4.4.
- (b) The inline Alpine expressions using `window.innerWidth`, `localStorage`, `JSON.parse`, `$watch(… => …)` and `$t()` (`src/partials/sidebar.html`, `src/index.html:15-18`). The CSP build rejects all of these, so the behaviour moves into `Alpine.data()`.
- (c) The theme set from a deferred bundle (`src/js/index.js`), which flashes the wrong theme on load.
- (d) **The sidebar has zero `focus`/`focus-visible` styles.**
- (e) Badge text `success-600` on `success-50` measures **3.54:1** and `error-600` on `error-50` measures **4.44:1**. Both fail AA for small text; use the -700/-800 shades.
- (f) ApexCharts. It is no longer MIT: it is dual-licensed, free only for organisations under $2M, with an OEM licence needed when embedded in a platform used by others (`apexcharts@7.8.0/LICENSE`). Use the existing weekly PNG and server-rendered SVG or `<meter>` instead.
- (g) The preloader, i18next, flatpickr, dropzone, swiper and the vector map.

**Alternatives, one-line verdicts:**
- **Basecoat** (`basecoat-css` 1.0.2, MIT, 4.3k stars): shadcn/ui for any stack, Tailwind v4, small vanilla JS, ships Jinja templates. *Visual reference only: its vanilla JS would be a second library (brief §8).*
- **daisyUI** 5.7.47 (MIT): pure-CSS semantic classes (`btn`, `card`), works with the standalone CLI through a downloaded `daisyui.mjs`, needs zero JS. *Most CSP-friendly, but daisy-themed rather than TailAdmin.*
- **Flowbite** 4.0.2 (MIT core): v4 moved theming to CSS variables and has 5 themes; its JS uses data attributes. *Good markup reference; do not adopt its JS as a second library.*
- **Preline** 5.0.0 (MIT plus the "Preline UI Fair Use License"): heavier JS plugins and licence nuance. *Skip.*
- **Penguin UI** (MIT, Tailwind v4 + Alpine) and **Pines** (DevDojo, MIT): copy-paste Alpine components. *Useful patterns, but the inline Alpine has to be rewritten for the CSP build.*
- **Tailwind Plus** ($299 personal, commercial): top quality; its "Elements" are vanilla custom elements. *Paid; not used (D6-02: TailAdmin free is the reference).*
- **django-unfold** 0.108.0: a Tailwind skin for `django.contrib.admin`. *Not applicable; this project does not use contrib.admin (`powermon/settings.py:37-48`).*

## 2. Tailwind v4 build with no Node at runtime

| Option | Verdict |
|---|---|
| **Pinned standalone CLI in a Docker build stage + dev watcher** | **Recommended.** It fits the repo's pin-everything habit (`Dockerfile:4-5` pins Python and uv; `pyproject.toml:32-33` sets `exclude-newer` and the uv version). The binary is ~105–112 MB, so it stays out of the runtime image. Checksums come from the release's `sha256sums.txt` |
| django-tailwind-cli 4.8.1 (2026-09-18, MIT, Django 5.2–6.1) | Not used. It defaults to `TAILWIND_CLI_VERSION="latest"` (falling back to 4.1.3), downloads the binary at run time into `.django_tailwind_cli/`, and its `_download.py` does no checksum verification. Its `tailwind runserver` does not fit a stack that runs gunicorn in Docker |
| Node only in a Docker build stage (`npm ci` with a lockfile) | Acceptable fallback if npm lockfile integrity or a bundler is ever wanted. It adds `package.json` and a lockfile to a Python repo for about 5 static files |

**Dockerfile sketch.** The sha256 values are those in the v4.3.3 `sha256sums.txt` on 2026-10-04; re-verify them at the INV-26 checkpoint. `ADD --checksum` needs Dockerfile syntax 1.6+, and the repo already uses `# syntax=docker/dockerfile:1`. Keep every comment on its own line: an inline comment after an instruction breaks the build (see the note at `Dockerfile:30`).

```dockerfile
# syntax=docker/dockerfile:1
# Global build arg, declared before the first FROM. BuildKit sets it per target platform.
ARG TARGETARCH

FROM python:3.14.7-slim-trixie AS uvtool
# ... unchanged ...

FROM uvtool AS tailwind-amd64
ADD --chmod=755 --checksum=sha256:dc61b3ac6b8c9ca874c0cc4c57b2409791a64c5540404ca5f5367360babc313a \
    https://github.com/tailwindlabs/tailwindcss/releases/download/v4.3.3/tailwindcss-linux-x64 /usr/local/bin/tailwindcss

FROM uvtool AS tailwind-arm64
ADD --chmod=755 --checksum=sha256:55fd0b241214eff3de1e8ee4f22796662f2d2e7a49bcfca7477cfd0bac398195 \
    https://github.com/tailwindlabs/tailwindcss/releases/download/v4.3.3/tailwindcss-linux-arm64 /usr/local/bin/tailwindcss

# Stage css: builds the stylesheet. COPY every path the entry file lists in @source.
FROM tailwind-${TARGETARCH} AS css
WORKDIR /app
COPY powermon/web/assets powermon/web/assets
COPY powermon/web/templates powermon/web/templates
COPY powermon/web/static/web/admin.js powermon/web/static/web/admin.js
RUN tailwindcss -i powermon/web/assets/css/app.css -o /out/app.css --minify

FROM uvtool AS base
# ... uv sync, then COPY . /app (Dockerfile:16) ...
# The built CSS goes in AFTER the source copy and BEFORE collectstatic (Dockerfile:18).
COPY --from=css /out/app.css powermon/web/static/web/build/app.css
# ... RUN APP_BUILD=1 python manage.py collectstatic --noinput ...
```

BuildKit builds only the `tailwind-*` stage that matches the target platform. The `macos-arm64` and `macos-x64` binaries exist too, so the watcher can also run directly on a Mac.

**CSS entry point** (`powermon/web/assets/css/app.css`). Auto-detection is turned off so the build is deterministic:

```css
@import "tailwindcss" source(none);
@import "./fonts.css";                                /* @font-face rules, see §4 */
@source "../../templates";                            /* @source paths are relative to this file */
@source "../../static/web/admin.js";                  /* classes toggled by JS */
@plugin "@tailwindcss/forms" { strategy: "class"; }   /* no global form reset; opt in with form-input etc. */
@plugin "@tailwindcss/typography";                    /* only if long help text uses `prose` */
@custom-variant dark { /* see §5 */ }
@theme { --font-sans: "Inter Variable", ui-sans-serif, system-ui, sans-serif; /* + TailAdmin tokens */ }
@layer base { [x-cloak] { display: none !important; } }
```

Why `source(none)`: the v4.3.3 scanner walks hidden directories (`builder.hidden(false)`) and honours `.gitignore` even without `.git` (`require_git(false)`, `crates/oxide/src/scanner/mod.rs:723-731`). Explicit sources avoid scanning `.venv`, `.claude/` or `.planning/` by accident.

**Dev loop.** The local stack bakes the code and `collectstatic` into the image with no source bind mount (`docker-compose.local.yml:15-20`). A UI override compose file adds:
- a `css` service (`target: css`) running `tailwindcss -i powermon/web/assets/css/app.css -o powermon/web/static/web/build/app.css --watch=always` with the volume `./powermon:/app/powermon`. Without `=always`, watch mode exits when stdin closes in a container (CLI help text, v4.3.3).
- a `web` override with `DEBUG=1` and the volume `./powermon:/app/powermon`. Do **not** mount `.:/app`: that hides the image's `/app/.venv` (`Dockerfile:11-15`). Local only, published on 127.0.0.1. Django's technical error pages echo POST data (a typed bot token) and use inline script and style, so the UAT console checks (`TEST-STRATEGY.md` §11 item 1) run with DEBUG off.
- Template reloads: with no `loaders` option (`powermon/settings.py:69-82`), Django 5.2 uses the cached template loader even with DEBUG on, and only `runserver`'s autoreloader clears it. gunicorn `--reload` restarts on Python changes only. So the dev `web` override either runs `python manage.py runserver 0.0.0.0:8000`, or the container is restarted after template edits. Production keeps gunicorn.

With `DEBUG=1`, WhiteNoise's `USE_FINDERS` and `AUTOREFRESH` default to True and serve the rebuilt file, and the manifest storage returns unhashed URLs. Add `powermon/web/static/web/build/` to `.gitignore`.

## 3. JavaScript and the CSP

**Alpine.** The standard build needs `'unsafe-eval'` because it compiles attribute expressions with `Function` (official docs, `packages/docs/src/en/advanced/csp.md`).

The **`@alpinejs/csp`** build:
- Supports: object and array literals, arithmetic and comparisons, ternaries, `&&`/`||`/`!`, `++`, simple assignments, method calls like `items.push()`, `x-model`, and `Alpine.data()`.
- Does **not** support: nested property assignments (`user.name = 'John'`), arrow functions, destructuring, template literals, spread, globals (`console`, `document`, `window`, `Math`, `JSON`, `parseInt`), or `x-html`.
- The 3.17.4 build contains no `new Function` or `eval(` (checked 2026-10-04). The manifest hash (§7) pins that exact file.

**Rules for Phase 6:**
- Every component is `x-data="sidebar"` (or similar) with its logic registered through `Alpine.data()` in `admin.js`. Attributes only reference properties and methods. Port TailAdmin's behaviour, not its attributes.
- `admin.js` lives at `powermon/web/static/web/admin.js`: one first-party file, no build, no `import`/`export` (§6 pitfall 6). Components (brief §13): `sidebar`, `theme`, `copy`, `toasts`, submit guard, `tabs`, modal loader, `poll`, relative times (UI-11), chart image fallback (UI-06). Polish components (OFF-after hint N8, error-summary focus N10, throttle countdown N11, filter chips N13, chart lightbox) are optional and named in the plan if built.
- No Alpine plugins. The `sidebar` component reads and writes the collapsed-rail flag in `localStorage` itself, inside `try/catch` (brief §12 Q9; a UI preference, never a secret).
- No inline script, no `on*=` handlers, no `style=` attributes, no `javascript:` URLs (brief §8).
- No Django variable or tag inside any Alpine directive value (`x-data`, `x-init`, `x-show`, `x-text`, `x-model`, `x-effect`, `x-bind:*` / `:*`, `x-on:*` / `@*`). Autoescaping does not protect there: the browser decodes `&#x27;` back to `'` before Alpine reads the attribute, so a location name becomes part of the expression the CSP build evaluates. Server values reach components only through `data-*` attributes read inside `Alpine.data()`, and user data is written with `textContent`.
- No JS-initiated write: every POST is a native form submission. `fetch` is used only for GETs (the status JSON and the confirmation fragments), so `admin.js` never reads the CSRF cookie or token.

Load order (both `defer`, so they run in document order; `admin.js` must be first so its `alpine:init` listener exists when Alpine starts):

```html
<script src="{% static 'web/admin.js' %}" defer></script>
<script src="{% static 'web/vendor/alpine-csp-3.17.4.min.js' %}" defer></script>
```

**How each interaction works under a self-only CSP:**

| Feature | Mechanism |
|---|---|
| Confirmation modals (UI-07, D6-05) | The trigger stays a link to the confirmation page, which is the no-JS and deep-link fallback. The modal-loader component handles only same-origin `a[data-confirm]` links. It fetches the server's confirmation fragment with `fetch(url, {redirect: "manual", headers: {"X-PM-Fragment": "1"}})` and injects only when the response is 200, carries the header `X-PM-Fragment: 1` and has exactly one `[data-testid="confirm"]` root, which it parses with `DOMParser` and moves into a native `<dialog>` (never `innerHTML`, `insertAdjacentHTML`, `outerHTML`, `Range.createContextualFragment` or `document.write`) before calling `showModal()`. Every other outcome (opaque redirect, 404, 5xx, network error, missing header or root) means `location.assign(url)`. The fragment variant of a refusal queues no flash, so the full GET shows it exactly once (brief §14, `DESIGN-DIRECTION.md` §3). `showModal()` gives focus trapping, Esc, an inert background and focus return for free. "Keep …" uses `<form method="dialog">`, which needs no JS. Never `confirm()` |
| Static dialogs | `<button commandfor="d" command="show-modal">` invoker commands (Baseline 2025: Chrome 135, Firefox 144, Safari 26). Zero JS |
| Dropdowns (account menu, row actions) | `popover` + `popovertarget`, positioned with CSS anchor positioning (Baseline since Firefox 147, 2026-01-13). Zero JS. Older browsers fall back to a centred popover |
| Sidebar collapse and mobile drawer | Alpine `sidebar` component; `localStorage` for the collapsed rail; `inert` on the main content while the drawer is open |
| Theme switch (UI-02) | Alpine `theme` component that sets the cookie and `data-theme` (§5). No-JS fallback: the theme POST form |
| Copy to clipboard (UI-08) | Alpine `copy` component: `navigator.clipboard.writeText` (needs a secure context; localhost counts) and an `aria-live` "Copied". Not rendered while the key is masked |
| Tabs (device examples) | Alpine `tabs` (roving `tabindex`, arrow keys); without JS all four blocks render stacked under h3s (`DESIGN-DIRECTION.md` S8) |
| Toasts (UI-09) | Django messages rendered server-side into a `role="status"` region (errors `role="alert"`), so they show without JS. Alpine auto-dismisses success toasts after 10 s and pauses on hover or focus; everything else stays until closed (brief §12 Q7) |
| Submit guard (UI-09) | Alpine component that blocks a second submit and shows the pending state; see §6 pitfall 13 |
| Toggle switches (UI-10) | A submit button styled as a switch inside the existing POST form, which posts the target value. No JS required |
| Live refresh (UI-05) | Alpine `poll` component: `fetch()` of the status JSON every 30 s while `document.visibilityState === "visible"`, backoff after errors, and a "Live updates paused" chip (brief §12 Q6). It updates `textContent` and attributes only. Use `redirect: "manual"` so an expired session shows the paused chip instead of parsing the sign-in page |
| Revealed key and back/forward cache | On a page with `[data-testid="device-key"][data-state="revealed"]`, `admin.js` empties `#device-key` and the four example blocks on `pagehide`; on `pageshow` with `event.persisted` it calls `location.replace()` with the setup URL from a `data-*` attribute, which loads the masked GET. `Cache-Control: no-store` stays on every S8 response and the S9 POST (`DESIGN-DIRECTION.md` §3 S8) |

**CSP** (exactly brief §8; it replaces `powermon/web/middleware.py:13` and keeps the exact-path `/hb` exemption):

```
default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'
```

- `data:` in `img-src` is needed only because `@tailwindcss/forms` draws the select chevron and the checkbox and radio marks as `data:image/svg+xml` backgrounds (`@tailwindcss/forms@0.5.11/src/index.js:165,261,274,305`). Images loaded this way cannot run script.
- `connect-src 'self'` is needed only for UI-05 polling and the modal fragments.
- The chart PNG (UI-06) is a same-origin image, covered by `img-src 'self'`.
- No Trusted Types policy is required. `admin.js` still never uses `innerHTML`: the modal loader parses the fragment with `DOMParser` and moves the root, and scripts parsed that way never run. Injected fragments also obey the CSP: inline scripts, `on*=` handlers and `style=` attributes in them do nothing.
- No nonces are needed, so the custom middleware stays. Django 6.0 has built-in `SECURE_CSP` with nonces, but the project stays on 5.2 LTS (`pyproject.toml:6`, brief §3).

## 4. Fonts and icons

**Font: Inter Variable**, copied in from `@fontsource-variable/inter` 5.3.0 as subsets selected by `unicode-range`. Files go in `powermon/web/static/web/fonts/`.

| File | Size |
|---|---|
| `inter-latin-wght-normal.woff2` | 48,256 B |
| `inter-cyrillic-wght-normal.woff2` (covers Ukrainian ґ U+0490-0491) | 18,748 B |
| `inter-latin-ext-wght-normal.woff2` | 85,068 B |
| `inter-cyrillic-ext-wght-normal.woff2` (covers ₴ U+20B4) | 25,960 B |

- `@font-face` rules live in `powermon/web/assets/css/fonts.css` with `font-weight: 100 900` and `font-display: swap`.
- Their `url()`s are written relative to the **output** file `powermon/web/static/web/build/app.css`, e.g. `url("../fonts/inter-latin-wght-normal.woff2")` (§6 pitfall 4).
- Preload only the latin and cyrillic files: `<link rel="preload" as="font" type="font/woff2" href="{% static … %}" crossorigin>`. `crossorigin` is required even same-origin, or the font downloads twice.
- Use the `tabular-nums` utility for times and durations.
- Inter covers the uk/ru location names (LOC-02). The earlier system-font choice was made partly for Cyrillic (`01-UI-SPEC.md:45`); Inter now covers that.
- If the sketch wants more character than Inter: **Onest** (Cyrillic-first, OFL, updated 2026-08-25), Manrope or Geist all cover Cyrillic. **Outfit is ruled out**: no Cyrillic.
- Monospace for keys and URLs: the system `ui-monospace` stack.

**Icons:**
- **Lucide** 1.52.0 (the pick): ISC, ~2,130 icons, 24 px, 2 px stroke. Set `stroke-width="1.5"` to match TailAdmin's thin icons. It has zap, zap-off, plug-zap, bell-off, wrench, key-round, copy and send.
- Tabler 3.48.0 (MIT, 5,166 outline + 1,054 filled) and Heroicons 2.2.0 (MIT, 324 icons per style, last released 2024-11) were the alternatives.

Copy in only the icons used, as `powermon/web/templates/icons/<name>.svg`. Render them with a `{% icon "zap" class="size-5" %}` tag that inlines the SVG with `aria-hidden="true" focusable="false"` and `stroke="currentColor"`, so the colour follows Tailwind text classes.
- The tag accepts only names that match `^[a-z0-9-]+$` and are in a set of file stems built at import time; it never joins the name into a path. It builds attributes with `format_html` and a literal format string, so the `class` value is escaped. Marking the repo's own SVG file as safe is fine; it is not user data (R1). This module is the only place `mark_safe` is allowed (`TEST-STRATEGY.md` §3.5).
- Icon files stay byte-identical to `lucide-static`; the tag sets `stroke-width`, `class` and `aria-hidden` at render time, so the sha256 matches the npm tarball.
- Icon SVGs carry no `style=` attribute or `<style>` element; the template lint allows the SVG namespace (brief §10).
- An SVG sprite with `<use href>` is possible, but it is less flexible and its behaviour under this CSP was not checked. Inline SVG is the pick.

## 5. Dark mode, accessibility, responsive

**Dark mode without the wrong-theme flash (D6-03, UI-02).** Server-rendered, the chosen approach:
- A `theme` cookie holds `light`, `dark` or `system`; the default is `system`. SameSite=Lax, path `/`, about 1 year, `Secure` in production like the other cookies (R6), not HttpOnly so JS can update it. It is a UI preference, never a secret.
- The Alpine `theme` component writes exactly `theme=<value>; Path=/; Max-Age=31536000; SameSite=Lax`, plus `; Secure` when `location.protocol === "https:"`, the same attributes as the server's `set_cookie`. A cookie written without `Path=/` defaults to the page's directory and leaves several `theme` cookies that the server reads in undefined order. The context processor only reads and allowlists the cookie; only the theme POST sets it; no GET response sets or deletes it.
- A context processor checks the value against that allowlist (anything else means `system`) and the layout renders `<html data-theme="…">`.
- The Alpine `theme` toggle updates the cookie and `data-theme` instantly. The no-JS fallback is a CSRF-protected POST form to the theme endpoint that redirects to `/`, or, if the plan supports it, to a same-host Referer GET page that is not a confirmation route (R2, R8, R14; brief §6.3).
- The error-page layout hard-codes `data-theme="system"`: 500 has no request, and 404/403-CSRF must render the same with or without one (R11).
- No inline script, so no flash and no CSP hash to maintain.

```css
@custom-variant dark {
  &:where([data-theme=dark], [data-theme=dark] *) { @slot; }
  @media (prefers-color-scheme: dark) { &:where([data-theme=system], [data-theme=system] *) { @slot; } }
}
```

- Prefer **semantic tokens** (`--color-surface`, `--color-fg`, `--color-border`, …) redefined under the same selectors and exposed through `@theme inline`, over `dark:` on every element. Flowbite v4 reports ~50% fewer classes that way. The token values are in `DESIGN-DIRECTION.md` §5.
- Set `color-scheme` per theme in the stylesheet (`[data-theme=light]{color-scheme:light}`, `[data-theme=dark]{color-scheme:dark}`, `[data-theme=system]{color-scheme:light dark}`) so native controls and scrollbars match; never through a `style` attribute.
- Use `:where`, not TailAdmin's `:is(.dark *)`, which adds specificity.
- Rejected alternatives: a blocking external `theme.js` in `<head>` reading `localStorage` (CSP-safe, one cached request, but the server cannot know the theme), and an inline script allowed by a `'sha256-…'` CSP hash (breaks whenever the script changes).

**Accessibility (UI-12):**
- `focus-visible:` rings on every control. Reuse TailAdmin's `--shadow-focus-ring`, but darker: 12% alpha is too faint against a white card.
- A skip link, and `aria-current="page"` in the nav (the current base already does this, `powermon/web/templates/base.html:20`).
- `aria-expanded` and `aria-controls` on the hamburger and sidebar toggles.
- `motion-safe:` on transitions.
- Toasts use `role="status"`; errors use `role="alert"`.
- The viewport meta stays `width=device-width, initial-scale=1` with no zoom limits.
- Contrast computed on TailAdmin's palette:

  | Pair | Ratio | Verdict |
  |---|---|---|
  | gray-500 on white | 4.97 | OK |
  | gray-400 on white | 2.58 | Fail; decorative use only |
  | white on brand-500 | 4.84 | Passes AA, barely; prefer brand-600 (6.5) for primary buttons |
  | gray-500 on gray-900 | 3.57 | Fail in dark mode; use gray-400 (6.89) |

**Responsive (UI-01, UI-12):**
- Below `lg` the sidebar becomes an off-canvas drawer with an overlay; at `xl` and above it is a collapsible icon rail.
- Use `h-dvh`, not `h-screen`.
- Tables either sit in `overflow-x-auto` with a sticky first column, or turn into stacked cards on mobile (`hidden md:table` plus a card list), which suits a list of at most 20 locations.
- Touch targets at least 44 px. Forms are single-column on mobile, with a sticky bottom action bar on long edit forms.

## 6. Pitfalls specific to this combination

1. **Class names built in templates are never generated.**
   - Today's templates build classes like `status--{{ row.status }}` (`powermon/web/templates/web/location_list.html:37`, `location_detail.html:46,49`, `location_setup.html:23`).
   - Spell out complete class strings (an `{% if %}` chain in one pill include) or style by a `data-status` attribute with `data-[status=off]:` variants. No class names built in Python (brief §14).
   - `@source inline("…")` with brace expansion is the safelist escape hatch.
2. **Classes kept in Python are not scanned with `source(none)`.** This covers widget `attrs` in `powermon/web/forms.py`. Put the classes in the form widget templates instead.
3. **CSS must be built before `collectstatic`.** Otherwise collectstatic succeeds but requests fail with `ValueError: Missing staticfiles manifest entry` (`django-tailwind-cli/docs/whitenoise.md`).
4. **The CLI does not rewrite `url()` paths.** The v4.3.3 CLI never passes `shouldRewriteUrls` (`@tailwindcss-cli/src/commands/build/index.ts:280-285`), so font `url()`s in imported CSS must be relative to the **output** file. The manifest storage then hashes them; `data:` URIs are skipped.
5. **Do not build with `--map`** unless the `.map` file ships too. The manifest storage follows `sourceMappingURL` comments and fails on missing files. The vendored Alpine minified file contains none (checked 2026-10-04).
6. **ES-module imports break with hashed filenames** unless a storage subclass sets `support_js_module_import_aggregation = True`. Keep `admin.js` as one file with no imports.
7. **Never hard-code `/static/…`** in templates or JS. Pass static URLs to JS through `data-*` attributes rendered with `{% static %}`. Hashed files are cached forever by WhiteNoise, so there is no other cache busting.
8. **The CSP blocks some inline styling:**
   - Blocked: `style="…"` attributes (including `style="--pct:40%"` for progress bars), `<style>` elements, `on*=` handlers, `javascript:` URLs, `setAttribute('style')` and `.style.cssText`.
   - Allowed: setting `el.style.prop` directly (MDN `style-src`). So Alpine's `x-show` and `x-transition` (both use `style.setProperty`) work.
   - Alpine **string** `:style` bindings call `setAttribute('style')` and are blocked (`alpinejs/src/utils/styles.js`). Use the object syntax or classes.
   - For bars and gauges use `<meter>`, `<progress>` or SVG presentation attributes.
9. **`x-cloak` needs its CSS rule** in the stylesheet. Tailwind's preflight does not include it.
10. **TailAdmin's inline Alpine fails silently under the CSP build.** Port behaviour, not attributes.
11. **Use `@tailwindcss/forms` with `strategy: "class"`.** It avoids a global reset fighting the components. Its data-URI backgrounds need `img-src … data:` (§3).
12. **Form styling: use Django's own form templates.**
    - Set `FORM_RENDERER = "django.forms.renderers.TemplatesSetting"`, add `"django.forms"` to `INSTALLED_APPS`, and override `django/forms/widgets/{input,select,checkbox}.html` plus one field-group template (`{{ field.as_field_group }}`).
    - Django 5.x already adds `aria-invalid`, which Tailwind can target with `aria-invalid:border-error-500`.
    - Django's stock `django/forms/field.html` renders `{{ field.help_text|safe }}`. Drop `|safe` in the override: the S6 token help is already a `format_html` SafeString, and the lint bans `|safe`. The `input.html` override renders `value` only from `widget.value`, never from `field.value()` or `form.data`, so `PasswordInput(render_value=False)` keeps the token out after an invalid POST (R3).
    - **django-widget-tweaks** 1.5.1 (supports Django 5.2) would work for one-off attributes, but no new runtime dependency is planned (§0).
    - **crispy-tailwind** is stale: last release 1.0.3 in 2024-02, Django 4.2/5.0 classifiers only, Tailwind v3 era. Avoid it.
13. **Do not set `disabled` on the clicked submit button inside the `submit` handler.** The form's entry list is built after the event, and a disabled submitter is left out, so a button's `name`/`value` (for example a switch's target value) would not be posted. Block repeat submits with a flag plus `aria-disabled="true"` and the pending style.
14. **Preload fonts with `crossorigin`**, preload at most two subsets, and keep `font-src 'self'`.
15. **Pin and checksum every vendored file** (Alpine, fonts, icons, the Tailwind binaries) in the vendor manifest (§7), and have a test check the hashes. Tailwind minor upgrades can change the generated CSS, so keep the exact pin.
16. **The old guard tests are rewritten on purpose, not just deleted** (brief §10, `TEST-STRATEGY.md`):
    - the exact CSP string: `tests/test_walking_skeleton.py:30-33,85,102`, `tests/web/test_security.py:32,185`, and `powermon/web/middleware.py:13`;
    - `"<script" not in html`: 20 asserts across `tests/web/` (e.g. `tests/web/test_templates.py:233,341`), which become "no injected payload / no inline script body";
    - `tests/web/test_templates.py:362` (`web/app.css` in the manifest), which becomes "every asset is a hashed same-origin manifest path";
    - `tests/web/test_css.py` (parses the hand-written `app.css`, px allowlist at line 32): deleted;
    - the rule-5 comment in `powermon/web/templates/base.html:11-14` goes with the old templates;
    - the origin documents get supersede notes (brief §7): `01-UI-SPEC.md:14,45,424`, `.planning/research/STACK.md:31,329`.

    The actual lesson, `docs/v1-lessons.md:333`, only forbids **third-party** runtime assets. This stack respects it, and a test keeps it: no external origin in any template or CSS, and the CSP header equal to the string in §3.

## 7. Vendor manifest

One JSON file, suggested path `powermon/web/assets/vendor-manifest.json` (outside `static/`, so it is not served). One entry per vendored file, each icon included, with these fields:

| Field | Meaning |
|---|---|
| `name` | Package and file, e.g. `@alpinejs/csp dist/cdn.min.js` |
| `version` | Exact version, never a range |
| `source_url` | The exact file URL it was downloaded from at pin time (GitHub release asset, or the jsDelivr `npm/` mirror of the npm package). Never referenced at runtime |
| `sha256` | Hex sha256 of the file as committed |
| `licence` | SPDX id: `MIT`, `OFL-1.1`, `ISC` |
| `path` | Repo path of the file, or for the Tailwind binaries the Dockerfile stage it is fetched in |

Initial entries (sha256 values marked "pin" are recorded at planning time):

| name | version | source URL | sha256 | licence | path |
|---|---|---|---|---|---|
| tailwindcss-linux-x64 | 4.3.3 | `https://github.com/tailwindlabs/tailwindcss/releases/download/v4.3.3/tailwindcss-linux-x64` | `dc61b3ac…c313a` | MIT | `Dockerfile`, stage `tailwind-amd64` |
| tailwindcss-linux-arm64 | 4.3.3 | `…/v4.3.3/tailwindcss-linux-arm64` | `55fd0b24…98195` | MIT | `Dockerfile`, stage `tailwind-arm64` |
| @alpinejs/csp | 3.17.4 | `https://cdn.jsdelivr.net/npm/@alpinejs/csp@3.17.4/dist/cdn.min.js` | pin | MIT | `powermon/web/static/web/vendor/alpine-csp-3.17.4.min.js` |
| @fontsource-variable/inter (4 files) | 5.3.0 | `https://cdn.jsdelivr.net/npm/@fontsource-variable/inter@5.3.0/files/<file>` | pin | OFL-1.1 | `powermon/web/static/web/fonts/<file>` |
| lucide-static (one row per icon) | 1.52.0 | `https://cdn.jsdelivr.net/npm/lucide-static@1.52.0/icons/<name>.svg` | pin | ISC | `powermon/web/templates/icons/<name>.svg` |

- At the INV-26 checkpoint, confirm each npm-sourced file matches the file inside the npm tarball whose `dist.integrity` the registry publishes, and re-read the Tailwind values from the release's `sha256sums.txt`.
- The hash test recomputes the sha256 of every listed repo file, fails on any file under `static/web/vendor/` (except `LICENSES/`), `static/web/fonts/` or `templates/icons/` that is not listed, and checks that the Dockerfile `--checksum` values equal the manifest.

## 8. Licences to ship

Licence texts go in `powermon/web/static/web/vendor/LICENSES/`, copied verbatim from the pinned package or repo:

- `tailwindcss-MIT.txt`: Tailwind CSS, MIT. **Build tool only**: no Tailwind code runs in the browser. It also covers the bundled `@tailwindcss/forms` and `@tailwindcss/typography` plugins (both MIT).
- `tailadmin-MIT.txt`: TailAdmin free edition, MIT, "Copyright (c) 2023 TailAdmin". Required because its tokens and layout patterns are adapted (D6-02).
- `alpinejs-MIT.txt`: Alpine.js (`@alpinejs/csp`), MIT.
- `inter-OFL-1.1.txt`: Inter, SIL Open Font License 1.1.
- `lucide-ISC.txt`: Lucide, ISC. Ship the package's `LICENSE` file as-is; it also credits Feather (MIT) for the icons derived from it.

## Sources (all accessed 2026-10-04)

- github.com/TailAdmin/tailadmin-free-tailwind-dashboard-template (`package.json` 2.4.0, `LICENSE`, `README.md`, `src/css/style.css`, `src/index.html`, `src/js/index.js`, `src/partials/*`); GitHub API metadata (2,342 stars, pushed 2026-09-15)
- tailadmin.com, tailadmin.com/pricing, tailadmin.com/license
- tailwindcss.com/docs (detecting-classes-in-source-files, dark-mode, adding-custom-styles, functions-and-directives, compatibility); github.com/tailwindlabs/tailwindcss v4.3.3 release, `sha256sums.txt`, `@tailwindcss-standalone/src/index.ts`, `@tailwindcss-cli/src/commands/build/index.ts`, `crates/oxide/src/scanner/mod.rs`
- `@tailwindcss/forms` 0.5.11 README and `src/index.js`
- github.com/django-commons/django-tailwind-cli (`docs/settings.md`, `docs/whitenoise.md`, `config.py`, `_download.py`)
- alpinejs.dev/advanced/csp and alpine `packages/docs/src/en/advanced/csp.md`, `src/utils/styles.js`, `src/directives/x-show.js`
- htmx.org/docs (2.x); `htmx.org@2.0.11/dist/htmx.js`; four.htmx.org/docs/whats-new-in-htmx-4; four.htmx.org/extensions/hx-csp; four.htmx.org/announcements/2026-08-28-htmx-4.0.0-is-released; morello.dev/blog/htmx-4
- developer.mozilla.org CSP `style-src`; docs.djangoproject.com/en/6.0/releases/6.0/; whitenoise.readthedocs.io (6.12.0)
- Fontsource API (`api.fontsource.org/v1/fonts/{outfit,inter,onest,geist,manrope}`); jsDelivr listing for `@fontsource-variable/inter@5.3.0`; github.com/rsms/inter releases (v4.1, 2024-11-16)
- npm registry (tailwindcss, alpinejs, @alpinejs/csp, htmx.org, lucide-static, @tabler/icons, heroicons, apexcharts, flowbite, preline, daisyui, basecoat-css); PyPI (django-tailwind-cli, django-widget-tweaks, crispy-tailwind, django-cotton, django-unfold, whitenoise)
- `apexcharts@7.8.0/LICENSE`; github.com/hunvreus/basecoat; daisyui.com/docs/install/standalone; github.com/htmlstreamofficial/preline LICENSE discussion; penguinui.com; github.com/thedevdojo/pines
- Invoker commands Baseline 2025: dev.to/grimicorn, blog.openreplay.com/invoker-commands-api-guide. Anchor positioning Baseline (Firefox 147, 2026-01-13): refontelearning.com/blog/css-anchor-positioning-reaches-baseline
- tailwindcss.com/blog/vanilla-js-support-for-tailwind-plus; flowbite.com/docs/getting-started/changelog
