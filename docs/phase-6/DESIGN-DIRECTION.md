# Phase 6 design direction: rebuilding the Power Monitor admin from scratch

**Status:** approved direction, 2026-10-04. Scope and the maintainer decisions D6-01…D6-07 are in `PHASE-6-BRIEF.md`.
**Feeds:** `/gsd-sketch` (decision D6-06) and `/gsd-ui-phase 6`, which turns it into `06-UI-SPEC.md`.
**Precedence:** maintainer decisions, `PHASE-6-BRIEF.md`, and `06-CONTEXT.md` / `06-UI-SPEC.md` once written override this document (brief §0). This document overrides `01-UI-SPEC.md`, `04-UI-SPEC.md` and `05-UI-SPEC.md` for everything visual, interaction-related and asset-related. Their security-bound rules (brief §6.3, R1–R16) still apply.

**What the chosen sketch variant may change, and what it may not**
- **May change:** visual details. That covers tokens, colours, radii, shadows, density, spacing and the type scale. Two limits apply:
  - Every changed token must still pass the AA checks in §5.
  - The status hues stay matched to the Telegram chart (principle 3).
- **May not change:**
  - the structure: the shell, the page layouts, the card set and the DOM order;
  - the states listed for each screen;
  - the behaviour and security contract (brief §6, R1–R16);
  - the accessibility rules (UI-12).

**Tags used below**
| Tag | Meaning |
|---|---|
| `parity` | An existing screen, state or behaviour (S1–S13, E1–E3 in `ADMIN-INVENTORY.md`), rebuilt from scratch |
| `UI-xx` | An approved Phase 6 requirement (brief §4) |
| `polish` | Small polish. It is in scope under UI-01, UI-09 or UI-12 unless the planner finds it costly; in that case it may be dropped and recorded in the plan |
| `optional` | Build it only if it is cheap (brief §12 Q8) |

**Scope amendments (brief §7, applied before planning):** two rows of the Out of Scope table in `.planning/REQUIREMENTS.md` change:
- "admin UI localisation **or themes**" loses "or themes" (UI-02). Localisation stays out.
- "Web charts or analytics in the admin panel" now excludes the weekly chart preview (UI-06) and the fleet summary (UI-04).

All paths are relative to the repo root.

---

## 1. Design principles

1. **The status is readable in two seconds.** The admin's main question is "is everything OK, and if not, where?"
   - Every page that shows a location leads with its status pill.
   - The list opens with a fleet summary (N1, UI-04).
   - Trouble (power off, delivery failing, maintenance) goes to the top of the page. It never sits in a table cell the admin has to look for.
2. **Calm by default, loud only when something is wrong.**
   - Surfaces stay neutral. Colour marks state and danger only.
   - Red means two things only: power OFF and destructive actions.
   - Delivery failing gets its own orange "warning" tone. Today it shares red with OFF (`powermon/web/static/web/app.css:198,202`).
   - Maintenance and waiting look different. Today both use the same grey dot (`app.css:199,201`).
3. **One visual language with the Telegram chart.**
   - The status hues are exactly the chart's: on `#62C28A`, off `#CC3434`, not-monitored `#A09D94` hatched on `#E2E0DA` (`docs/chart-spec.md:110-113`).
   - The font is the chart's Inter (`docs/chart-spec.md:77`).
   - The weekly chart itself appears on the location page (N5, UI-06).
   - Subscribers and the admin should feel they are using one product.
4. **JavaScript is welcome, but every action is still a real form.**
   - JS adds copy buttons, modals, toasts, switches with pending states and live status.
   - Every state change is still a CSRF POST with redirect-after-POST, and every confirmation still exists as a server GET.
   - This is the security contract (R2, R4, R7), not stylistic minimalism. It also means a failed JS load never blocks the admin.
5. **Secrets look different from everything else.**
   - The device key and the bot token get their own components: masked by default, lock and key icons, monospace, an explicit Reveal, and Copy only once revealed.
   - The token is write-only and never has a "show" eye (R3).
6. **Sized for one admin and at most 20 locations.**
   - No pagination, search, bulk actions, roles or settings maze.
   - Use the space for clarity: an explanation next to every control, and the fix next to every problem (for example, "Send test message" inside the delivery-failing alert).
7. **Accessible by construction (UI-12).**
   - Contrast meets AA in both themes. Every colour also has a text label and an icon.
   - Visible `focus-visible` rings, 44 px touch targets, announced toasts, tables with accessible names, and silent breadcrumb separators.
   - These fix the open 04/05-UI-REVIEW findings by design rather than by patching.

---

## 2. Layout shell (UI-01)

```
┌──────────────┬────────────────────────────────────────────────────────────┐
│ ⚡ Power      │ [≡]  Locations / Kyiv office            [☀/☾/🖥]  [admin ▾]│ ← sticky topbar
│   Monitor    ├────────────────────────────────────────────────────────────┤
│              │ ⚠ Ops chat is not configured. Set OPS_BOT_TOKEN …    (list)│ ← banner slot
│ ▣ Locations  │                                                            │
│ ＋ Add       │  Kyiv office  (● On) [Alerts off]           [Send test ▸]  │ ← page header
│              │  On since 2026-10-04 07:12:03 EEST · heartbeat 12 s ago    │
│ LOCATIONS    │                                                            │
│ ● Kyiv office│  ┌ card ───────────┐ ┌ card ───────────┐                   │
│ ● Lviv shop  │  │                 │ │                 │                   │
│ ◍ Dacha      │  └─────────────────┘ └─────────────────┘                   │
│ ○ Garage     │                                                            │
│ ───────────  │                                              ┌ toast ────┐ │
│ ⚠ Ops chat   │                                              │✓ Changes… │ │
│   off        │                                              └───────────┘ │
└──────────────┴────────────────────────────────────────────────────────────┘
```

### Sidebar

Only "Locations" is a top-level destination today (`powermon/web/templates/base.html:18-21`). The sidebar adds the location list.

- **Brand block:** a zap mark plus the wordmark "Power Monitor". It links to `/`.
- **Main:**
  - "Locations" (layout-grid icon), with `aria-current="page"` on the list.
  - "Add location" (plus icon), a link to S4.
- **Locations group (N2, UI-03):** every non-deleted location, sorted as on the list, each with a status dot and its name.
  - Long names are truncated with an ellipsis, using CSS only. The full name stays in the DOM, so the link's accessible name is complete; `title` repeats it for mouse users.
  - The current location is highlighted with `aria-current="page"`. One click opens any location.
  - The data comes from a context processor that reuses `status.location_status` (`powermon/web/status.py:44`). It returns nothing for an anonymous request and is lazy (a `SimpleLazyObject` evaluated only by the app layout), because Django runs every context processor on the sign-in page, fragments, 404 and 403-CSRF. It is one query that selects only the shown columns (`.only("pk", "name", "maintenance", "state__status", …)`), so the bot token and device key columns are never loaded. The items are a frozen dataclass with no secrets (R4).
  - On the three polling pages (UI-05), the dots update from the same status JSON.
- **Footer:**
  - An "Ops chat off" warning chip, shown only while `ops_configured` is false (N7, `polish`). Today the warning appears only on the list (`powermon/web/views.py:217`). The chip links to the list, where the full banner explains.
- **Behaviour:**
  - Fixed and 280 px wide at `lg` and above.
  - At `xl` and above it can be collapsed to a 72 px icon rail. The choice is remembered in `localStorage`; it is a UI preference, never a secret (brief §12 Q9).
  - Below `lg` it becomes an off-canvas drawer with an overlay. The main area gets `inert`, Esc closes the drawer, and the hamburger has `aria-expanded`/`aria-controls`.

### Topbar

- Sticky and 64 px high, with a hamburger below `lg`.
- **Breadcrumbs** sit in the topbar on desktop and drop below the title on mobile. They are an `<ol>`; the separators are CSS chevrons with `aria-hidden`, so screen readers no longer read "/" aloud.
- **Theme switch (UI-02):** a three-state segmented control (Light / Dark / System) inside a popover. See "Theme" below and §5.
- **Admin menu:** an avatar circle with the initial and the username. It opens a popover containing "Signed in as admin" and the **Sign out** POST form (CSRF).
- **No search and no notifications** (brief §3, out of scope).

### Theme (UI-02, D6-03)

