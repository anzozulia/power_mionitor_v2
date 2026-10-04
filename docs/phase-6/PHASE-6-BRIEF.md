# Phase 6 — Admin UI rebuild: kickoff brief

**Date:** 2026-10-04 · **Owner:** maintainer · **Status:** scope and key decisions approved by the maintainer
**Companion docs (this folder):** `ADMIN-INVENTORY.md` (parity contract), `DESIGN-DIRECTION.md`, `FRONTEND-STACK.md`, `TEST-STRATEGY.md`, `PROJECT-STATUS.md`, `before/` (screenshots of the current admin)

---

## 0. How to use this document (for GSD agents)

- This brief is the **primary input** for `/gsd-sketch`, `/gsd-discuss-phase 6`, `/gsd-ui-phase 6`, `/gsd-plan-phase 6` and the Phase 6 verifier, reviewer and UI auditor (README.md gives the order and the exact commands).
- **Precedence:** maintainer decisions (§5) > this brief > `06-CONTEXT.md` and `06-UI-SPEC.md` once written > `DESIGN-DIRECTION.md` / `FRONTEND-STACK.md` / `TEST-STRATEGY.md` > the old UI contracts `01-UI-SPEC.md`, `04-UI-SPEC.md`, `05-UI-SPEC.md`, and the admin-UI rules in `01/04/05-CONTEXT.md`. 06-CONTEXT.md and 06-UI-SPEC.md absorb the `/gsd-sketch` winner. They may change tokens, colours, radii, shadows, density, spacing and the type scale, but never §5, §6 or R1–R16.
- **Clean slate.** Phase 6 **replaces** the admin frontend; it does not extend it. The Phase 6 UI-SPEC **supersedes** 01/04/05-UI-SPEC for everything visual, interaction and asset-related. Only their **security-bound rules** survive (listed in §6.3). Do not inherit "no JavaScript", "system fonts", "no build step", "one CSS file ≤ 300 lines", "frozen CSS", "no icons", "light only", "no live refresh", "one accent button per page" or the old CSP string (`default-src 'none'; style-src 'self'; img-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'`, which allows no script, font or fetch) — see §7 for why they existed and why they are void. The §8 policy still starts with `default-src 'none'`: keep that directive.
- **Keep the server.** URLs, HTTP methods, status codes, POST → redirect → GET flows, view-model helpers, engine/action functions and Python-owned copy stay as they are unless a requirement below needs a change. Phase 6 is a presentation rebuild plus a small set of new UI capabilities, not a backend rewrite.
- Phase 6 requirement IDs are `UI-01` … `UI-13` (§4). Only UI-01…UI-13 go in a plan's `requirements:` frontmatter or in bold `**ID**` form. Cite INV-xx, K-x and R1–R16 in plain text, because GSD's requirement scanners match any bold `[A-Z][A-Z0-9]*-\d+`. The old decision IDs `UI-D1`…`UI-D15` (Phase 4 UI-SPEC) and `UI5-Dn` (Phase 5) are unrelated to UI-01…UI-13 and are superseded: never read UI-D3 as UI-03.

## 1. Goal

As the admin, I want a beautiful, modern, pleasant-to-use admin panel — TailAdmin-like: sidebar layout, cards, status pills, icons, light and dark themes, modals, toasts, copy buttons and live status — rebuilt from scratch on a proper Tailwind-based frontend, so that day-to-day work (checking status, setting up devices, fixing problems) is fast and enjoyable, **without weakening any behaviour or security guarantee of phases 1–5**.

## 2. Why

> "The admin panel is ugly as hell. It has no visual bugs, it gets the job done — but it looks like the design budget was 10 dollars and 2 beer cans. I want a really beautiful (probably Tailwind-based, something like tailadmin.com) admin panel that is pleasing to use. … We are not reshaping/improving the admin frontend part — we are throwing out ALL that was done on that matter and re-creating all from scratch, as it should be this time." — maintainer, 2026-10-04

Observed problems (see `before/`):
- **Layout:** a narrow 960 px column with no branding or icons, and only one navigation item.
- **List:** a plain table with underlined links, a tiny dot for status, raw timestamps and raw error codes (`http_400`). There is no at-a-glance fleet health.
- **Location page:** one ~1,750 px scroll with 8 stacked sections. Each switch is a paragraph plus an outline button. A 24-row outage table with plain "Remove" links. Destructive actions look like harmless ones.
- **Warnings:** important ones, such as the missing ops chat, are a low-contrast grey box; warning flashes look like success.
- **Phones:** location names break mid-word, pills split and tables are cramped.
- **UI reviews:** 17/24 and 18/24, both done blind without screenshots. None of their fixes were applied, because UI5-D12 froze the CSS.