- **Cookie:** `theme=light|dark|system`. It is allowlisted, and any other value counts as `system`. **System is the default.**
- **Server-rendered:** a context processor puts `data-theme` on `<html>`; `color-scheme` comes from the stylesheet (`[data-theme=light]{color-scheme:light}`, `[data-theme=dark]{color-scheme:dark}`, `[data-theme=system]{color-scheme:light dark}`), never from a `style` attribute. `system` resolves through `prefers-color-scheme` in CSS, so no script runs before paint and the page never flashes the wrong theme.
- **With JS:** the switch sets the cookie and flips `data-theme` in place. There is no reload.
- **Without JS:** the popover opens through the native `popovertarget` attribute. Its three options are submit buttons of a `<form method="post" action="{% url 'theme' %}">` (CSRF; the plan sets the path, for example `/theme/`, `TEST-STRATEGY.md` §8.4). The view sets the cookie and redirects to the fixed `/`, or, if the plan supports it, to the path of a same-host Referer that is a GET page other than the four confirmation routes (a confirmation or POST-result URL maps to its parent S5 or S8 page). There is no free `next` field (R8).
- **Error pages** (E1–E3) ignore the cookie and always use `system` (R11). Django renders 404 and 403-CSRF with the request, so the error layout must not read the theme value even though the context processor runs.

### Banner slot

Full-width banners sit above the page header:
- the ops-chat warning on the list (parity);
- "Maintenance is on…" on a location page in maintenance (derived from existing copy, see S5);
- the delivery-failing alert on the location page (S5).

### Page header pattern

`[breadcrumbs] → h1 (+ status pill and tags on location pages) → meta line (muted, tabular) → actions on the right`.

On mobile the actions wrap under the title. The primary action is the only filled brand button in the header.

### Flash messages become toasts (UI-09)

Rendered messages (`base.html:32-38`) are mapped as follows:

| Django level | Tone | Icon | Role | Dismissal |
|---|---|---|---|---|
| success | success (green) | circle-check | `status` | auto after 10 s, with a visible timer bar; pauses on hover or focus |
| info | brand (blue) | info | `status` | stays until closed |
| warning | warning (orange); **fixes the bug that warnings look like success** | triangle-alert | `status` | stays until closed |
| error | error (red) | circle-alert | `alert` | stays until closed |

- **Placement:** top-right under the topbar on desktop, full-width at the top on mobile. At most 3 are stacked, newest on top.
- **Tone for screen readers:** a visually hidden prefix ("Warning:", "Error:") goes before warning and error text, so the tone is not carried by colour alone.
- **Instructive success flashes** (location deleted, history reset, channel changed) stay until dismissed. Views opt in with `extra_tags="sticky"`, a one-line change per view (brief §12 Q7).
- **No-JS fallback:** the server renders the same toast markup statically, so it stays visible.
- **Reliable screen-reader announcement:** the server renders each message as a visible toast inside the `role="status"` region (or the `role="alert"` region for errors), so it shows without JS (`TEST-STRATEGY.md` §4.3). After load, `admin.js` re-inserts each toast's text (clear it, then set `textContent` on the next frame), so screen readers that ignore content present at load announce it. This fixes the open "flashes not announced" finding.

---

## 3. Page by page

ASCII sketches abbreviate times; real pages keep `display_time` full and compact `YYYY-MM-DD HH:MM` formats (brief §6.1).

### S1 Sign in (and the throttled 429 state)

```
┌───────────────────────────────┬──────────────────────────────┐
│  ⚡ Power Monitor              │                              │
│                               │   brand panel (lg+ only):    │
│  Sign in                      │   brand-950 bg, subtle grid  │
│  ┌─ alert (error|warning) ─┐  │   pattern (CSS/SVG), mark,   │
│  └─────────────────────────┘  │   "Power outage alerts for   │
│  Username [______________]    │    your locations"           │
│  Password [______________]    │                              │
│  [        Sign in        ]    │                              │
└───────────────────────────────┴──────────────────────────────┘
```

- **Layout:** a TailAdmin-style split screen. The brand panel is hidden below `lg`, where the form sits in a centred card. No sidebar.
- **Components:** text field, password field, a full-width primary button, and an alert. The password field needs no show/hide eye; the password never comes back anyway.
- **States:**
  - Normal: the username field has autofocus.
  - Wrong credentials: an error alert with the `powermon/web/forms.py:14` copy. The username is kept.
  - **Throttled (429):** the copy from `powermon/throttle/rules.py:30` in a *warning* alert with a clock icon.
    - The fields stay (the form is unbound, `login.html:9-10`). The server renders the button enabled.
    - N11 throttle countdown (`polish`): JS disables the button and re-enables it when a countdown from `data-retry-after` ends. Without JS (or without N11) the button stays enabled, and a POST simply gets 429 again (R9 is unchanged). The locked "Try again in 5 minutes." text stays.
  - Submitting: a spinner in the button.
  - Signed-out flash: an info toast.
  - The throttle message and the wrong-credentials message stay inline alerts in that response, never Django messages or toasts. A stored message would show again on the next page after the cool-down and break R9.

### S3 Locations (the home page)

```
 Locations                                                 [＋ Add location]
 ┌ On ──────┐┌ Off ─────┐┌ Maintenance ┐┌ Waiting ──┐┌ Delivery failing ┐   ← N1, UI-04
 │ ⚡ 14    ││ ⚡̸ 2      ││ 🔧 1        ││ ⏳ 1      ││ ⚠ 1              │
 └──────────┘└──────────┘└─────────────┘└───────────┘└──────────────────┘
 [All 18] [Problems 3] [Off 2] [Maintenance 1]                     ← N13 chips (optional)
 ┌ card ────────────────────────────────────────────────────────────────┐
 │ Name              Status                Last heartbeat      Delivery │
 │ Kyiv office    ›  ● On                  12 s ago            ✓ OK     │
 │                                         2026-10-04 10:31:07 EEST     │
 │ Dacha          ›  ◍ Maintenance         3 h ago             ⚠ Failing│
 │                   🔕 Alerts off  ⟳ Router grace              since …  │
 └──────────────────────────────────────────────────────────────────────┘
```

- **Fleet summary tiles (N1, UI-04):** On, Off, Maintenance, Waiting, Delivery failing.
  - The counts come from the `rows` the view already builds (`LocationRow`, `powermon/web/views.py:147-162`, built at `:202-216`), so there is no new query.
  - Calm by default: a tile with a count of 0 renders muted. Off and Delivery failing take their tone only when their count is above 0.
  - Grid: `grid-cols-2 sm:grid-cols-3 xl:grid-cols-5`. There is no horizontal scroll (UI-12).
  - The counts update with live refresh (UI-05).
  - If N13 is built, each tile is a button that selects the matching chip. Otherwise the tiles are plain, non-interactive stat tiles.
- **Table:** wrapped in a card, borderless, with row dividers. The `<caption>` "Locations" is visually hidden.
- **Whole row clickable:** the name link is stretched with `after:absolute after:inset-0`, so the row has one link and stays accessible. A chevron sits on the right.
- **Status cell:** the status pill, then tag chips in this order (parity): "Alerts off" with a bell-off icon, "Router grace" with a router icon.
- **Last heartbeat:** the relative time on top (N3, UI-11), the absolute `display_time` below in muted tabular text, or "Never".
- **Delivery:** "✓ OK" in success text, or an orange pill "Failing since 10:42 (http_403)". The string comes from `status.py:60-74`, unchanged.
- **Mobile (below `md`):** stacked cards replace the table, one per location: the name with a chevron, the pill and tags, then the heartbeat and delivery lines.
- **Filter chips (N13, `optional`, brief §12 Q8):** these are purely client-side. They toggle `hidden` on rows by `data-status`, and are hidden without JS. A chip with zero matches shows "No locations match — show all".
- **States:**
  - **Empty:** an empty-state card with an icon in a tinted circle, "No locations yet", the existing text, and the primary "Add location" button. No tiles are shown.
  - Populated.
  - Ops chat not configured: the warning banner (parity copy).
  - **Live refresh (N4, UI-05):** pills, tags, heartbeat times, delivery cells and tile counts update in place every ~30 s while the tab is visible (§6, J10).

### S5 Location page

The page is a **card grid with an in-page anchor sub-nav**, not tabs (brief §9). Everything stays visible at once, and the old section order survives.

```
 Locations › Kyiv office
 Kyiv office  (● Off) [🔕 Alerts off]                        [✎ Edit] [⋯]
 Outage since 2026-10-04 09:58:12 EEST (34 min ago) · last heartbeat 34 min ago
 ┌ alert warning ───────────────────────────────────────────────────────┐ (only when
 │ ⚠ Delivery failing since 2026-10-04 10:42:00 EEST (http_403)         │  failing)
 │   {cause line}  {retry line}                    [➤ Send test message]│
 └──────────────────────────────────────────────────────────────────────┘
 [Overview] [Chart] [Outages] [Settings] [Danger zone]  ← anchor pills (#status …)
 ┌ Status ──────────────────────────────────────┐ ┌ Controls ─────────────────────┐
 │ Status        ● Off                          │ │ Maintenance          [○━━]    │
 │ Power state   (maintenance only)             │ │ OFF is detected as usual…     │
 │ Outage since  2026-10-04 09:58 · 34 min ago  │ │ Alerts               [━━●]    │
 │ Last heartbeat …                             │ │ Router grace         [○━━]    │
 │ Delivery      ✓ OK  (help)                   │ ├ Test message ─────────────────┤
 └──────────────────────────────────────────────┘ │ {paragraph}  [➤ Send test]    │
                                                  └───────────────────────────────┘
 ┌ Weekly chart ─────────── [⤢ Open full size] ┐ ┌ Settings ──────── [✎ Edit] ┐
 │ ┌ PNG 1280×1000, scaled to the card width ┐ │ │ dl rows (masked 🔒 token)   │
 │ │                                          │ │ ├ Device setup ──────────────┤
 │ │                                          │ │ │ URL [copy]  [Open setup →]  │
 │ └──────────────────────────────────────────┘ │ └────────────────────────────┘
 │ In the channel's language (Ukrainian)        │
 └──────────────────────────────────────────────┘
 ┌ Recent outages · 14 days · 3 · 2h 10m ───────┐
 │ Outage                     Off time          │
 │ ● in progress  10-04 09:58 –   34m           │
 │   10-02 21:10 – 23:15        2h 05m      [🗑] │
 │ {off-time note} {in-progress note}           │
 └──────────────────────────────────────────────┘
 ┌ Danger zone (error-200 border) ──────────────────────────────────────┐
 │ Reset history   {description or refusal line}   [Reset history…]     │
 │ Delete location {sentence}                       [Delete location…]  │
 └──────────────────────────────────────────────────────────────────────┘
```

**Header**
- The h1 is the name, set to `break-words` so words never split in the middle. `[overflow-wrap:anywhere]` applies only to long tokens with no spaces. This fixes the `app.css:265` finding (UI-12).
- Then the status pill and tags, and a meta line: "Outage since …" or "On since …", plus the last heartbeat. Each time has its relative time next to it (UI-11).
- Actions: Edit, and a kebab popover with "Device setup", "Reset history…" and "Delete location…". The two "…" items open the confirmation modal (UI-07).

**Banners**
- **Delivery failing:** a warning (orange) alert that carries the cause, migrate and retry lines (`powermon/web/location_views.py:119-148`) and the Send test message button, so the problem and its fix sit together. The Status card still shows the Delivery row (parity).
- **Maintenance:** a muted "not monitored" banner with a hatched left edge and a wrench icon. Its copy is the existing Power-state help line. The Power state row stays in the Status card.

**Controls card: the three switches become toggle switches (UI-10)**
- Each toggle is a `<form method="post">` with the hidden target `value` (idempotent, UI-D3) and a `<button role="switch" aria-checked>`.
- The accessible name is "Maintenance". A visually hidden suffix keeps the action wording "Turn maintenance on", so the state is shown and the action is named.
- Below each toggle: the state line ("Maintenance is off", the parity h3 copy) and the fixed help text.
- JS shows a pending spinner in the knob. Then the normal POST → redirect → GET runs, and the flash shows as a toast. A repeat still answers "Nothing changed."

**Test message**
- The button shows a spinner and "Sending… up to 15 s", because the request can take about 15 s (see the inventory).
- After the click it is marked `aria-disabled="true"` and `aria-busy="true"`, and further submits are blocked by a flag, which ends the accepted double-send (UI-09). It never gets the `disabled` attribute: a disabled submitter's name/value is not posted (`FRONTEND-STACK.md` §6 pitfall 13).

**Chart preview card (N5, UI-06)**

*Placement*
- A card `id="weekly-chart"` with the anchor pill "Chart".
- Title "Weekly chart", subtitle "The chart pinned in the channel".
- Header action: a ghost button "Open full size" with a `maximize-2` icon.
- In the DOM it comes after Controls/Test and before Recent outages. On `xl` it heads the wide column of grid row 2; below `xl` it sits in DOM order.

*Image sizing*
- The PNG is always 1280×1000 (32:25, `powermon/chart/render.py:42-43`, `docs/chart-spec.md` §2).
- It is shown scaled down in the card: `<img width="1280" height="1000" class="h-auto w-full">`. The size attributes reserve the 32:25 box, so the layout never jumps.
- `rounded-xl`, a 1 px border, on a `surface-muted` placeholder with the same aspect ratio.
- Never cropped. Never inverted or CSS-filtered in dark mode: it is the real light artefact, and on a dark card it sits inside its border.
- At about 720 px wide (the 2/3 column at `xl`) the row labels read at about 19 px. On a 360 px phone the image is a thumbnail, which is why full size exists.

*Full size*
- The image is wrapped in `<a href="{chart url}" target="_blank" rel="noopener">`. Its accessible name is "Open the weekly chart at full size (opens in a new tab)". It works without JS; the browser shows the 1280×1000 PNG.
- The header button uses the same link.
- A `<dialog>` lightbox (the image at natural size, scrollable, Esc or ✕ closes it) is an optional `admin.js` enhancement. If it is built, the link stays as the fallback.

*Alt text*
- `alt="Weekly power chart for {name}, the same image as the chart pinned in the Telegram channel"`. The name is auto-escaped (R1).
- A muted line under the image, linked with `aria-describedby`: "Shown in the channel's language ({Ukrainian|English|Russian}). The same outages are listed under Recent outages."

*States*
- **Loading:** the 32:25 placeholder with a centred spinner *behind* the image. This is CSS only: the opaque PNG covers the spinner once it loads. `loading="lazy"` and `decoding="async"` make the render request fire only when the card scrolls into view.
- **No history** (`has_history` is false, the existing context flag at `location_views.py:525`): the page renders no `<img>`, so no request is made.
  - An empty state appears instead: a `calendar-days` icon in a gray circle, the title "No chart yet", and "The weekly chart appears here once the device has sent its first heartbeat."
  - It is a normal empty-state height, not the 32:25 box.
- **Error** (the endpoint fails, for example a render error → 500):
  - `admin.js` listens for the image's `error` event through `addEventListener` (no inline handler) and swaps in an inline warning alert: "The chart could not be drawn right now. Reload the page to try again."
  - Without JS, the browser shows the alt text in the box.
- **Maintenance:** renders normally. The chart draws maintenance as hatched "not monitored".
- **Waiting with history** (for example after a restore): renders normally. The channel may have no pinned chart until the first heartbeat, because the worker posts only for on/off locations (`powermon/chart/lifecycle.py` module docstring).
- **Live refresh:** polling does not re-request the image. When the status poll sees a status change, the page offers its "Reload" chip (§6, J10). A reload gets a fresh render, cached for at most 60 s.

*Endpoint contract* (brief §6.3 new-surface rules, brief §12 Q5)
- **Route:** `GET /locations/<int:pk>/chart.png`, URL name `location-chart`, next to the other `locations/<int:pk>/…` routes.
- **Access:**
  - Login-required through the default-deny middleware (R14). An anonymous request gets the usual login redirect.
  - GET and HEAD only; any other method gets 405. There is no state change and no CSRF (R2).
  - An unknown or soft-deleted pk gets 404, through the same lookup as the location page.
- **Body:** the worker's own code path, live:
  - `source.load_week(pk, today=<today's local date>, now=now, tz=settings.TIME_ZONE, live=True)`, then `render.render_png(week, lang=location.language, name=location.name)`.
  - This is the image half of `chart_content` (`powermon/chart/lifecycle.py:623-639`, called live at `:757`).
  - Import `powermon.chart.render` inside the view, as `chart_content` does, so Pillow loads only on first use. Never import `powermon.chart.lifecycle` at module level either (`tests/chart/test_lifecycle.py:1445` checks both).
  - **Do not build a `ChartLocation`:** it carries the bot token (`lifecycle.py:287`).