## 3. Scope

### In scope
1. **Rebuild every existing screen and state from scratch:** S1–S13 and E1–E3 in `ADMIN-INVENTORY.md`.
   - Screens: sign-in (incl. throttled 429), locations list, add, location page, edit, device setup (masked, revealed, after regenerate), delete, regenerate-key, remove-outage and reset-history confirmations, switch and test-message actions, 404 / 403-CSRF / 500.
   - Every action, flash and conditional state listed there must exist in the new UI.
2. **New frontend foundation:**
   - Tailwind CSS v4 build, design tokens and dark mode.
   - Self-hosted font, icons and JS.
   - Component partials and form rendering.
   - Updated CSP.
   - A new test hook contract.
3. **New capabilities** approved by the maintainer (UI-02…UI-06, UI-07 modals, UI-08 copy, UI-09 toasts, UI-10 toggles, UI-11 relative times).
4. **Remove the old frontend completely:**
   - `app.css`, the old templates, `tests/web/test_css.py`, the old CSS-literal and class-coupled assertions.
   - The superseded constraint notes in planning docs (§7).

### Out of scope for Phase 6
- Admin UI localisation (stays English-only).
- Search or command palette, a notification centre, multiple admins or roles.
- Analytics dashboards, and JS chart libraries (ApexCharts and similar). The weekly PNG preview (UI-06) is the only chart.
- PWA / offline.
- Any change to engine, alerts, worker, chart rendering or Telegram behaviour, except read-only endpoints needed by UI-05 and UI-06.
- htmx in any version (no htmx at all; §8), and a Django 6 upgrade (Django 6's built-in CSP is not needed).
- Copying markup from the paid TailAdmin **Pro** demo (demo.tailadmin.com). Only the MIT **free** edition may be adapted.

## 4. Requirements (new, Phase 6)

Add these to `.planning/REQUIREMENTS.md` under a new category **"Admin UI (UI)"** and map them all to Phase 6.

- [ ] **UI-01**: Admin works in a rebuilt, responsive app shell from 360 px phones to wide desktops.
  - Sidebar navigation (an off-canvas drawer on small screens).
  - A top bar with breadcrumbs, the theme switch and an account menu containing Sign out.
  - Every existing screen, action, flash and state from `docs/phase-6/ADMIN-INVENTORY.md` works in it.
- [ ] **UI-02**: Admin can switch between Light, Dark and System themes. The choice persists in that browser, and pages never flash the wrong theme on load.
- [ ] **UI-03**: Admin sees every location in the sidebar with its status indicator and can open any location in one click.
- [ ] **UI-04**: Admin sees a fleet summary at the top of the Locations page: how many locations are on, off, in maintenance, waiting for their first heartbeat, and with failing delivery.
- [ ] **UI-05**: Admin sees status, last heartbeat and delivery health update without reloading the page.
  - While the tab is visible, each change appears at most 35 s after the server records it, on the Locations page, the location page and the device-setup page.
  - The setup page shows "first heartbeat received" as soon as it arrives.
  - The live data never contains a device key or bot token, not even masked.
- [ ] **UI-06**: Admin sees the location's current weekly chart on the location page: the same image the channel's pinned chart shows.
- [ ] **UI-07**: Admin confirms delete location, reset history, remove outage and regenerate key in a modal.
  - The modal shows the same server-side confirmation content and performs the same server-side checks as today.
  - The full confirmation pages still work when opened directly or without JavaScript.
- [ ] **UI-08**: Admin can copy the heartbeat URL, the revealed device key and each device example with one click and gets visible confirmation. Copy is unavailable while the key is masked.
- [ ] **UI-09**: Admin gets clear feedback for every action: a pending state while it runs and a toast when it finishes.
  - Toast tones: success, info, warning, error. Warnings look different from success.
  - Screen readers announce the toast.
  - Buttons show a pending state that prevents accidental double submits.
- [ ] **UI-10**: Admin flips Maintenance, Alerts and Router grace with toggle switches. The semantics stay the same: an idempotent POST of the target value, and the existing flashes.
- [ ] **UI-11**: Admin sees relative times ("12 s ago", "3 h ago") next to the absolute times for heartbeats, on-since/outage-since and delivery incidents.
- [ ] **UI-12**: The admin passes these WCAG 2.2 AA checks in both themes: contrast, focus, keyboard operation, touch targets, labelled tables, announced messages and 360 px reflow.
  - Text contrast, visible focus, and keyboard operation of menus, modals and tabs.
  - Touch targets of at least 44 px, labelled tables and announced messages.
  - No page-level horizontal scroll at 360 px, and location names wrap at word boundaries (the sidebar list may truncate a long name with an ellipsis, as long as the full name stays in the DOM and in `title`).
- [ ] **UI-13**: Every frontend asset (CSS, JS, fonts, icons) is self-hosted and reproducible.
  - Each asset is pinned, and its checksum and licence are recorded.
  - Assets are built inside the Docker image and served as hashed files with DEBUG off.
  - The CSP allows only same-origin scripts, styles and fonts, with no `unsafe-inline` or `unsafe-eval`.
  - No page references a third-party origin.

## 5. Maintainer decisions (locked for Phase 6)

| ID | Decision |
|---|---|
| D6-01 | **Throw out and rebuild** all admin templates, styles and frontend tests. No reuse of the old CSS or markup. |
| D6-02 | **Tailwind-based, TailAdmin-like** look. Use TailAdmin **free** (MIT) as the visual reference, and its `@theme` tokens may be copied. Rewrite the markup: TailAdmin's inline Alpine fails under a strict CSP, its sidebar has no focus styles, its viewport line blocks zoom, its badge contrast fails AA, and it pulls Google Fonts and ApexCharts. Ship the MIT notice. |
| D6-03 | **Dark mode: Light / Dark / System**, default System, server-rendered from a cookie so there is no flash. |
| D6-04 | **All four extras approved:** fleet summary tiles (UI-04), live status refresh (UI-05), weekly chart preview (UI-06), sidebar location list (UI-03). |
| D6-05 | **Confirmations:** native `<dialog>` modal whose body is the server's existing confirmation (GET pre-checks, HMAC marker, state blocks). The 4 confirmation pages stay as the no-JS / deep-link fallback. A client-side `confirm()` is never acceptable. |
| D6-06 | **Visual direction is locked by `/gsd-sketch` before `/gsd-ui-phase 6`.** Make 2–3 variants and let the maintainer pick. The sketch findings feed the UI-SPEC. |
| D6-07 | **JavaScript, a build step and web fonts are welcome**, but only self-hosted (v1-lessons §4: no third-party runtime assets). |

## 6. What must not change (the contract)

### 6.1 Behaviour
- **Routes and responses:** all URLs, methods, status codes and redirect targets in `ADMIN-INVENTORY.md` §1a and §2 stay as they are.
- **The no-JS path must keep working:** every state change stays a plain `<form method="post">` with `{% csrf_token %}` and POST → redirect → GET. JavaScript only enhances it. Concretely:
  - with JS disabled, every action still works;
  - a failed JS load never blocks the admin.
- Switches still post the **target value** (idempotent), and repeats still answer "Nothing changed."
- Reveal and Regenerate still answer **200 with the setup page**, `Cache-Control: no-store`, as top-level navigations. There is no in-place fetch reveal.
- Time formats come from `display_time`: full `YYYY-MM-DD HH:MM:SS TZ`, compact `YYYY-MM-DD HH:MM`. Relative times (UI-11) are **added** next to them, never replace them.

### 6.2 Copy
- **Python-owned copy is kept** (flashes, form errors, help texts, status labels, cause lines and the throttle message), **except the pending amendments in the third bullet**. Those deliberately change a few Python-owned strings in `powermon/web/location_views.py` and `powermon/web/history_views.py`, as well as some template prose (`DESIGN-DIRECTION.md` §7 Q14).
- **Template prose may be restructured:** for example, long help can move into a disclosure or popover, and intro sentences can be shortened. Its **meaning must be kept**, especially warnings and consequences.
- **Apply the pending copy amendments** from 04/05-UI-REVIEW (list in `DESIGN-DIRECTION.md` Q14 and `ADMIN-INVENTORY.md` §3).
- The setup page keeps three things (01-UAT #9): the curl example labelled "recommended", the `?key=` examples labelled, and the link-previewer warning.

### 6.3 Security-bound rules (binding; the IDs are `ADMIN-INVENTORY.md` §4 R1–R16)

| Rule | Requirement |
|---|---|
| R1 | Auto-escaping everywhere; no `\|safe` / `mark_safe` / `autoescape off` on user data |
| R2 | Every write or secret reveal is a CSRF POST; GET never writes |
| R3 | The bot token never enters HTML, and has no "show" eye |
| R4 | The full device key appears only in the Reveal and Regenerate POST responses (`no-store`). It never appears in fragments, JSON, the sidebar or the live endpoint, and never in any browser storage (sessionStorage, localStorage, IndexedDB, Cache Storage, a service worker, history state) |
| R5 | No third-party assets, and the CSP is sent on every response except `/hb` |
| R6 | Framework security headers and cookies unchanged |
| R7 | A server-side confirmation step for destructive actions |
| R8 | No open redirect. This includes the new theme fallback POST, which redirects to the fixed `/`, or, if the plan supports it, to the path of a same-host Referer that is a GET page other than the four confirmation routes (a confirmation or POST-result URL maps to its parent S5 or S8 page) |
| R9 | 429 throttle semantics |
| R10 | Short error causes only |
| R11 | Error pages echo nothing and use one error layout that reads no context variable (Django renders 404 and 403-CSRF with the request, so context processors run there; 500 has no context). So no sidebar, toasts or theme cookie on error pages. |
| R12 | `start_us` is never echoed |
| R13 | The heartbeat URL comes from `PUBLIC_BASE_URL` |
| R14 | Default-deny login, which covers new endpoints automatically |
| R15 | `noindex` |
| R16 | Stale forms never revert state |

**New surfaces** — the status JSON, the chart PNG, modal fragments and the theme POST:
- login-required;
- GET-only for reads;
- `never_cache`, or a short private cache for the PNG;
- **no secrets**;
- added to the INV-23 secret-scan matrix;
- the fragment header `X-PM-Fragment` is honoured only by the four confirmation GET views, never by a middleware, base template or context processor, and a refused fragment request queues no flash (`DESIGN-DIRECTION.md` §3).

## 7. Superseded constraints and the planning amendments they need

**Why the strictness existed.** `docs/v1-lessons.md` §4 said: "Admin pages that show secrets load no third-party runtime scripts; self-host the CSS and JS." The legacy app had loaded the Tailwind Play CDN without SRI. Here is how that became "no JavaScript":
1. **Stack research** (`.planning/research/STACK.md:31,329`, `SUMMARY.md:64`) narrowed the lesson to "one self-hosted CSS file, no JS build".
2. **`01-UI-SPEC.md`** filled the remaining gaps with *defaults* ("chosen here because nothing upstream answers it"): no icons, no web fonts, light only, no `<script>`, a 300-line CSS cap, and a CSP with no `script-src`, `font-src` or `connect-src`.
3. **Later phases** inherited these defaults as if they were decisions. 04-CONTEXT D-13 even misattributes "no JavaScript" to Phase 1 D-09.

The maintainer never chose any of this. **Only the lesson's actual rule survives: no third-party runtime assets.**

**Amendments to make before planning.** They are applied by the kickoff prompt in `README.md` step 2.

| File | Amendment |
|---|---|
| `.planning/REQUIREMENTS.md` | Add the "Admin UI (UI)" category with UI-01…UI-13. Change "Scope is closed: 49 requirements" to "49 v1 requirements + 13 Phase 6 UI requirements (UI-01…UI-13, added 2026-10-04 by the maintainer)". In Out of Scope, remove **"or themes"** from the admin-extras row (localisation stays out), change "Web charts or analytics in the admin panel (beyond the recent-outages list in DATA-02)" to "… beyond the recent-outages list (DATA-02), the weekly chart preview (UI-06) and the fleet summary (UI-04)", and add a row for the §3 exclusions (search, notifications, PWA, JS chart libraries, htmx). Add traceability rows UI-01…UI-13 → Phase 6. |
| `.planning/REQUIREMENTS.md` Coverage block | 62 total (49 v1 + 13 Phase 6 UI); Phase 6: 13; the footer date. |
| `.planning/PROJECT.md` | Add Key Decision **KD8 "Admin frontend stack"** (FRONTEND-STACK.md §0, D6-01…D6-07) with Outcome "— Pending". Add UI-01…UI-13 to Active. Add `docs/phase-6/` to the Reference docs. Do **not** change the "no paid services / Telegram is the only external dependency" constraint: every asset is self-hosted. |
| `.planning/PROJECT.md` Out of Scope | The same row edits as REQUIREMENTS.md: PROJECT.md keeps its own copy of both rows (lines ~98 and ~100), and discuss-phase reads PROJECT.md first. |
| `.planning/research/STACK.md` (lines ~31, ~329), `.planning/research/SUMMARY.md` (~64) | Append the note to the last cell of the named table row, keeping the row on one line: "Superseded for the admin UI by Phase 6 (docs/phase-6/FRONTEND-STACK.md, KD8). The 'avoid Tailwind CDN / third-party runtime assets' rule still holds." |
| `.claude/CLAUDE.md` (lines ~66, ~214) | The same supersede note, appended to the last cell of the same two rows, so agents stop re-imposing "no JS / one CSS file". Lines 40–291 are a GSD-generated block (`<!-- GSD:stack-start source:research/STACK.md -->`), rebuilt from STACK.md tables and bullets only: edit STACK.md first, so a later regeneration already carries the note, and never insert a separate line between table rows. |
| `01-UI-SPEC.md`, `04-UI-SPEC.md`, `05-UI-SPEC.md` | Add a line directly under the H1, below the YAML frontmatter (never above its first `---`): "Visual, interaction and asset rules superseded by 06-UI-SPEC.md (Phase 6). Security-bound rules remain binding via docs/phase-6/PHASE-6-BRIEF.md §6.3." |
| `01-CONTEXT.md`, `04-CONTEXT.md`, `05-CONTEXT.md` | A note under the H1 saying their no-JavaScript, one-CSS-file / no-JS-build and confirmation-page rules are superseded for the admin UI. 04-CONTEXT D-13/D-17 and 05-CONTEXT "Carried from prior phases (binding)" state them as binding, and discuss and the planner read both files. |
| `.planning/ROADMAP.md` | Add Phase 6 (README steps 1–2, entry text provided): move the entry before `## Progress`, and add the checklist bullet under `## Phases`, the `→ 6` in the execution order, the Progress row and one Overview sentence. |

README.md step 2 holds the exact prompt; where it is more detailed than this table, it wins.

## 8. Frontend stack (KD8; details and pins in `FRONTEND-STACK.md`)

| Layer | Choice |
|---|---|
| CSS | **Tailwind CSS v4.3.3 standalone CLI** (MIT): a single binary pinned by sha256, run only in a Docker build stage (`css`, before `collectstatic`) and in a local dev watcher. No Node in the runtime image. Includes `@tailwindcss/forms` (`strategy: "class"`) and `@tailwindcss/typography`. Entry `powermon/web/assets/css/app.css` with `@import "tailwindcss" source(none)` and explicit `@source` paths. |
| Tokens | TailAdmin-derived `@theme` (starting point; the `/gsd-sketch` winner decides brand and neutrals, §12 Q1–Q2): brand indigo-blue `#465FFF` / `#3641F5`, cool grays, `shadow-theme-*`, focus ring. **Semantic tokens** (surface, fg, border…) are redefined for dark mode. Status tokens reuse the chart's hues (on `#62C28A`, off `#CC3434`, not-monitored hatch `#A09D94` on `#E2E0DA`) with darker text shades for AA (`DESIGN-DIRECTION.md` §5). |
| JS | Native `<dialog>`, invoker commands and the Popover API; **`@alpinejs/csp` 3.17.4** (MIT) for components; one first-party `admin.js` registering `Alpine.data()` components. **No htmx**: one library is enough, and htmx's history snapshots could copy secret pages into sessionStorage. No inline script, no `on*=`, no `style=` attributes. |
| Font | **Inter Variable** woff2 (latin + cyrillic + their -ext subsets; `@fontsource-variable/inter` 5.3.0, OFL-1.1). It matches the chart's Inter. Outfit is ruled out: no Cyrillic. |
| Icons | **Lucide** (`lucide-static` 1.52.0, ISC). Copy only the ~30 SVGs needed into `templates/icons/` and render them inline with an `{% icon %}` tag. |
| Forms | Django `TemplatesSetting` form renderer with project widget and field-group templates. Keep the existing `aria-describedby` / `aria-invalid` wiring. No crispy. |
| Static | WhiteNoise `CompressedManifestStaticFilesStorage`, unchanged. The CSS is built before `collectstatic`. Use `{% static %}` everywhere and never hard-code `/static/`. |
| CSP | `default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'`. `data:` is needed only for `@tailwindcss/forms` SVG backgrounds; `connect-src` only for UI-05 polling and modal fragments. |
| Supply chain | A vendor manifest listing name, version, URL, sha256 and licence for every vendored file, plus a test that verifies the hashes. Licence texts go in `static/web/vendor/LICENSES/`. New binaries and files go through the INV-26 legitimacy checkpoint, like Pillow/Inter in Phase 3. |

## 9. Design direction (summary; full spec in `DESIGN-DIRECTION.md`)

- **Principles:**
  - Status readable in two seconds.
  - Calm by default, loud only when something is wrong. Red means power OFF or danger, nothing else; delivery failing uses its own orange tone.
  - One visual language with the Telegram chart.
  - Secrets look different from everything else.
  - Sized for one admin and at most 20 locations.
  - Accessible by construction.
- **Shell:**
  - Sidebar: brand, Locations, Add location, the location list with status dots (UI-03), and an ops-chat warning chip.
  - Sticky top bar: breadcrumbs, theme switch, account menu.
  - A banner slot and the page-header pattern; flashes become toasts.
- **Locations:** fleet tiles (UI-04); a card-wrapped table whose rows are fully clickable, with status pills and tags, relative plus absolute heartbeat time, and a delivery pill; stacked cards on mobile; filter chips optional.
- **Location page:** a **card grid with an anchor sub-nav**, not tabs.
  - Header: name, status pill, tags, meta line, actions.
  - Banners for delivery failing (with the fix button) and for maintenance.
  - Cards: Status, Controls (toggles plus test message), Weekly chart (UI-06), Recent outages (icon row actions), Settings, Device setup, and a red-bordered Danger zone.
  - The DOM order of the old sections is preserved for robustness.
- **Forms:** sectioned cards (Basics / Monitoring / Telegram), a live "Reported OFF after N s" hint, an error summary with jump links, a write-only token field and a sticky action bar.
- **Device setup:** a numbered guide.
  1. Before you start
  2. URL with copy
  3. Key: masked → Reveal (POST) → copy / Hide
  4. Examples as tabs with copy
  5. Live "waiting for first heartbeat" (UI-05)
- **Confirmations:** a `<dialog>` loading the server fragment (D6-05). "Keep …" comes first and gets initial focus; the destructive button is last and shows a spinner.
- **Error pages:** a standalone error layout, with the theme taken from the system media query and no context variables (R11).

## 10. Testing approach (details in `TEST-STRATEGY.md`)

- **Behaviour and security suites stay untouched** and are the regression net. About 196 of the 351 web test functions need no change. A changed status code, redirect, `Cache-Control`, cookie or secret-scan result is a regression, not a style change.
- **Wave 0** builds the test infrastructure before any template is written:
  - a **test hook contract** in the UI-SPEC: semantic hooks plus `data-testid` on data regions;
  - one shared HTML parsing module (`tests/web/pages.py`) replacing ~30 copy-pasted regex helpers;
  - a guard test that fails if any test asserts on `class="`.
- **Migrate each page's tests in the same plan as its template,** so the suite is green at every commit.
- **Rewrite the policy tests on purpose; do not just delete them:**
  - the CSP string (3 places);
  - `"<script" not in html`: 20 asserts, which become "no injected payload / no inline script body";
  - "exactly one `<link>`" becomes "every asset is a hashed same-origin manifest path";
  - the template lint keeps `|safe`, `on*=` and `style=` bans and allows the SVG namespace;
  - delete `test_css.py`.
- **Add:**
  - a render matrix for every route × state (~35 cases);
  - asset and supply-chain checks;
  - CSP negative assertions (no `unsafe-inline`, no `unsafe-eval`, no `*`, no `http(s):`);
  - tests for the new endpoints, including no secrets in JSON or fragments.
- **Coverage gate:** add `powermon/web/*.py` (except `gunicorn_conf.py`) to the `--fail-under=80` include list in README and `config.json`.
- **Browser checks:**
  - the maintainer captures signed-in screenshots of every screen in light and dark at 1440 px and 360 px into `.planning/ui-reviews/06-manual/` and points `/gsd-ui-review 6` at them (README "Before you start" item 4 and step 6). The auditor's own static capture only probes ports 3000/5173/8080 for a 200 on `/`, so it finds nothing on its own (the earlier reviews were blind), and `workflow.ui_interaction_capture` does not change that;
  - UAT includes a manual console check for CSP violations, JS errors and both themes.
  - Playwright is optional (Q4).

## 11. Success criteria (observable; for the Phase 6 roadmap entry and the verifier)

The README "Roadmap entry" carries the same six criteria, one line each.

1. **Parity (UI-01, UI-07).** Every screen and state in `ADMIN-INVENTORY.md` renders in the new design in light and dark.
   - Every action and flow works with JavaScript on.
   - Every form and destructive flow also works with JavaScript off.
   - The behaviour and security suites pass unchanged in meaning.
2. **Security (UI-13; R1–R16).** All R1–R16 checks pass, and the CSP header equals the §8 policy exactly.
   - Zero CSP violations or JS errors in the browser console on every page in both themes.
   - No third-party origin anywhere.
   - The secret scans pass on every page, fragment, JSON response and the chart PNG route.
3. **Live data and preview (UI-05, UI-06).**
   - Unplugging a device shows Off on an open Locations page and location page within the detection window plus 35 s, without a reload.
   - The setup page flips to "first heartbeat received" on its own.
   - The chart preview matches the channel's chart.
4. **Mobile and accessibility (UI-01, UI-12).**
   - At 360 px: no page-level horizontal scroll, names wrap at word boundaries, the drawer works, and touch targets are at least 44 px.
   - Keyboard-only use of the menu, the modals, the tabs and the theme switch.
   - AA contrast in both themes.
5. **Build and quality (UI-13; maintainer decision D6-01).**
   - The image builds reproducibly with pinned, checksummed assets, and DEBUG-off serves hashed files.
   - The full gate is green with `powermon/web` at ≥ 80% coverage.
   - The old `app.css`, the old templates and `test_css.py` are gone.
6. **New capabilities (UI-02, UI-03, UI-04, UI-07, UI-08, UI-09, UI-10, UI-11).**
   - Light, Dark and System each persist across reloads in that browser, no page flashes the wrong theme on load, and System follows an OS theme change.
   - The sidebar lists every non-deleted location with its status and opens any of them in one click.
   - The Locations page shows on, off, maintenance, waiting and delivery-failing counts that match the table.
   - Delete, reset history, remove outage and regenerate key open a modal with the server's confirmation content and refusals; the four pages still work when opened directly.
   - The heartbeat URL, the revealed key and each example copy with one click and a visible confirmation; there is no key copy while the key is masked.
   - Every flash appears as a toast in its tone (warnings distinct from success) and is announced by a screen reader; submit buttons block double submits.
   - Maintenance, Alerts and Router grace are toggle switches that post the target value; a repeat still answers "Nothing changed."
   - Heartbeats, on/outage-since times and delivery incidents show a relative time next to the unchanged absolute time.

**Acceptance (after execution, not a verifier must-have):** `/gsd-ui-review 6` scores ≥ 21/24 with no pillar below 3, from the manual screenshots described in README "Before you start" item 4. The phase verifier runs inside `/gsd-execute-phase` before any UI review exists, so this cannot be one of its must-haves.

## 12. Open questions for `/gsd-discuss-phase 6` (recommended default first)

Q1 and Q2 are **decided by the `/gsd-sketch` winner (D6-06)**, which runs before discuss (README step 3). Discuss records the winner's values and never locks different ones. Q3–Q11 are confirmed in discuss.

1. **Brand colour.** The sketch's starting point is TailAdmin indigo-blue `#465FFF` / `#3641F5` (Variant A). Other variants may propose alternatives; the maintainer picks.
2. **Neutrals.** The sketch's starting point is the cool TailAdmin grays. The alternative is the chart's warm stone.
3. **HTML parsing in tests.** `beautifulsoup4` + `soupsieve` as **dev-only** dependencies, through the supply-chain checkpoint. The alternative is a small stdlib `html.parser` helper.
4. **Playwright.** Not in Phase 6: use the UI review with real screenshots captured by hand (README "Before you start" item 4) plus manual console checks. Revisit if JS regressions appear.
5. **Chart preview cost.** Render on request with a short per-location cache (e.g. 60 s), login-required and `private`. The alternative is to store the last rendered PNG from the worker. Check the memory budget on a 1–2 GB VPS (render ≈ +25 MB transient).
6. **Live refresh transport.** A ~30-line Alpine `poll` component fetching `GET /locations/status.json` every 30 s while visible, with backoff after errors and a "Live updates paused" chip. The alternative is server-rendered HTML fragments.
7. **Toast lifetime.** Success toasts auto-dismiss after 10 s and pause on hover or focus. Everything else stays until closed, and views mark instructive successes `sticky`.
8. **Filter chips on the list.** Include them only if cheap; they are low value at 20 rows or fewer.
9. **Sidebar rail collapse at `xl`.** Yes, remembered in `localStorage`. This is a UI preference, never a secret.
10. **GSD UI-checker limits.** `gsd-ui-checker` BLOCKs a UI-SPEC that declares more than 4 font sizes or more than 2 weights (Dimension 4), or spacing outside 4/8/16/24/32/48/64 px (Dimension 5). A BLOCK clears only by revising the spec, or by "Force approve" after two revision rounds. `DESIGN-DIRECTION.md` §5 uses 7 sizes (12, 13, 14, 16, 24, 28, 30) and 3 weights. *Default:* fit the contract.
    - Sizes: 12 px (caption, th, pill), 14 px (body, label, button, nav, table, help, code), 16 px (card title, inputs below `sm`), 24 px (page title and stat value).
    - Weights: 400 and 600. DESIGN-DIRECTION's 500 becomes 600 on buttons, pills, th and the active nav item, and 400 elsewhere.
    - Spacing: 4/8/16/24/32/48/64 px for gaps and section spacing. The component-internal paddings (2, 10, 12, 14 and 20 px on pills, table cells and the card header and body) are listed under the UI-SPEC's "Exceptions".

    *Alternative:* keep §5 as written and choose "Force approve" at `/gsd-ui-phase 6`, which turns the Dimension 4/5 BLOCKs into accepted FLAGs.
11. **`Clear-Site-Data` on sign-out.** Send `Clear-Site-Data: "cache"` on the sign-out response, so cached pages, the 60 s private chart PNG and back/forward entries do not outlive the session. *Default:* add it; it is one header on one response. This adds a header to R6, so it needs the maintainer's OK. *Alternative:* leave R6 unchanged.

## 13. Suggested plan shape (for the planner; a suggestion, not a mandate)

**GSD planner shape.** `/gsd-plan-phase` is tracer-first by default. Every PLAN.md leads with one `type="tracer"` task that runs end to end, and the planner restructures "lay the foundation" tasks. Keep the default and make the first plan the tracer: S1 sign-in and the E1–E3 error pages rendered end to end on the new stack. That plan includes the `css` Docker stage, vendored Alpine/Inter/Lucide with the manifest hash test, the new CSP and its rewritten policy tests, the theme cookie, the app, auth and error layouts, `tests/web/pages.py` and the class-assertion guard. The groups below are then ordering guidance for the expansion plans. To get the literal horizontal waves instead, run `/gsd-plan-phase 6 --no-tracer`. "Wave 0" below means "first". GSD numbers plan waves from 1, and its own Nyquist "Wave 0" means test scaffolds that must exist before implementation tasks.

- **Wave 0, foundation and test infrastructure:**
  - the `css` Docker stage plus the dev watcher compose override;
  - vendored assets with manifest, licences and hash test;
  - CSP update and policy-test rewrite;
  - `@theme` tokens and dark-mode variant plus the theme cookie, context processor and POST fallback;
  - the `{% icon %}` tag;
  - form renderer templates;
  - the hook contract, `tests/web/pages.py` and the class-assertion guard.
- **Wave 1, shell and components:**
  - app layout, auth layout (sign-in) and error layout (E1–E3, reads no context variable);
  - sidebar with the location-list context processor;
  - top bar, account menu, toasts and live region;
  - component partials: button, pill, card, stat tile, table, alert/banner, secret field, code block with copy, switch, modal shell, empty state, spinner;
  - `admin.js` components: sidebar, theme, copy, toasts, submit guard, tabs, modal loader, poll, relative times (UI-11), chart image fallback (UI-06). Polish components (OFF-after hint N8, error-summary focus N10, throttle countdown N11, filter chips N13, chart lightbox) are optional and named in the plan if built.
- **Wave 2, pages in parallel.** Each plan migrates its own tests.
  - (a) sign-in, throttled state and error pages;
  - (b) Locations list and fleet tiles;
  - (c) location page with toggles, outages, settings, danger zone and the chart preview endpoint and card;
  - (d) add and edit forms;
  - (e) device setup with reveal, regenerate, tabs and copy;
  - (f) confirmation fragments with the modal and the fallback pages.
- **Wave 3, live data and polish:**
  - the status JSON endpoint and polling on the list, location and setup pages;
  - relative times;
  - accessibility and responsive passes;
  - removal of the old assets and tests;
  - README updates (dev watcher, asset pinning);
  - UI review with screenshots.

## 14. Risks

| Risk | Mitigation |
|---|---|
| Old guard tests silently re-impose the old rules, or agents re-read STACK.md and CLAUDE.md | Apply the §7 amendments first; rewrite the policy tests in Wave 0 |
| Tailwind never generates dynamically built class names (`status--{{ x }}`) | Style by `data-status` attributes or spell out full class strings; no class names built in Python |
| The Alpine CSP build rejects TailAdmin-style inline expressions | All logic lives in `Alpine.data()` in `admin.js`; port behaviour, not attributes |
| CSS built after `collectstatic` gives "Missing staticfiles manifest entry" | Build order in the Dockerfile `css` stage; a test checks every asset is in the manifest |
| The modal fragment `fetch` follows redirects and swallows flashes | Use `redirect: "manual"`; an opaque redirect leads to `location.assign(url)` (`DESIGN-DIRECTION.md` §3); a refused fragment request queues no flash, the following full GET queues it once |
| A secret leaks through new surfaces (JSON, fragments, PNG, history snapshots, storage) | The R4 rules in §6.3; extend the INV-23 scans; no htmx; key reveal never via fetch |
| The chart preview loads the 1 vCPU VPS | Short cache; render only on demand; login-required |
| Disk at 96% and Docker hangs under parallel executors | Free space first; if needed, set `parallelization` to false for Phase 6 (README "Before you start" item 2) |
| Dark-mode contrast regressions | Use the AA-checked tokens in `DESIGN-DIRECTION.md` §5; never use gray-500 on dark backgrounds |

## 15. UAT interplay with phases 1–5

- At `/gsd-verify-work`, mark these superseded by Phase 6: 04-UAT #5 and 05-UAT #5 (visual passes and the old UI-REVIEW fixes).
- Re-express 01-UAT #7 (a 100-character name at 360 px) as Phase 6 acceptance item UI-12.
- The backend UAT (the DoD 1–4 drills, the restore drill, the DST change on 2026-10-25) does **not** depend on Phase 6 and should run now.