- **Headers:**
  - `Content-Type: image/png`.
  - `Cache-Control: private, max-age=60`. The brief allows `never_cache` or a short private cache for the PNG.
  - The CSP and the framework security headers, as on every response (R5, R6).
- **Server cache:**
  - The rendered bytes are cached for about 60 s per location. The cache key includes the pk, language and name, so an edit shows at once.
  - At 20 locations or fewer, a per-process cache is fine.
- **Cost:**
  - A render costs about +25 MB transient on the 1 vCPU, 1–2 GB VPS.
  - Lazy loading plus the cache limit it to one render per location per minute per web process.
  - Brief §12 Q5 alternative: the worker stores its last rendered PNG. Use it only if the memory check fails.
- **No secrets:** the PNG holds only the name, dates, times and totals. Add the route to the INV-23 secret-scan matrix: scan the response bytes and headers for the key and the token.
- **Matches the channel:** the same data, renderer and language as the pinned chart. The on-request render can be up to 15 min newer than the channel's copy, which the worker refreshes every 15 min. So "matches" means the same picture, up to the now line.

**Recent outages**
- A card with `id="recent-outages"`, a header caption, and a table with a `<caption>` (fixes the finding).
- **Rows:**
  - Outage: start – end, or "In progress" with a pulsing red dot. "in progress" never splits across lines.
  - Off time, in tabular figures.
  - A row action: a ghost icon button "🗑" with the existing hidden accessible name ("Remove the outage from {start}"). It opens the confirmation modal (UI-07).
- The in-progress row has no action.
- Header stats "3 outages · 2h 10m" (N6 outage totals, `polish`), computed from `outage_rows`.
- **Empty states:** "No outages in the last 14 days." with a check-circle illustration, and "No power history yet…" with an hourglass.

**Settings card**
- `id="settings"`. A two-column description list. The token is in mono with a lock icon (`{bot_id}:••••••••`). The Edit button sits in the card header.

**Device setup card**
- The heartbeat URL (no secret) with a Copy button (N12 copy URL on the location page, `polish`; UI-08), and "Open device setup →".
- **Never the key** (R4).

**Danger zone**
- `id="danger-zone"`, with `id="reset-history"` on its row (fixes the missing anchors).
- **Each row:** a title, the existing description or refusal line, and an outline-danger button that opens the confirmation modal (UI-07; see Confirmations).
- **Reset** is disabled, showing the refusal text, when power is off or there is no history (parity: exactly one of three states).

**Grid, DOM order and test pins**
- The DOM order is Status → Switches → Test → **Weekly chart** → Outages → Settings → Device setup → Reset → Delete. The old sections keep their test-pinned relative order (brief §9). The new chart card slots in between Test and Outages.
- **Layout at `xl`:** row-major, so no source reordering is needed.
  - Row 1: Status (2/3) | Controls + Test (1/3).
  - Row 2: a wrapper holding Weekly chart + Recent outages (2/3) | a wrapper holding Settings + Device setup (1/3).
  - Row 3: Danger zone, full width.
- Below `xl`: one column in DOM order.

**Live refresh (N4, UI-05)**
- The pill, meta line, Status card rows and delivery pill update in place (§6, J10).
- Sections that depend on the status cannot be patched from JSON: the delivery banner, the outage list and the reset refusal line. When the status key or the delivery state changes, an info chip "Status changed · Reload page" appears in the meta line. It is a plain link to the same URL; the page never reloads itself.
- The JSON's `delivery.text` is the list format (`HH:MM`, date added on an earlier day), so on this page a delivery change only shows the reload chip; it never overwrites the Status card's full-format "Failing since …" text.

### S4 Add location / S6 Edit location

```
 Locations › Add location
 Add location
 ┌ alert error: "The location was not saved. Fix the fields marked below."
 │  • Bot token — {error}   ← jump links (N10, polish)
 ┌ card ──────────────────────────────────────────────────────────────┐
 │ Basics          │ Name [________________]  help                    │
 │ ───────────────────────────────────────────────────────────────────│
 │ Monitoring      │ Heartbeat period [60] s   Grace period [30] s     │
 │ how fast OFF is │ ⓘ Reported OFF after 90 s without a heartbeat     │ ← N8 live (polish)
 │ ───────────────────────────────────────────────────────────────────│
 │ Telegram        │ Bot token [🔑 •••••••••••]  help / masked current │
 │                 │ Channel chat ID [________]  ▸ How to find the ID  │
 │                 │ Language [Ukrainian ▾]                            │
 └────────────────────────────────────────────────────────────────────┘
 ┌ sticky action bar ─────────────────────── [Back to locations] [Create]┐
```

- **Sections:** a left label column with a short description, and the fields on the right. It stacks below `lg`.
- **Field details:**
  - The seconds inputs have an "s" suffix add-on and `inputmode="numeric"`.
  - The long chat-ID help (`forms.py:39-45`) goes into a `<details>` disclosure.
  - Inputs are 16 px on mobile, so iOS does not zoom on focus.
- **N8 OFF-after hint** (`polish`): a live "Reported OFF after {P+G} s without a heartbeat" line under the two period fields. It is pure client-side arithmetic, and the server stays the source of truth. Without JS, the line shows the server-rendered value for the initial or bound values.
- **N10 error summary** (`polish`): a form-level error alert that lists each field error as a jump link to the field. On load, focus moves to the summary (UI-12).
- **Field errors:** a red border with `aria-invalid:` variants, an error icon, and the message linked through `aria-describedby` (parity wiring, `forms.py:115-126`).
- **Token field:** a write-only secret input with a key icon. It is never prefilled.
  - It shows the re-paste note on errors: always on create; on edit only when a token was submitted (`forms.py:236-240`).
  - On Edit, the label is "New bot token", and the help shows the current masked token in mono.
- **Edit only:** the Note callout becomes an info alert inside the Telegram section, above the action bar. The old gap bug cannot happen in this layout. The channel-change sentences may move into the relevant field help (Q14).
- **Action bar:**
  - Create: "Back to locations" + "Create location".
  - Edit: "Discard changes" + "Save changes".
  - Sticky at the bottom on mobile.
  - On submit, the button spins and is marked `aria-disabled="true"` and `aria-busy="true"`, and further submits are blocked by a flag (`FRONTEND-STACK.md` §6 pitfall 13: a disabled submitter's name/value is not posted). This prevents the accepted duplicate create (UI-09).
- **States:** pristine, invalid (form-level alert + field errors), submitting.

### S8 Device setup (masked and revealed)

The page becomes a numbered setup guide, with a right rail on desktop.

```
 Locations › Kyiv office › Device setup
 Kyiv office  (⏳ Waiting for first heartbeat)   · Last heartbeat: Never
 ┌ ① Before you start ─────────────┐ ┌ rail: Location settings ─┐
 │ 🔌 mains, no UPS …  🌐 power+net │ │ dl … [✎ Edit location]   │
 │ ⏱ every 60 s, OFF after 90 s    │ └──────────────────────────┘
 ├ ② Heartbeat URL ────────────────┤
 │ [https://…/hb            ][Copy] │
 ├ ③ Device key ───────────────────┤
 │ masked: [🔒 ••••••••••••a1B2 ] [👁 Reveal key]                   │
 │ revealed: [k3y…full…a1B2 ][Copy] [Hide key]  "hidden again next time"│
 │ If the key has leaked … [↻ Regenerate key…]                       │
 ├ ④ Examples  [curl] [Cron] [GNU wget] [BusyBox]  ← tabs          │
 │ ⓘ Reveal the key above to fill it in   (masked only)            │
 │ ┌ code ──────────────────────────────────────── [Copy] ┐        │
 │ └──────────────────────────────────────────────────────┘ caption │
 │ ⚠ Warning: do not paste a URL with the key into Telegram …       │
 ├ ⑤ Check it works ───────────────┤
 │ 200 ok / 401 …   ⏳ Waiting for the first heartbeat… (live, UI-05)│
 └─────────────────────────────────┘
```

- **Key field, masked:** 12 bullets + the last 4 characters, with the hidden text "Hidden key ending in XXXX" (parity, `location_setup.html:44-46`).
  - Reveal is a POST that returns this page with `no-store` (parity).
  - There is no in-place fetch reveal (brief §6.1, R4).
- **Key field, revealed:** the full key as text in a mono `<code id="device-key">` block with Copy (UI-08), and "Hide key" as a GET link. Never an `<input value>`, `title`, `aria-label` or any other attribute.
- **Back/forward cache.** On a page with `[data-testid="device-key"][data-state="revealed"]`, `admin.js` empties `#device-key` and the four example blocks on `pagehide`. On `pageshow` with `event.persisted` it calls `location.replace(<setup URL from a data-* attribute>)`, which loads the masked GET. `Cache-Control: no-store` stays on every S8 response and the S9 POST; the J4 `pageshow` re-enable never re-submits a form.
- **Copy buttons (UI-08):**
  - While the key is masked, only the URL has a Copy button. The key and the example blocks get theirs only in the revealed response; the masked note "Reveal the key above to fill it into these examples." explains why.
- **Examples:** tabs with arrow-key roving `tabindex`. Without JS, all four blocks render stacked under h3s.
  - The commands stay verbatim strings from `powermon/locations/examples.py`.
  - The page keeps three things (01-UAT #9, brief §6.2): the curl example labelled "recommended", the `?key=` examples labelled, and the link-previewer warning.
- **Step ⑤ live status (N4, UI-05):**
  - It polls the no-secret status JSON and flips to "✓ First heartbeat received — On" as soon as the heartbeat arrives. This is the highest-value use of polling.
  - It **never re-fetches the setup page** (R4), and the page never reloads itself: a revealed page came from a POST.
  - There is no "send test heartbeat" button, link or fetch to `/hb` from the admin (§6 "Not built").
- **States:** masked / revealed / revealed after regenerate (2 flashes as toasts) / waiting / on / off / maintenance.

### Confirmations: S7 Delete, S9 Regenerate, S10 Remove outage, S11 Reset history (UI-07, D6-05)

**Decision:** a native `<dialog>` modal whose body is fetched from the existing confirmation GET. The 4 pages stay as the no-JS fallback and as deep links. A client-side `confirm()` is never acceptable.

```
 ┌ dialog (max-w-lg, rounded-3xl) ─────────────────────────┐
 │ (🗑 in error-50 circle)                              [✕] │
 │ Delete Kyiv office?                                      │
 │ This cannot be undone. Deleting this location:           │
 │  • consequence 1 … 5          (S10: details dl first)    │
 │ ┌ info: pause instead — maintenance on or alerts off ┐   │
 │                          [Keep location] [Delete location]│
 └──────────────────────────────────────────────────────────┘
```

**How it works**
- The trigger is a real `<a href=confirm-url>` that `admin.js` intercepts. The loader handles only `a[data-confirm]` whose resolved URL is same-origin.
- It calls `fetch(url, {redirect: "manual", headers: {"X-PM-Fragment": "1"}})`.
- When the views see that header, they render a `_confirm_<name>.html` partial. The full page includes the same partial, so there is one copy source. The fragment response adds `Vary: X-PM-Fragment`, `never_cache` and the response header `X-PM-Fragment: 1`, and it holds no secrets (brief §6.3).
  - Only the four confirmation GET handlers (S7, S9, S10, S11) honour `X-PM-Fragment`. The check lives in those views (or a mixin applied only to them), never in a base template, context processor or middleware. Every other route, especially the S8 GET, the Reveal POST and the S9 POST, answers a request carrying the header exactly as it answers one without it.
- If the result is an **opaque redirect** (the S10 and S11 GET pre-checks refuse, or the session expired), JS calls `location.assign(url)` with the same confirmation URL, and that full GET re-runs the check. So a refusing view queues its flash only for the full-page variant: with `X-PM-Fragment: 1` it returns the same 302 without calling `messages.*`. The flash is then queued once, by the full GET, and shown once on the right page.
  - A normal `fetch` would follow the redirect, render the detail page in the background and silently eat the flash. A fragment that queued the flash itself would show it twice: the messages cookie or session is still written on the fetch's 302, and the follow-up GET adds the message again.
- The loader injects only when all three hold:
  - `response.status === 200`;
  - the response carries the header `X-PM-Fragment: 1`;
  - `DOMParser` finds exactly one `[data-testid="confirm"]` root in the body.

  It moves that root into the dialog, and never uses `innerHTML`, `insertAdjacentHTML`, `outerHTML`, `Range.createContextualFragment` or `document.write`. Every other outcome calls `location.assign(url)`: `opaqueredirect`, 404 (location deleted meanwhile), 5xx, a network error, a missing header or a missing root.
- The modal's form submits **natively** (no fetch). The S9 POST therefore returns the revealed setup page as a top-level `no-store` navigation, and the HMAC `marker` stays a server value (UI-D7).
- While the fragment loads, the dialog shows a centred spinner.

**Content per confirmation (unchanged)**
- S9 shows exactly one state block: a warning alert followed by the "Open the location page" link (maintenance off, power on), or one of three notes (power off, waiting, maintenance on); `location_regenerate.html:25-34`.
- S10 shows the details list (Start, End, Off time) and the conditional consequence 2.
- S11 shows the alternative ("To remove a single false outage…", with a link to `#recent-outages`).
- "Keep outage" and "Keep history" link back to `…#recent-outages` and `…#reset-history`.

**Buttons (re-decided for all four; supersedes UI5-D11)**
- "Keep …" (secondary) comes first; the destructive button comes last, on the right.
- Initial focus goes to Keep.
- On mobile both buttons are full width, with the destructive one on top.
- The ✕ close button and Keep sit outside the POST `confirm-form`: ✕ is a `type="button"` in the dialog shell, and Keep is a link on the page or a `form[method="dialog"]` button in the modal. Inside `confirm-form` the only submit control is `confirm-submit`.
- After a click, the destructive button shows a spinner and is marked `aria-disabled="true"` and `aria-busy="true"`; further submits are blocked by a flag (UI-09; `FRONTEND-STACK.md` §6 pitfall 13).

**Fallback pages** use a centred "focus card" layout: the same content in a card, with breadcrumbs. They look right when reached directly.

### E1 404, E2 403 CSRF, E3 500

- A standalone `base_bare.html` error layout: no sidebar, no user menu, no toasts, and it reads no context variable (no `request`, `user`, `messages`, `request_path`, `exception`, theme or sidebar value). Django renders 404 and 403-CSRF with the request, so context processors run there; 500 renders with no context (`500.html:11-14`, R11). The page must render the same with or without a request.
- A centred card with a big tabular "404" / "403" / "500" in a brand tint, an icon, the fixed copy, and the "Back to locations" primary button. The copy never echoes the path or the reason (parity).
- The theme is always `system` (hard-coded `data-theme="system"`), through the CSS media query; the cookie is never read on these pages.

### Loading and empty states (cross-cutting)

- **Loading:**
  - Pages render on the server, so they need no skeletons.
  - Buttons show a spinner and get `aria-busy` and `aria-disabled="true"` during submit; a flag blocks repeat submits (never the `disabled` attribute, `FRONTEND-STACK.md` §6 pitfall 13).
  - The modal shows a centred spinner while its fragment loads.
  - The chart card shows its placeholder spinner until the PNG loads.
  - Polling shows "Updated 12 s ago" in the meta line, and an amber "Live updates paused" chip after 3 failures in a row.
- **Empty states:**
  - no locations;
  - no outages in 14 days;
  - no history yet (outages and chart);
  - reset not possible (disabled, with a reason);
  - a filter chip with zero matches (if N13 is built).

### Accessibility checklist (UI-12)

- AA text contrast in both themes. Use the §5 tokens, and never gray-500 on a dark background.
- A visible `focus-visible` ring on every interactive element, including the sidebar and the toggles.
- Keyboard operation:
  - menus and popovers: Esc closes, and focus returns to the trigger;
  - modals: focus trap, initial focus on Keep, Esc closes;
  - tabs: arrow keys;
  - the theme switch and the drawer.
- Touch targets of at least 44 px.
- Tables have a `<caption>` and the toasts are announced.
- At 360 px there is no page-level horizontal scroll, and location names wrap at word boundaries. Code blocks scroll inside themselves.
- `prefers-reduced-motion` is respected: every animation sits under `motion-safe:`.
- The viewport meta allows zoom (TailAdmin's does not).

---

## 4. Component inventory (Tailwind v4 `@theme` tokens, TailAdmin-derived)

Test hooks (`data-testid` and semantic hooks) are defined in `TEST-STRATEGY.md` §4. `data-status` below is a styling hook.

Sizes and weights in this table are TailAdmin's. Unless the maintainer takes the brief §12 Q10 alternative, 06-UI-SPEC maps them to the 4-size, 2-weight contract in §5 "Typography" (for example `font-medium` → `font-semibold` on buttons, pills and th; `text-[13px]` → `text-sm`).

No per-element inline values: there is no `style=` attribute anywhere (CSP `style-src 'self'`). The toast timer bar is a fixed-duration CSS animation paused by `:hover` / `:focus-within`, not a width set via `style=`; popover anchors use static `anchor-name` classes, not `style="anchor-name:…"`.

| Component | Variants and tokens |
|---|---|
| **Button** | `primary`: brand-600 bg, white text (6.5:1); hover brand-700. `secondary`: white/gray-800 bg, gray-300 border, gray-700 text. `ghost`: icon or row actions. `danger`: error-700 `#B42318` (white 6.57:1). `outline-danger`: danger-zone triggers. Sizes: `sm` h-9 only with a 44 px hit area (padding or an `after:` pseudo-element); `md` h-11 (44 px). rounded-lg, font-medium text-sm, gap-2 with an icon at size-4. Pending state: a spinner, `aria-busy`, `aria-disabled="true"` and `aria-disabled:opacity-60` (never the `disabled` attribute on a clicked submitter) |
| **Status pill** | rounded-full px-2.5 py-0.5 text-xs font-medium, with a dot or icon. **Styled by `data-status`** (`data-[status=on]:bg-on-50 …`), so live updates only set an attribute plus `textContent`, and templates never build class names (stack pitfall 1). Keys (`data-status`): on, off, maintenance, waiting (`powermon/web/status.py:18-23`). The delivery pill uses `data-delivery="ok\|failing"` |
| **Tag chip** | gray-100 bg, gray-700 text, rounded-md, icon at size-3.5: "Alerts off" (bell-off), "Router grace" (router) |
| **Card** | white / dark `gray-900`, border gray-200 / gray-800, rounded-2xl, shadow-theme-xs. Header `px-5 py-4 md:px-6` with title plus actions; body `p-5 md:p-6`. Variants: `danger` (error-200 border, error-25 header tint); `focus` (centred, max-w-xl) |
| **Stat tile** | a card with an icon in a tinted 40 px rounded-xl square; label text-sm gray-600; value text-2xl font-semibold tabular-nums; muted when the count is 0 |
| **Chart image** | `img` at `h-auto w-full`, with `width`/`height` attributes 1280/1000, rounded-xl, a 1 px border, on a `surface-muted` 32:25 placeholder with a spinner behind it; wrapped in a full-size link |
| **Description list** | `grid grid-cols-[minmax(9rem,auto)_1fr]` gap-x-6 gap-y-3; dt text-sm gray-600, dd text-sm gray-900; stacks below `sm` |
| **Relative time** | `<time datetime="ISO">{display_time}</time>` keeps the absolute text. The relative text is a sibling `<span data-relative="ISO">12 s ago</span>`, server-rendered and refreshed by JS |
| **Table** | inside a card; th text-xs font-medium gray-500 (dark: gray-400), left-aligned; rows `divide-y` gray-100; cells py-3.5 px-5; times `tabular-nums`; `<caption class="sr-only">`; row hover gray-50 |
| **Text field / select** | h-11 rounded-lg border gray-300, shadow-theme-xs, `focus:border-brand-500 focus:ring-4 ring-brand-500/20`, `aria-invalid:border-error-500 aria-invalid:ring-error-500/15`; help text-sm gray-600; error text-sm error-700 with an icon; "s" suffix add-on |
| **Switch** | a `button role=switch` inside a POST form; track w-11 h-6, off gray-200, on brand-600; white knob with shadow; pending: a spinner in the knob; 44 px hit area through padding |
| **Secret field** | mono text-sm on gray-50, a lock or key icon on the left, actions on the right (Reveal, Copy, Hide); the masked text is `aria-hidden`, plus an sr-only description; the revealed key is element text, never an attribute value. The token variant is input-only (password type) with no actions |
| **Code block** | gray-900 bg (both themes), gray-100 mono text-[13px]/5, rounded-xl, `overflow-x-auto`, `select-all` fallback; a header row with a caption and a Copy button that switches to "✓ Copied" for 2 s, plus an `aria-live` message |
| **Alert / banner** | 4 tones (success, info = brand, warning, error) plus a `muted` hatched tone for maintenance; rounded-xl border, `-50` bg, `-700` text, an icon, an optional title and an action slot |
| **Toast** | white card, shadow-theme-lg, a left icon in the tone, message text-sm, a close ✕, a timer bar (motion-safe), an sr-only tone prefix |
| **Modal** | native `<dialog>`, `backdrop:bg-gray-950/50 backdrop:backdrop-blur-sm`, rounded-3xl p-6, max-w-lg, an icon in a tinted circle; motion-safe scale-in |
| **Popover menu** | `popover` attribute, rounded-xl, shadow-theme-lg, items h-11; works without JS through `popovertarget` |
| **Theme switch** | a segmented control with 3 options (sun, moon, monitor), each a submit button of the theme form (`{% url 'theme' %}`); `aria-pressed` on the current one |
| **Tabs** | underline style (a 2 px brand-600 indicator), `role=tablist`, roving tabindex |
| **Breadcrumbs** | text-sm gray-500, the current page gray-800, CSS chevron separators with `aria-hidden` |
| **Empty state** | a 48 px icon in a gray-100 circle, title text-base semibold, text-sm gray-600, an optional CTA |
| **Live chip** | a small pill in the meta line: "Updated 12 s ago" (muted) / "Live updates paused" (warning) / "Status changed · Reload page" (info, a link) |
| **Spinner** | an SVG `animate-spin` at size-4 |
| **Pagination** | not needed (at most 20 rows; a 14-day outage window) |

---

## 5. Visual language

### Colour tokens

**Brand**
- TailAdmin indigo-blue: `brand-500 #465FFF`, `brand-600 #3641F5` (buttons and links, 6.5:1 on white), `brand-700 #2A31D8`. Check -700 against TailAdmin's `src/css/style.css`.
- Dark-mode links: `#7592FF` (6.18:1 on `#101828`).
- "Electric yellow" is rejected as the brand colour because it collides with warning and maintenance (Q1).

**Neutrals**
- TailAdmin cool grays: `gray-50 #F9FAFB` … `gray-900 #101828`, `gray-950 #0C111D`.
- Body text gray-900. Secondary gray-600 `#475467` (7.69:1). Meta gray-500 `#667085` (4.97:1, only at 14 px or larger). gray-400 is decorative only (2.58:1).

**Status tokens (fills match the chart exactly; text uses darker shades for AA)**

| Key | Dot / fill | Light pill bg / text (ratio) | Dark text on `#101828` | Icon |
|---|---|---|---|---|
| on | `#62C28A` (chart) | `#EAF7EF` / `#166E45` (5.69) | `#62C28A` (8.12) | zap |
| off | `#CC3434` (chart) | `#FCEBEB` / `#A82A2A` (6.01) | `#F97066` (6.37); **not** `#CC3434` (3.47 fails) | zap-off |
| maintenance | hatch `#A09D94` on `#E2E0DA` (the chart's not-monitored; a 45° CSS `repeating-linear-gradient` in the stylesheet) | `#F1F0EC` / `#57534E` (6.69) | `#A09D94` (6.55) | wrench |
| waiting | hollow ring gray-400 | `#F2F4F7` / `#475467` (6.98) | `#98A2B3` (6.89) | hourglass |
| failing (delivery) | warning-500 `#F79009` | `#FFFAEB` / `#B54708` (5.2) | `#FDB022` (9.64) | triangle-alert |

- `failing` is a `data-delivery` tone, not a `data-status` value: the `data-status` vocabulary is on, off, maintenance, waiting.
- On white, the dot `#62C28A` is 2.19:1 and `#A09D94` is 2.71:1. As in the chart, they are never the only cue: every pill also has a text label and an icon.
- Danger buttons use error-700 `#B42318` (white 6.57:1); TailAdmin's error-600 is `#D92D20` (white 4.83:1). OFF fills keep the chart red.
- The ratios above use WCAG relative luminance. The dark-mode `/15` pill backgrounds still need a check in the browser.

**Dark mode (UI-02)**
- Page `gray-950 #0C111D`, cards `gray-900 #101828`, borders `gray-800 #1D2939`.
- Text gray-100, secondary gray-400 (6.89:1). Never gray-500 on dark (3.57:1 fails).
- Pills use `<tone>-500/15` backgrounds with the dark text column above.
- Implement it as semantic tokens (`--color-surface`, `--color-surface-muted`, `--color-fg`, `--color-border`, …). Redefine them under `[data-theme=dark]`, and under `[data-theme=system]` inside `@media (prefers-color-scheme: dark)`. Set `color-scheme` per theme in the stylesheet (§2 Theme). The Tailwind `dark:` variant is a `@custom-variant` covering both cases (details in `FRONTEND-STACK.md`).
- The theme comes from a cookie the server reads, so the page never flashes the wrong theme (§2, Theme).
- The chart PNG is never recoloured in dark mode (§3, Chart preview card).

### Typography

> **GSD fit (brief §12 Q10).** `gsd-ui-checker` allows at most 4 sizes and 2 weights, and spacing on 4/8/16/24/32/48/64 px. Unless the maintainer takes the Q10 alternative, 06-UI-SPEC maps the table below to these sizes: 12 px (caption, th, pill), 14 px (body, label, button, nav, table, help, code), 16 px (card title, inputs below `sm`) and 24 px (page title, stat value). It uses weights 400 and 600: 500 becomes 600 on buttons, pills, th and the active nav item, and 400 elsewhere. Gaps use the 4…64 scale, and the 2, 10, 12, 14 and 20 px component paddings are listed as UI-SPEC exceptions.

- **Inter Variable**, self-hosted woff2: latin + cyrillic + their -ext subsets (brief §8). It is used at weights 400/500/600 (400/600 under the Q10 default), with `tabular-nums` on every time, duration and count.
- Monospace: the system `ui-monospace` stack.

| Use | Size / line | Weight |
|---|---|---|
| Page title h1 | 24/32, 30/38 at `md` | 600 |
| Card title h2 | 16/24 | 600 |
| Stat value | 28/36 | 600 |
| Body, table, help | 14/20 (prose help 14/22) | 400 |
| Label, button, nav | 14/20 | 500 |
| Caption, th, pill | 12/18 | 500 |
| Inputs | 16 px below `sm` (no iOS zoom), 14 px above | 400 |
| Code | 13/20 mono | 400 |

### Spacing, radius, shadow, motion

- **Spacing:** a 4 px grid; page padding `p-4 md:p-6`; card gaps `gap-4 md:gap-6`; content `max-w-(--breakpoint-2xl)`; forms `max-w-4xl`.
- **Radius:** pills full; inputs and buttons 8 px (`rounded-lg`); alerts, code blocks and the chart image 12 px; cards 16 px; modals 24 px.
- **Shadows:** TailAdmin `shadow-theme-xs` (cards, inputs), `-lg` (popovers, toasts), `-xl` (modals). Check the exact values in TailAdmin's `style.css`.
- **Focus ring:** 2 px brand-500 plus a 4 px brand-500/25 halo. This is deliberately darker than TailAdmin's 12%. The sidebar gets it too; TailAdmin's sidebar has no focus style.
- **Motion:** 150–200 ms ease-out, all of it under `motion-safe:`.

### Icons

- **Lucide** at stroke 1.5: size-5 in the nav, size-4 inline.
- An `{% icon %}` tag inlines the SVG with `aria-hidden="true"`.
- About 30 icons: zap, zap-off, wrench, hourglass, triangle-alert, circle-alert, circle-check, info, bell-off, router, send, key-round, lock, eye, copy, check, rotate-ccw, refresh-cw, trash-2, pencil, plus, layout-grid, map-pin, sun, moon, monitor, log-out, menu, panel-left-close, chevron-right, x, clock, calendar-days, maximize-2. Check that each name exists in `lucide-static` 1.52.0.
- **Favicon:** a self-hosted SVG plus an ICO zap mark. Today `/favicon.ico` renders the 404 HTML.

---

## 6. Interaction upgrades enabled by JS

- **What ships:** self-hosted static files only: `@alpinejs/csp` plus one first-party `admin.js` that registers `Alpine.data()` components (brief §8). **No htmx.**
- **Wiring:** through `data-*` attributes. There is no inline script, no `on*=` handler and no `style=` attribute. Dynamic text is set with `textContent`.
- **CSP:** exactly the brief §8 policy: `default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'`.

| # | Upgrade | Tag | Implementation and CSP notes |
|---|---|---|---|
| J1 | Copy to clipboard (URL, revealed key, examples; URL on the location page) | UI-08 (+ N12 `polish`) | `navigator.clipboard.writeText`, which needs a secure context (localhost is fine). An `aria-live` "Copied" message (fixed text, never the value). `select-all` kept as the fallback. Key and example copy buttons are not rendered while the key is masked |
| J2 | Confirmation modals loading the server GET | UI-07 | `<dialog>.showModal()`; `fetch(redirect:"manual")` plus a fragment header; injects only a 200 response carrying `X-PM-Fragment: 1` with exactly one `confirm` root, parsed with `DOMParser`; every other outcome (opaque redirect, 404, 5xx, network error) leads to `location.assign` and the full GET, which queues the refusal flash once; a native form submit inside; the pages stay as the fallback (§3 Confirmations) |
| J3 | Toggle switches with a pending state | UI-10 | The form POST with redirect-after-POST is unchanged. JS only sets `aria-busy` and a spinner. No optimistic fetch toggling |
| J4 | Submit spinners and a submit guard (all POSTs) | UI-09 | Stops the accepted duplicate create and the double test-send. One delegated `submit` listener sets a flag, `aria-disabled="true"` and `aria-busy` (never `disabled`, which would drop the submitter's name/value; `FRONTEND-STACK.md` §6 pitfall 13); it resets on `pageshow` (bfcache) and never re-submits a form |
| J5 | Flash toasts, auto-dismiss, reliable announcement | UI-09, UI-12 | Toasts are server-rendered inside the live regions; after load JS re-inserts each toast's text so it is announced; timers pause on hover or focus; the `sticky` extra tag |
| J6 | Sidebar drawer and rail collapse | UI-01 | The Alpine `sidebar` component; `inert` on main; the rail state in `localStorage` (a per-browser preference, never a secret) |
| J7 | Theme switch | UI-02 | Cookie `theme=light\|dark\|system` (allowlisted), written by JS with the same attributes as the server (`FRONTEND-STACK.md` §5); the server renders `data-theme`; JS flips it in place. A no-JS POST fallback to `{% url 'theme' %}` that redirects to `/` or, if supported, to a same-host Referer GET page that is not a confirmation route (R8: no `next` field) |
| J8 | Example tabs, anchor sub-nav, `<details>` help | UI-01, UI-12 | Roving tabindex; without JS everything renders stacked |
| J9 | Relative times ("12 s ago", "3 h ago") | UI-11 | See "Relative times" below |
| J10 | Live status: list, location page, setup step ⑤ | UI-05 | See "Live status" below |
| J11 | Live "Reported OFF after {P+G} s" hint in forms | N8 `polish` | Pure client-side arithmetic; the server stays the source of truth |
| J12 | Error summary with jump links | N10 `polish` (UI-12) | A server-rendered list of field errors; focus moves to the summary on load |
| J13 | Throttle countdown | N11 `polish` (UI-09) | Reads a `data-retry-after` value set by the view and re-enables the button |
| J14 | Fleet filter chips | N13 `optional` (brief §12 Q8) | Toggles `hidden` on rows by `data-status`; hidden without JS |
| J15 | Chart image error fallback, optional full-size dialog | UI-06 | An `error` listener swaps in the warning alert; the optional `<dialog>` lightbox keeps the plain link as the fallback |

**Relative times (J9, UI-11)**
- They apply to heartbeats, on-since and outage-since times, and delivery incidents.
- The server renders `<time datetime="ISO">{display_time}</time>` with the unchanged absolute text, and next to it a sibling `<span data-relative="ISO">` with the initial relative text. The absolute text is never replaced (brief §6.1).
- JS refreshes the relative text every 15 s.
- Format, in English only:
  - under 60 s: "N s ago";
  - under 60 min: "N min ago";
  - under 48 h: "N h ago";
  - otherwise "N d ago";
  - a future time (clock skew): "just now".

**Live status (J10, UI-05; transport per brief §12 Q6)**
- **Endpoint:** `GET /locations/status.json`.
  - Login-required by default deny (R14), GET-only, `never_cache`.
  - **No key and no token, not even masked** (R3, R4). Add it to the INV-23 secret-scan matrix.
- **Shape:**
  ```
  {"generated_at": ISO,
   "ops_configured": bool,
   "counts": {"on", "off", "maintenance", "waiting", "failing"},
   "locations": {"<pk>": {
      "status": key, "label": text, "power": key,
      "last_heartbeat": {"iso", "display"} | null,
      "since": {"kind": "on" | "outage", "iso", "display"} | null,
      "delivery": {"state": "ok" | "failing", "text": "Failing since 10:42 (http_403)"}}}}
  ```
- **Strings:**
  - Every `display` string is server-formatted through `display_time`, so JS never formats time zones. `last_heartbeat` is `null` when there was no heartbeat, and the page renders "Never".
  - `counts` and `delivery.text` come from the same helpers the pages use: `counts` equal the fleet tiles, and `delivery.text` equals `views.delivery_text()` (`powermon/web/views.py:165`) or "OK".
  - This shape is the one `TEST-STRATEGY.md` §8.1 pins; the plan may adjust it, and the test then pins the adjusted shape.
- **Polling:**
  - An Alpine `poll` component runs every 30 s while the tab is visible (`visibilitychange`), so each change appears at most 35 s after the server records it (UI-05).
  - After errors it backs off, and after 3 failures in a row it shows "Live updates paused".
  - Unknown pks are ignored.
- **What it updates:** `data-status` plus `textContent` only, in:
  - the list: pills, heartbeat cells, delivery cells, tiles;
  - the sidebar dots;
  - the location page: the pill, meta line, Status card rows and delivery pill, with a "Status changed · Reload page" chip for the sections that depend on the status;
  - setup step ⑤.
- **It never** re-fetches or reloads the setup page, and never runs on revealed setup content beyond step ⑤'s status text.

**Not built:** a command palette or search, an in-place key reveal, optimistic switch toggling, htmx, and charts drawn with JS libraries. ApexCharts' licence changed, and it injects `<style>`, which the CSP blocks. Also not built: a "send test heartbeat" link, button or fetch to `/hb` from the admin. It would put the key in a URL (history, Referer, logs) or in JS memory, and record a heartbeat from the admin's browser.

**Scope status roll-up**

| Item | Effort | Value | Status |
|---|---|---|---|
| N1 fleet tiles | XS | high | **UI-04, approved** |
| N2 sidebar location list | S | high | **UI-03, approved** |
| N3 relative times | XS | medium | **UI-11, approved** |
| N4 live status (list, location page, setup step ⑤) | S–M | high (setup) / medium (list) | **UI-05, approved** |
| N5 weekly chart preview | M | high | **UI-06, approved** (now in scope; the REQUIREMENTS out-of-scope row is amended per brief §7) |
| Theme switch / dark mode | S | high | **UI-02, approved** |
| Confirmation modals | S | high | **UI-07, approved** |
| Copy buttons | XS | medium | **UI-08, approved** |
| Toasts and pending states | S | high | **UI-09, approved** |
| Toggle switches | XS | medium | **UI-10, approved** |
| Accessibility | — | high | **UI-12, approved** |
| N6 outage totals | XS | medium | `polish`: in scope under UI-01/UI-09/UI-12 unless the planner finds it costly |
| N7 ops-chat chip on every page | XS | medium | `polish`: same rule |
| N8 OFF-after hint | XS | medium | `polish`: same rule |
| N10 error summary | XS | medium (a11y) | `polish`: same rule |
| N11 throttle countdown | XS | low | `polish`: same rule |
| N12 copy URL on the location page | XS | low–medium | `polish`: same rule |
| N13 filter chips | XS | low at ≤20 rows | `optional` (brief §12 Q8) |

---

## 7. Open questions (answered ones marked; the rest keep their defaults)

Questions marked **Answered** are settled by the maintainer (brief §5) or by the brief. The others carry a recommended default and are confirmed in `/gsd-discuss-phase 6` (brief §12).

1. **Brand and accent colour.** *Decided by the `/gsd-sketch` winner (D6-06, brief §12 Q1); starting point:* TailAdmin indigo-blue `#465FFF`/`#3641F5`. It is neutral, the familiar "SaaS admin" look, and it never collides with the on/off/warning status hues. The `/gsd-sketch` variants may propose alternatives, and the maintainer picks one. Rejected alternatives: a teal-green brand (collides with ON) and electric yellow (collides with warning and maintenance).
2. **Font.** **Answered (brief §8, KD8):** Inter Variable. It has Cyrillic and matches the chart. Outfit (TailAdmin's font) is ruled out because it has no Cyrillic.
3. **Dark mode.** **Answered (D6-03, UI-02):** yes. A Light/Dark/System switch, default System, rendered on the server from a cookie with no flash.
4. **Neutrals: cool TailAdmin grays or the chart's warm stone (`#FCFCFB`/`#F4F3EF`)?** *Decided by the `/gsd-sketch` winner (D6-06, brief §12 Q2); starting point:* cool TailAdmin grays. That is the requested look; the status hues and Inter carry the link to the chart.
5. **Sidebar or top nav.** **Answered (D6-04, UI-03):** a sidebar with the location list and status dots.
6. **Location page: card grid or tabs.** **Answered (brief §9):** a card grid plus an anchor sub-nav. Everything is visible at once, the test-pinned order survives, and the missing anchors are fixed.
7. **Confirmations: modal or page.** **Answered (D6-05, UI-07):** a `<dialog>` modal loaded from the server GET, with the 4 pages kept as the no-JS and deep-link fallback.
8. **Auto-refresh.** **Answered (D6-04, UI-05):** yes, on the Locations page, the location page and setup step ⑤, about every 30 s, only while the tab is visible. *Transport default (brief §12 Q6):* the Alpine `poll` component on `GET /locations/status.json`.
9. **Fleet overview.** **Answered (D6-04, UI-04):** fleet tiles on the Locations page, and no separate dashboard page. A separate dashboard would duplicate the list at 20 locations or fewer.
10. **Weekly chart preview.** **Answered (D6-04, UI-06):** yes (§3, Chart preview card). *Caching default (brief §12 Q5):* render on request with a ~60 s per-location cache, login-required, `private`. The alternative is to store the worker's last PNG.
11. **TailAdmin as the code base or as inspiration only.** **Answered (D6-02):** inspiration plus copied `@theme` tokens; all markup is rewritten. Its inline Alpine fails under the CSP build, its sidebar has no focus styles, its viewport line blocks zoom, its badge contrast fails, and it would bring in ApexCharts and Google Fonts. Ship the MIT notice in `LICENSES/`. Never copy from the paid Pro demo.
12. **Toast lifetime.** *Default (brief §12 Q7):* success toasts auto-dismiss after 10 s; everything else stays until closed; views mark instructive successes `sticky`.
13. **Button order in confirmations.** **Answered (brief §9):** "Keep" first, the destructive button last, initial focus on Keep. This supersedes UI5-D11.
14. **Pending copy amendments from 04/05-UI-REVIEW.** **Answered (brief §6.2):** apply them in Phase 6, since every copy-pinning test is being rewritten anyway. `ADMIN-INVENTORY.md` §3 holds the same list.
    - **Test message, HTTP 5xx (`transient`)** (`powermon/web/location_views.py:108-111`, `:364-365`): "Telegram had a server error ({code}), so the test message was not sent. Try again in a minute." Keep the current text for `not_sent`.
    - **Off-time note** (`location_detail.html:111`): "Off time counts only time recorded as power off, as the chart's daily totals do. Time that was not monitored is left out, so off time and the end shown can differ from the alerts."
    - **Remove-outage consequence 3** (`outage_remove.html:40`): "sends nothing itself, and drops its queued OFF and ON alerts if the OFF alert was never sent (if it already went out, its queued ON alert is still sent, so the channel is not left at power off);"
    - **"Removal deferred" flash** (`powermon/web/history_views.py:56-59`): "Not removed: an alert about this outage is being sent to the channel right now. Try again in a minute."
    - **Optional:** in the outage-removed flash, change "No message was sent." to "The removal sent no message."
    - **Optional (A6): edit-form Note callout** (`location_edit.html:44`): move the two channel-change sentences into the Channel chat ID and New bot token help. Keep the meaning.
    - **Locked, recorded only:** the throttle text "Try again in 5 minutes." (D-16) stays. The N11 countdown complements it.

Other defaults that belong to the brief: the test HTML parser (§12 Q3), Playwright (§12 Q4), filter chips (§12 Q8, here N13), rail collapse at `xl` (§12 Q9, here J6), the GSD type and spacing contract (§12 Q10, here §5 "Typography") and `Clear-Site-Data` on sign-out (§12 Q11).
