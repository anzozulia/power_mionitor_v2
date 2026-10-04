# Phase 6 test strategy

**Date:** 2026-10-04 · **Expands:** `PHASE-6-BRIEF.md` §10 (Testing approach) · **Status:** defaults below follow the brief §12; confirm them at `/gsd-discuss-phase 6`
**Readers:** `/gsd-ui-phase 6` (must adopt the hook contract in §4), `/gsd-plan-phase 6` (Wave 0 and per-plan test work), Nyquist validation (§12), the Phase 6 verifier and `/gsd-secure-phase 6` (§8, §9)
**Precedence:** maintainer decisions and `PHASE-6-BRIEF.md` win over this document. Screen IDs (S1–S13, E1–E3), actions, flashes and security rules R1–R16 are the ones in `ADMIN-INVENTORY.md`. File paths for assets are the ones in `FRONTEND-STACK.md`. All paths below are relative to the repo root.

**Defaults for the brief's open questions that concern tests:**
- **Q3, HTML parsing in tests:** `beautifulsoup4` + `soupsieve` as **dev-only** dependencies, added through the INV-26 supply-chain checkpoint. The alternative is a small helper on the stdlib `html.parser`. Details in §5.1.
- **Q4, Playwright:** **not in Phase 6.** Browser-level behaviour is covered by `/gsd-ui-review 6` with real screenshots captured by hand (§11), a manual console and behaviour checklist at UAT (§11), and static lint tests over `admin.js` and the templates (§5.5). Revisit if JS regressions appear.

---

## 0. Summary

- **Scale.** The admin web layer is covered by 22 test files: `tests/web/*.py` (21 files, 334 test functions) plus the CSP tests in `tests/test_walking_skeleton.py` (17 functions). That is 351 functions, about 528 cases after parametrization. The whole suite has 1,281 test functions; `.planning/STATE.md` records "2108 passed" at the end of Phase 5.
- **Most of it survives.**
  - About 196 functions (56%) need no change: the device endpoint `/hb`, the web start-up tests, validators, model rules, the `display_time` filter, and the status-code, redirect and secret-scan tests.
  - About 22 more survive if the new toasts keep `role="status"` / `role="alert"`; their regex helpers are swapped for the parser.
  - About 75 are behaviour tests with a few markup assertions; most are fixed by swapping regex helpers.
  - About 58 are about markup or CSS itself and must be rewritten or deleted. 14 of them are `tests/web/test_css.py` (about 41 cases), which is deleted outright.
- **The breakage sits in shared helpers.** About 30 regex helpers, copy-pasted across files, match exact class names and attribute order (`<dl class="panel settings">`, `<ol class="crumbs">`, `<p class="error" id="id_x_error">`, `<pre class="copy" id=…>`, `<form method="post" action="…">`, `<th scope="col">`). `tests/web` has 164 lines with a `class=` literal. Tailwind class strings change all the time, so **no assertion on a class survives Phase 6.**
- **Policy tests encode the old "no JS, one CSS file" rule** and are rewritten on purpose (§3.5): the CSP constant (3 places), 20 `"<script" not in html` assertions in 11 files, "exactly one `<link>`", "no `http://`", the template lint and the stylesheet rules. Inline SVG icons alone (`xmlns="http://www.w3.org/2000/svg"`) would break `tests/web/test_security.py:274` and the lint at `:289`.
- **Five new server surfaces need their own suites** (§8): the status JSON (UI-05), the chart PNG route (UI-06), the confirmation fragments for the modal (UI-07), the theme cookie and its POST fallback (UI-02), and the sidebar context processor (UI-03). All of them join the INV-23 secret-scan matrix (§9).
- **The coverage gate does not cover `powermon/web` today.** Phase 6 adds it (§10).
- **No browser automation in Phase 6.** What the Django test client cannot see (CSP violations at runtime, JS errors, focus, contrast, the drawer, live updates) is checked by the UI review with screenshots and the manual UAT checklist (§11).

---

## 1. Ground rules

1. **Behaviour and security suites are not up for redesign.** The tests in §3.2 stay untouched in meaning. Run the full gate after each plan. A plan that changes a status code, a redirect target, `Cache-Control`, a cookie attribute or a secret-scan result has introduced a regression, not a style change. The one allowed Cache-Control change is a strengthening that §8 asks for: `never_cache` on the S7, S10 and S11 pages and on all four fragments.
2. **Each page plan migrates its own tests in the same plan as its template,** so the suite is green at every commit and every GSD plan stays atomic (§6).
3. **Tests read pages only through the hook contract** (§4) and the shared parser module (§5.1). Never through class names, Tailwind utilities, CSS rules, DOM depth or attribute order.
4. **The Django test client sees the no-JS page, and that view is authoritative.** Every state change must work there (brief §6.1). JS-only behaviour is checked statically (§5.5) and by hand (§11).
5. **Copy:** Python-owned copy is imported from its module (`powermon/web/views.py`, `location_views.py`, `history_views.py`, `forms.py`, `powermon/web/status.py`, `powermon/throttle/rules.py`), not duplicated as literals. Template-owned copy is pinned against the Phase 6 UI-SPEC copy table.
6. **Names carry IDs.** Keep the INV/K/LOC numbers in existing test names when editing them (`.claude/CLAUDE.md` Acceptance tests). New tests carry the Phase 6 requirement ID (`test_UI05_status_json_has_no_secrets`). Extensions of the secret scans keep the `INV23_2` prefix.
7. **No test starts, stops or restarts containers** (`.claude/CLAUDE.md`). Tests run inside the image built by the README gate command, after the CSS build and `collectstatic` (§10).

---

## 2. The web test suite today

Each test function is in exactly one bucket. Section 3 explains the buckets.

- **Keep**: unchanged. Some depend on copy staying the same.
- **Roles**: survives if the `role="status|alert"` + text pattern is kept; needs only the parser helper.
- **Edit**: a behaviour test; swap a helper or change a few lines.
- **Rewrite**: the test's subject is markup or CSS.

"Migrates in" names the plan that owns the file, using the wave and plan letters from brief §13 (W0 foundation, W1 shell, W2a sign-in and errors, W2b list, W2c location page, W2d forms, W2e device setup, W2f confirmations, W3 live data and clean-up). With GSD's default tracer-first planning (brief §13), W0 and W2a are the tracer plan and the other labels map to its expansion plans.

| File | Functions (~cases) | What it covers | Keep | Roles | Edit | Rewrite | Migrates in |
|---|---|---|---|---|---|---|---|
| test_auth.py | 19 (27) | sign-in/out, admin sync, `next` | 14 | 4 | 1 | 0 | W2a |
| test_css.py | 14 (~41) | cascade parser over `app.css` | 0 | 0 | 0 | **14** | deleted with `app.css` (W3) |
| test_delete.py | 12 | delete flow, INV19 | 8 | 0 | 3 | 1 | W2f |
| test_delivery_display.py | 7 (8) | Delivery column, failing-since text | 3 | 0 | 0 | 4 | W2b (list part), W2c (detail part) |
| test_display_time.py | 5 (9) | template filter | 5 | 0 | 0 | 0 | none |
| test_edit.py | 18 (21) | edit form, write-only token, INV02/06 | 9 | 0 | 7 | 2 | W2d |
| test_examples_verbatim.py | 9 (22) | device examples run for real | 9 | 0 | 0 | 0 | none |
| test_form_null_chars.py | 8 (20) | NUL input | 5 | 1 | 2 | 0 | W2d (forms), W2a (sign-in) |
| test_heartbeat.py | 30 (44) | `/hb` API | 30 | 0 | 0 | 0 | none |
| test_history_pages.py | 37 | outages table, remove/reset | 8 | 6 | 18 | 5 | W2c (outages card), W2f (confirmations) |
| test_inv23_pages.py | 3 | whole-page secret scans | 0 | 0 | 3 | 0 | W0 (helper swap); each plan extends it; W3 final matrix |
| test_location_page.py | 14 (20) | detail page panels, crumbs, CSS | 3 | 0 | 2 | 9 | W2c |
| test_locations.py | 45 (108) | model/validators + create form | 33 | 0 | 6 | 6 | W2d |
| test_regenerate.py | 12 (17) | key rotation | 5 | 2 | 4 | 1 | W2f (confirmation), W2e (POST response) |
| test_security.py | 14 (21) | headers, cookies, error pages, lint | 7 | 0 | 6 | 1 | W0 (policy tests), W2a (error pages) |
| test_setup_page.py | 14 (17) | reveal, examples, guidance | 2 | 0 | 8 | 4 | W2e |
| test_switches.py | 16 | maintenance/alerts/router switches | 8 | 2 | 5 | 1 | W2c |
| test_templates.py | 18 | list page, shell, nav, manifest | 3 | 0 | 5 | 10 | W1 (shell, nav), W2b (list) |
| test_test_message.py | 11 (21) | test send, delivery display | 7 | 2 | 2 | 0 | W2c |
| test_throttle.py | 16 | INV21 #2 429 | 10 | 5 | 1 | 0 | W2a |
| test_web_start.py | 12 | gunicorn start | 12 | 0 | 0 | 0 | none |
| test_walking_skeleton.py | 17 (20) | CSP header (2 tests) | 15 | 0 | 2 | 0 | W0 |
| **Total** | **351 (~528)** | | **196** | **22** | **75** | **58** | |

- **Not affected:** `tests/test_ops08.py:202-205` calls `/hb` only. Nothing else under `tests/` outside `web/` touches templates. One test outside `tests/web` constrains the new chart route: `tests/chart/test_lifecycle.py:1445` (`test_web_process_never_imports_pillow`), see §8.2.
- **No HTML parser in use today:** no BeautifulSoup, lxml, `assertTemplateUsed`, `assertContains` or `assertInHTML`. The one direct template call is `render_to_string("500.html")` (`tests/web/test_security.py:269`). Everything else is regex or substring matching on `response.content.decode()`.
- **Files with two owners** (test_delivery_display, test_form_null_chars, test_history_pages, test_regenerate, test_security, test_templates) would collide when Wave 2 plans run in parallel. See §6.1.

---

## 3. What the assertions depend on

### 3.1 Counts

A heuristic pass over 1,715 `assert` statements in the 22 files found:
- about 1,170 behaviour assertions;
- about 110 copy assertions, plus about 50 role-anchored copy assertions;
- about 290 markup or structure assertions;
- about 57 policy or CSS assertions;
- about 24 that already use stable hooks.

The pass misses helper results assigned to a variable before the assert, so the function-level buckets in §2 are the more reliable figure.

### 3.2 Security and behaviour assertions that must survive unchanged

These are the regression net for the rewrite. Phase 6 keeps all of them; only their helpers may change.

- **Secrets never in HTML or in the `Location` header.**
  - `tests/web/test_inv23_pages.py:184-298` scans every Phase 4/5 page and action response for the bot token and the device key.
  - `test_edit.py:460` (write-only token; `"value=" not in _input(...)` at :471, :527), `test_locations.py:609`, `test_location_page.py:384`, `test_delete.py:546-547`, `test_form_null_chars.py:139`, `test_templates.py:291`.
- **Key only in the Reveal and Regenerate POST responses, with `no-store`.** `test_setup_page.py:93`, `:126`, `:154`; `test_regenerate.py:147`, `:190`, `:246`, `:390`.
- **CSRF and POST-only.**
  - `test_switches.py:224` (GET is 405), `:252`; `test_delete.py:518`; `test_regenerate.py:468`; `test_setup_page.py:320`; `test_auth.py:287` (sign-out is POST only).
  - `test_security.py:228`, `:246` (Form expired page, no "CSRF" text).
- **Auth redirects and `next` handling.** `test_templates.py:294`, `test_auth.py:261`, `:275` (off-host `next` ignored), `test_location_page.py:526`, plus one "anonymous redirects" test per page file.
- **Throttling.** `test_throttle.py:66` (5 failures, then 429), `:91`, `:211`, `:239`, `:269`.
- **Headers and cookies.**
  - HSTS and Secure/HttpOnly/SameSite cookies: `test_security.py:118`, `:142`, `:153`.
  - nosniff, Referrer-Policy and DENY: `test_security.py:182-188`.
  - `check --deploy`: `test_security.py:191`, `:203`.
  - `/hb` carries no CSP: `test_heartbeat.py:470`.
- **Escaping (R1).** `test_templates.py:328`, `test_delete.py:533`, `test_edit.py:877`, `test_history_pages.py:886`, `test_location_page.py:403`. The escaped string assertion survives; the containing tag in the assertion (`<h1 class="name">…`) does not and moves to a hook.
- **Error pages never echo the request (R11).** `test_security.py:213` (404), `:256` (500, no context).
- **Web process never imports Pillow.** `tests/chart/test_lifecycle.py:1445`. The chart route must import `powermon.chart.render` lazily inside the view (§8.2).
- **Pure-domain tests:** heartbeat, web start, examples (run verbatim), validators, `sync_admin`, the throttle store, `display_time`.

### 3.3 Copy assertions (exact strings)

- **Python-owned copy (about 60 distinct strings) survives automatically.** Flashes and form errors live in `powermon/web/views.py`, `forms.py`, `history_views.py` and `location_views.py`. Examples: `SIGN_IN_ERROR` (`test_auth.py:23`), `CHANGES_SAVED` / `PERIOD_TOO_SHORT` (`test_edit.py:68-80`), `THROTTLE_MESSAGE` (`test_inv23_pages.py:71`).
- **The pending copy amendments A1–A7** (`ADMIN-INVENTORY.md` §3) change some Python-owned strings on purpose (A1 test-message 5xx flash, A4 removal-deferred flash, optionally A7). Before applying them, switch the tests that duplicate those strings as literals to importing the constant; then the amendment changes one place.
- **Template-owned copy (about 48 distinct strings) breaks if the wording changes.** Brief §6.2 allows restructuring template prose while keeping its meaning. Re-pin these tests against the Phase 6 UI-SPEC copy table; for warnings and consequence lists, assert the key phrases that carry the meaning. Examples of today's pins:
  - `"Add a location to get its heartbeat URL, device key and setup examples."` (`test_templates.py:91`)
  - the 404/500/403 bodies (`test_security.py:220`, `:236-239`, `:263-266`)
  - `HELP_POWER_ON` / `RETRY_LINE` / `DEVICE_SETUP_SENTENCE` (`test_location_page.py:47-74`)
  - `KEY_NOTE` / `REGENERATE_NOTE` (`test_setup_page.py:41-44`)
  - the `<title>X · Power Monitor</title>` pattern (`test_templates.py:88`, `:339`; `test_security.py:218`)
- **Copy is duplicated as literals across files** (for example `SIGN_IN_ERROR` in `test_auth.py:23` and `test_form_null_chars.py:34`). Only `test_inv23_pages.py:46-54` imports message constants from the views.
- **Copy anchored by ARIA role (22 functions).** The helpers `_flashes`, `_alerts`, `_role_text`, `_flash_count` and `_flash_texts` match `role="(status|alert)">TEXT<` (for example `test_delete.py:130`, `test_auth.py:159`). They require `role` to be the last attribute and plain text right after it, so a toast with an icon, a dismiss button or a nested `<p>` breaks them even with the role kept. They are replaced by `pages.messages()` (§5.1), which keeps their meaning: level, role and exact text.

### 3.4 Markup and structure assertions a from-scratch redesign breaks

About 133 functions (75 edit + 58 rewrite). The coupling sits in the helpers, so fixing the helpers fixes most of them.

| Helper (tests using it) | Pattern it matches | Where | Replacement in `tests/web/pages.py` |
|---|---|---|---|
| `_field_error` (14) | `<p class="error" id="id_{f}_error">` | test_locations.py:434, test_edit.py:210, test_form_null_chars.py:67 | `field_error(page, name)` by id `id_<name>_error`, plus `aria-invalid` and `aria-describedby` checks |
| `_main` (17) | slices from `<main` (a stable hook) | test_delete.py:134 | `main(page)` |
| `_crumbs` (9) | `<nav aria-label="Breadcrumb"><ol class="crumbs">` | test_location_page.py:143, test_delete.py:139 | `breadcrumbs(page)` → list of (text, href) |
| `_rows` / `_cells` / `_table_rows` / `_headers` (21) | bare `<tbody>`, `<tr>` (no attributes), `<th scope="col">` | test_templates.py:60-72, test_history_pages.py:203-211 | `table(page, testid)` → headers and rows of cell texts |
| `_block` (8) | `<pre class="copy" id="…"><code>` | test_setup_page.py:71 | `code_block(page, id)` → exact text |
| `_loaded_form` (6) | `<form class="form"`, `<option value=".." selected>` | test_edit.py:129 | `form_values(page, testid)` |
| `_section` / `_reset_section` (9) | slices between exact `<h2>Recent outages</h2>` … `<h2>Settings</h2>` | test_history_pages.py:193-200 | `section(page, id)` by element id (`recent-outages`, `reset-history`, …) |
| `_panel` / `_status_rows` / `_settings_rows` / `_delivery` (11) | `<dl class="panel settings[ name]">`, `<dt>…</dt>\s*<dd>`, `<p class="help">` | test_location_page.py:123-141, :227 | `definitions(page, testid)` → ordered (term, value) pairs |
| `_form` / `_maintenance_form` (4) | `<form method="post" action="{url}">`, exact attribute order | test_switches.py:99, :266 | `post_form(page, action)` |
| `_marker` (8) | `<input type="hidden" name="marker" value=…>` | test_regenerate.py:99 | `hidden_value(page, "marker")` |

Inline markup literals beyond the helpers:
- Status and tag spans: `'<span class="status status--on">On</span>'`, `'<span class="tag">Router grace</span>'` (`test_templates.py:128-130`, `:160-166`) → `status-pill` with `data-status`, `tag` with `data-tag`.
- Name link: `'<a class="name" href="/locations/{pk}/">'` (29 `class="name"` uses) → `location-link`.
- Number cells: `'<td class="num">…'` (32 uses) → cell text through `table()`.
- Button-variant counts that encode the old "one accent button per page" rule: `html.count("btn--primary") == 1` (`test_templates.py:94`, `test_location_page.py:480`, `test_setup_page.py:119`, `:224`, `test_delivery_display.py:214`). Brief §0 voids that rule. Delete these assertions unless the Phase 6 UI-SPEC re-adopts the rule, in which case count `data-variant="primary"`.
- `"btn" not in section` (`test_history_pages.py:501`) really says that Remove is a link, not a button that acts. It becomes: each Remove entry point is an `<a href>` to the S10 GET page (R7).
- Nav and sign-out shell: `<nav class="site-nav" aria-label="Main">`, `<form class="site-header__signout">` (`test_templates.py:307-314`) → `nav[aria-label="Main"]` and `sign-out-form`.
- Visually hidden helper text: `<span class="visually-hidden">` (`test_history_pages.py:490-498`, `test_setup_page.py:105`) → the accessible-name text of the element (`pages.text()` includes visually hidden text and skips `aria-hidden` subtrees).

Tests that read the stylesheet itself (deleted):
- All of `test_css.py`: a selector-specificity and cascade engine for the invalid-input border; `:380` caps the file at 300 lines, bans `@font-face`/`url(` and allows a fixed px whitelist. Its one real property, that an invalid input is marked, becomes a hook check: `aria-invalid="true"` on the input.
- `test_location_page.py:544-554`: the `@media (width < 640px)` `.switch` rule and the `.crumbs` rule. The 360 px behaviour moves to the UI review and UAT (§11).
- `test_delivery_display.py:249-252`: the `.table-wrap { overflow-x: auto; }` literal. Same.

### 3.5 Policy assertions that encode the old strict front end

Rewrite these on purpose in Wave 0; do not just delete them. Each row says what the test becomes.

| Today | Where | Becomes |
|---|---|---|
| CSP string written out in full | `powermon/web/middleware.py:13`; `tests/web/test_security.py:32-35` (used at :185); `tests/test_walking_skeleton.py:30-33` (used at :94, :102) | The brief §8 policy, compared as a full string in both test files (a changed constant must fail a test), plus negative assertions (§7.3) |
| `"<script" not in html`: 20 assertions in 11 files (list in `ADMIN-INVENTORY.md` §4) | e.g. `test_templates.py:233`, `:341`; `test_history_pages.py:420`, `:629`, `:901`, `:1039`, `:1091`, `:1284` | `pages.assert_no_injected_script(html)`: the payload `<script>alert(1)</script>` is absent, no `<script>` element has a body, every `<script>` has a `src` that is a hashed same-origin static path |
| `_assert_no_script` | `test_inv23_pages.py:137` | the same helper, keeping its ban on `on*=` handlers |
| `test_pages_load_only_same_origin_assets`: zero scripts, no `http(s)://`, exactly one `<link>` matching `/static/web/app.<hash>.css` | `test_security.py:274` | Part of the page invariants (§5.2): every `src`/`href` on `script`, `link` and `img` is same-origin; every static path is a manifest-hashed `/static/` path; no attribute value starts with `http:`, `https:` or `//` except `xmlns` on inline SVG. The robots assertion at `:286` stays |
| `test_no_template_disables_escaping` (template lint) | `test_security.py:289` | Keep the bans on `\|safe`, `autoescape off`, `<style`, `style=`, `on*=` and `mark_safe` in `powermon/web/**/*.py`. Allow `<script src="{% static … %}" defer></script>` with an empty body, nothing else. Allow the SVG namespace. Replace the fixed template-name set (:292) with the new tree. Add `x-html` and `javascript:` bans and lint `templates/icons/*.svg` too (§5.5). `mark_safe` is allowed in exactly one place, the `{% icon %}` tag module, which only reads repo SVG files by allowlisted name; `SafeString(`, `SafeText`, `@html_safe`, `__html__` and `format_html(` with a non-literal first argument are banned everywhere in `powermon/web/**/*.py` (the old lint at `:309` greps only for `mark_safe`) |
| `test_list_has_no_live_refresh`: no `http-equiv`, no `<script` | `test_templates.py:229` | Retired by UI-05. Keep "no `<meta http-equiv="refresh">`" on every page; live updates come from the poll component only |
| `web/app.css` in the manifest | `test_templates.py:361` | Every `{% static %}` reference in every template resolves in the manifest, and the built asset list is complete (§7.2) |
| One accent button per page | `test_location_page.py:469-486`, `test_templates.py:94`, `test_setup_page.py:119`, `:224` | Dropped with the old design rules (`ADMIN-INVENTORY.md` §5), unless the UI-SPEC re-adopts it as `data-variant` counts |
| `test_css.py` | whole file | Deleted in the plan that deletes `powermon/web/static/web/app.css` |

---

## 4. Test hook contract (the UI-SPEC must adopt this)

The Phase 6 UI-SPEC must contain a "Test hooks" table. The table below is the starter: the UI-SPEC may rename or add hooks, but once it is approved its table is binding, and renaming a hook means changing the UI-SPEC and the tests in the same plan.

### 4.1 Rules

1. **Semantic hooks first.** Use what the page needs anyway: landmarks (`<header>`, `<nav aria-label>`, `<main id="main">`, `<aside>`), exactly one `<h1>`, `<title>`, `role="status|alert"`, `aria-current="page"`, `aria-invalid`, `aria-describedby`, `aria-expanded`, Django's `id_<field>`, `id_<field>_error`, `id_<field>_helptext`, `id_<field>_note`, `<time datetime>`, `<dialog>`, `<form method action>`, `<table>` with `<caption>` and `th[scope]`.
2. **`data-testid` on data regions and on components whose role is ambiguous.** Values are static kebab-case strings. They are never built from data.
3. **State and identity live in a small set of `data-*` attributes with closed vocabularies** (§4.2). These are also the CSS and JS hooks (brief §14: style by `data-status`, never by class names built from data), so they are not test-only noise.
4. **Never classes, never Tailwind utilities, never DOM depth or sibling position.** Order is asserted only where the contract fixes it (the S5 card order, table columns, button order in confirmations).
5. **One DOM per data set where possible.** If a page renders the same data twice (for example a table plus a mobile card list), the second copy uses a different `data-testid` (`location-card`), carries the same `data-location-id`, and repeats no element `id`.
6. **Hooks never carry secrets.** No hook value, `data-*` value or `title` contains a token, a key, a mask of either, or the key tail. The copy button points at an element (`data-copy-target`); it never holds the text itself.
7. **Hooks ship to production.** They are not stripped; they cost nothing and the JS uses most of them.

### 4.2 Shared attribute vocabularies

| Attribute | Values | Used on |
|---|---|---|
| `data-status` | `on`, `off`, `waiting`, `maintenance` (the `LocationStatus.key` vocabulary, `powermon/web/status.py:18-26`) | status pill, sidebar dot, list row, fleet tile, live elements |
| `data-power` | `on`, `off`, `waiting` | the S5 "Power state" row while maintenance is on |
| `data-delivery` | `ok`, `failing` | delivery cell, delivery row, delivery banner |
| `data-level` | `success`, `info`, `warning`, `error` (Django message level tags) | toast |
| `data-tone` | `success`, `info`, `warning`, `error`, `muted` (the Alert tones in `DESIGN-DIRECTION.md` §4; `muted` is the maintenance banner) | banner, alert, callout, state block |
| `data-state` | `on`, `off` (switch); `masked`, `revealed` (device key) | switch form, key region |
| `data-variant` | `primary`, `secondary`, `danger`, `ghost` (final list in the UI-SPEC) | buttons and button-styled links |
| `data-theme` | `light`, `dark`, `system` | `<html>`, theme switch options |
| `data-location-id` | the location pk (integer) | list row, card, sidebar item, every live element |
| `data-live` | `status`, `last-heartbeat`, `delivery`, `first-heartbeat` | elements the poll component updates |
| `data-confirm` | present (no value needed) | the four confirmation entry links (S7, S9, S10, S11) |
| `data-copy-target` | the `id` of the element whose text is copied | copy buttons |
| `data-js-only` | present | controls that need JS (copy buttons, drawer toggle, filter chips); rendered with `hidden`, which the JS removes. The theme switch popover and the account menu open through `popovertarget` and are not `data-js-only` (`DESIGN-DIRECTION.md` §2 Theme) |
| `data-tag` | `alerts-off`, `router-grace` | tags next to the status |
| `data-metric` | `on`, `off`, `maintenance`, `waiting`, `failing` (equal to the status JSON `counts` keys, §8.1) | fleet tiles |

### 4.3 Starter hook table, per page region

**App shell (all signed-in pages; Wave 1)**

| Region | Hook | Tests use it for |
|---|---|---|
| Root | `<html lang="en" data-theme="light\|dark\|system">` | theme rendering (§8.4) |
| Skip link | `a[href="#main"]`, `data-testid="skip-link"` | landmark invariant |
| Sidebar | `<aside id="sidebar" data-testid="sidebar">` containing `nav[aria-label="Main"]` | shell present on app pages, absent on bare pages |
| Main nav items | `data-testid="nav-locations"`, `data-testid="nav-add-location"`; `aria-current="page"` on the active one | navigation, `aria-current` (replaces `test_templates.py:307-314`) |
| Sidebar location list (UI-03) | `nav[aria-label="Locations"][data-testid="sidebar-locations"]`; items `a[data-testid="sidebar-location"][data-location-id][data-status]` with the full name in `title`; `aria-current="page"` on the current location | §8.5 |
| Ops-chat chip | `data-testid="ops-chat-warning"`, present only while `ops_configured` is false | both states |
| Drawer toggle | `button[data-testid="sidebar-toggle"][aria-controls="sidebar"][aria-expanded]` | a11y hook check |
| Top bar | `<header data-testid="topbar">` | shell present |
| Breadcrumbs | `nav[aria-label="Breadcrumb"][data-testid="breadcrumbs"] > ol > li`; last item `aria-current="page"`; separators `aria-hidden="true"` | `breadcrumbs()` (replaces `_crumbs`) |
| Theme switch | `data-testid="theme-switch"`; no-JS fallback `form[method="post"][data-testid="theme-form"]` with buttons `name="theme"` `value="light\|dark\|system"` and `aria-pressed` | §8.4 |
| Account menu | `data-testid="account-menu"`; sign-out `form[method="post"][action="/logout/"][data-testid="sign-out-form"]` | sign-out is POST only |
| Toast regions (UI-09) | `[data-testid="toasts-status"][role="status"]` and `[data-testid="toasts-alert"][role="alert"]`, both present even when empty | announcement, role checks |
| Toast | `[data-testid="toast"][data-level]` inside the region for its level (error → alert region, others → status region); text in `[data-testid="toast-text"]`; `data-sticky` when sticky; dismiss `button[aria-label]` | `messages()` (replaces `_flashes` and friends) |
| Banner slot | `[data-testid="banner"][data-tone]` | delivery and maintenance banners |
| Page header | the single `<h1>`; actions in `data-testid="page-actions"` | title checks |
| Live status chip | `data-testid="live-status"` | present on pages that poll |

**S1 Sign in (bare auth layout; W2a)**

| Region | Hook |
|---|---|
| Form | `form[method="post"][data-testid="sign-in-form"]`, fields `#id_username`, `#id_password`, hidden `next` |
| Error | `[data-testid="form-error"][role="alert"]` |
| Throttled (429) | `[data-testid="throttle-message"][role="alert"]`, inline in the 429 response (never a toast); the `data-retry-after="300"` attribute, matching the `Retry-After: 300` header, only if N11 (throttle countdown) is built |
| Absent | `sidebar`, `sidebar-locations`, `topbar`: the sign-in page shows no location data |

**S3 Locations list (W2b)**

| Region | Hook |
|---|---|
| Fleet summary (UI-04) | `[data-testid="fleet-summary"]`; tiles `[data-testid="fleet-tile"][data-metric]`, each with a number in `[data-testid="fleet-count"]` |
| Add button | `a[data-testid="add-location"][href="/locations/new/"]` |
| Ops-chat banner | `[data-testid="ops-chat-banner"][data-tone="warning"]` |
| Table | `table[data-testid="locations-table"]` with a `<caption>` and `th[scope="col"]` |
| Row | `tr[data-testid="location-row"][data-location-id][data-status]` |
| Name link | `a[data-testid="location-link"]` |
| Status | `[data-testid="status-pill"][data-status]`, text = the status label; tags `[data-testid="tag"][data-tag]` in the order Alerts off, Router grace |
| Last heartbeat | `[data-testid="last-heartbeat"][data-live="last-heartbeat"]` with a `<time datetime>` or the text "Never" |
| Delivery | `[data-testid="delivery"][data-delivery][data-live="delivery"]` |
| Empty state | `[data-testid="empty-state"]` |
| Filter chips (optional, Q8) | `button[data-testid="filter-chip"][data-js-only]` |

**S4 Add and S6 Edit forms (W2d)**

| Region | Hook |
|---|---|
| Form | `form[method="post"][novalidate][data-testid="location-form"]` |
| Error summary | `[data-testid="error-summary"][role="alert"]` with `a[href="#id_<field>"]` jump links |
| Fields | Django ids: `#id_<field>`, `#id_<field>_error`, `#id_<field>_helptext`, `#id_<field>_note`; `aria-invalid="true"` on invalid inputs; `aria-describedby` lists the error, note and help ids |
| Token field | `#id_bot_token` (`type="password"`, never a `value`); current token mask in `[data-testid="masked-token"]` (S6 help only) |
| OFF-after hint | `[data-testid="off-after-hint"]`, server-rendered with the initial value |
| Edit note | `[data-testid="edit-note"]` |
| Actions | `button[type="submit"][data-testid="submit"]`, `a[data-testid="cancel"]` |

**S5 Location page (W2c)**

The cards are `<section id="…" aria-labelledby="…">` elements in this DOM order, which tests assert: `status` → `controls` → `weekly-chart` → `recent-outages` → `settings` → `device-setup` → `danger-zone`. This keeps the test-pinned order of the old sections (Status · Switches · Test message · Recent outages · Settings · Device setup · Reset history · Delete location) with the new chart card inserted after Controls.

| Region | Hook |
|---|---|
| Header | `[data-testid="location-header"]`: the `<h1>` name, `status-pill`, tags, `[data-testid="location-meta"]` |
| Section nav | `nav[aria-label="Sections"][data-testid="section-nav"]` with `href="#<card id>"` links |
| Banners | `[data-testid="delivery-banner"][data-tone="warning"]` (delivery failing, with the fix button), `[data-testid="maintenance-banner"][data-tone="muted"]` |
| Status card | `dl[data-testid="status-panel"]`; rows via `definitions()`; power state row `[data-power]`; delivery row `[data-delivery]`, cause line `[data-testid="delivery-cause"]`, retry line `[data-testid="delivery-retry"]`; live elements `[data-live][data-location-id]` |
| Switches (UI-10) | `form[method="post"][data-testid="switch"][data-switch="maintenance\|alerts\|router-grace"][data-state="on\|off"]` with `input[type="hidden"][name="value"]` = the target state; `button[type="submit"][role="switch"][aria-checked]` = the current state; current-state text in `[data-testid="switch-state"]` |
| Test message | `form[method="post"][data-testid="test-message-form"]` |
| Weekly chart (UI-06) | `figure[data-testid="weekly-chart"]` with `img[src="/locations/<pk>/chart.png"][alt][width][height]`; or `[data-testid="weekly-chart-empty"]` when the location has no stored history (`has_history` false, `powermon/web/location_views.py:525`). A waiting location with history (for example after a restore) renders the image (`DESIGN-DIRECTION.md` §3 Chart states; §8.2) |
| Recent outages | `table[data-testid="outages-table"]` with a `<caption>`; rows `tr[data-testid="outage-row"]`, the in-progress row also `data-in-progress`; `a[data-testid="remove-outage"][data-confirm]` with the accessible name "Remove the outage from {start}"; notes `[data-testid="off-time-note"]`, `[data-testid="in-progress-note"]`; empty states `[data-testid="outages-empty"][data-reason="none-in-14-days\|no-history"]` |
| Settings | `dl[data-testid="settings-panel"]`; token mask `[data-testid="masked-token"]`; `a[data-testid="edit-location"]` |
| Device setup card | heartbeat URL in `code#heartbeat-url`, copy button (§4.2), `a[data-testid="open-setup"]`; never the key |
| Danger zone | rows `#reset-history` and `#delete-location`; `a[data-testid="reset-history"][data-confirm]` or `[data-testid="reset-unavailable"][data-reason="in-progress\|no-history"]` (exactly one of three, parity); `a[data-testid="delete-location"][data-confirm]` |

Duplicated entry points use their own testids, because `by_testid` asserts exactly one match: the header kebab uses `menu-reset-history` / `menu-delete-location` and follows the same three reset states; the delivery banner's form is `banner-test-message-form`.

**S8 Device setup (W2e)**

| Region | Hook |
|---|---|
| Steps | `ol[data-testid="setup-steps"]`, items `[data-testid="setup-step"][data-step="before\|url\|key\|examples\|first-heartbeat"]` |
| URL | `#heartbeat-url` (keep today's id), copy button |
| Key | `[data-testid="device-key"][data-state="masked\|revealed"]`; value element `#device-key` (keep today's id); masked text `aria-hidden="true"` plus the visually hidden "Hidden key ending in XXXX" |
| Reveal / Hide | `form[method="post"][data-testid="reveal-form"]`; `a[data-testid="hide-key"]` |
| Examples | `#example-curl`, `#example-cron`, `#example-wget-gnu`, `#example-wget-busybox` (keep today's ids); all four are in the server HTML whatever tab is active; tabs `role="tablist"` / `role="tab"` / `role="tabpanel"` |
| Copy buttons (UI-08) | `button[data-testid="copy"][data-copy-target][data-js-only][hidden]`; on the masked page only the URL has one |
| Link-previewer warning | `[data-testid="previewer-warning"][data-tone="warning"]` |
| Regenerate entry | `a[data-testid="regenerate-key"][data-confirm]` |
| First heartbeat (UI-05) | `[data-testid="first-heartbeat"][data-live="first-heartbeat"][data-location-id][aria-live="polite"]`; it never wraps the key region |

**S7, S9, S10, S11 confirmations: full page and modal fragment (W2f)**

| Region | Hook |
|---|---|
| Fragment root | `[data-testid="confirm"]`, the shared partial included by the full page and returned alone as the fragment |
| Title | `[data-testid="confirm-title"]` with an `id` the `<dialog aria-labelledby>` references; on the full page the title is the page's single `<h1>` |
| Details (S10) | `dl[data-testid="outage-details"]` (Start, End, Off time) |
| Consequences | `ul[data-testid="consequences"]` or `ol` |
| State block (S9) | `[data-testid="state-block"][data-tone][data-state-block="warning-on\|power-off\|waiting\|maintenance"]`, exactly one |
| Form | `form[method="post"][data-testid="confirm-form"]` with the same `action` as today, the CSRF input, and on S9 `input[type="hidden"][name="marker"]` |
| Buttons | Keep first: `[data-testid="keep"]` (a link on the full page; may be a `form[method="dialog"]` button in the fragment); destructive last: `button[type="submit"][data-testid="confirm-submit"][data-variant="danger"]` |
| Keep targets | S10 "Keep outage" → `/locations/<pk>/#recent-outages`; S11 "Keep history" → `/locations/<pk>/#reset-history` (the anchor fix from `ADMIN-INVENTORY.md` §0) |

**E1 404, E2 403 CSRF, E3 500 (error layout that reads no context variable; W2a)**

| Region | Hook |
|---|---|
| Root | `<html lang="en" data-theme="system">`, hard-coded (`FRONTEND-STACK.md` §5) |
| Body | `[data-testid="error-page"][data-code="404\|403\|500"]`, the `<h1>`, `a[data-testid="back-to-locations"][href="/"]` |
| Absent | `sidebar`, `topbar`, `toasts-*`, any location name |

---

## 5. Wave 0 test infrastructure (before any page template)

### 5.1 `tests/web/pages.py`: one parsing module

Replace the ~30 copy-pasted regex helpers with one module that uses a real parser, so attribute order, whitespace and nested icons no longer matter.

**Parser (brief §12 Q3, default):** `beautifulsoup4` with `soupsieve` for CSS selectors.
- Add both to `[dependency-groups].dev` in `pyproject.toml` with an upper bound like the other dev pins, lock them in `uv.lock` under the existing `exclude-newer` policy, and pass them through the INV-26 legitimacy checkpoint (publisher, release history, licence: both MIT).
- They are installed only where dev dependencies are (the Docker `dev` target); the runtime image built with `uv sync --locked --no-dev` never has them.
- Use the stdlib `html.parser` builder (`BeautifulSoup(html, "html.parser")`). No lxml or html5lib.
- **Alternative** if the checkpoint rejects them: a ~150-line helper on stdlib `html.parser.HTMLParser` that builds a small element tree and supports lookup by id, `data-testid`, role and tag. No CSS selectors, so the API below stays the same.

**API** (names are a suggestion; the point is that tests never see raw HTML):

| Function | Returns |
|---|---|
| `parse(response)` | the parsed page; asserts `Content-Type: text/html` |
| `by_testid(page, name)` / `all_by_testid(page, name)` | one element (asserts exactly one) / all |
| `main(page)`, `h1(page)`, `title(page)` | landmarks and headings |
| `text(el)` | normalised text: whitespace collapsed, visually hidden text included, `aria-hidden="true"` subtrees (icons, the masked-key glyphs) skipped |
| `messages(page)` | list of `(level, region_role, text)` from the toast hooks |
| `breadcrumbs(page)` | list of `(text, href)` |
| `table(page, testid)` | headers and rows of cell texts |
| `definitions(page, testid)` | ordered `(term, value)` pairs of a `<dl>` |
| `field_error(page, name)`, `field(page, name)` | the error text; the input with its `aria-*` attributes |
| `post_form(page, action)`, `hidden_value(form, name)`, `form_values(page, testid)` | forms and values |
| `code_block(page, id)` | the exact text of a code block, nothing stripped |
| `section(page, id)` | the element with that id |
| `assert_page(response, **expect)` | the page invariants (§5.2) |
| `assert_no_injected_script(html)` | §3.5 |
| `assert_no_secrets(body, secrets, allow=())` | §5.3 |

Selectors passed to `pages.py` may use ids, `data-*` attributes, roles, ARIA attributes and tag names. They may not use class selectors (§5.4).

### 5.2 Page invariants: `assert_page()`

Applied to every full HTML page in the render matrix (§7.1) and available to every test:

1. The expected status code.
2. `<title>` matches `^{page title} · Power Monitor$`.
3. Exactly one `<h1>`.
4. Landmarks: one `<main id="main">` and a skip link to it. App pages have the `sidebar` and `topbar` hooks; bare pages (sign-in, error pages) have neither.
5. `<html lang="en">` with `data-theme` in `{light, dark, system}`.
6. `<meta name="robots" content="noindex, nofollow">` (R15). The viewport meta has no `maximum-scale` and no `user-scalable=no` (zoom stays allowed; TailAdmin's viewport line blocks it, D6-02).
7. No `<script>` with a body. Every `<script src>` and every `<link href>` (stylesheet, preload, icon) resolves to a manifest-hashed `/static/` path. Font preloads carry `crossorigin`.
8. No `style=` attribute, no `on*=` attribute, no `<style>` element, no `javascript:` URL, no `<meta http-equiv="refresh">`.
9. No attribute value starts with `http:`, `https:` or `//`, except `xmlns` on inline SVG. The heartbeat URL and the device examples appear only as text.
10. Every `<form>` has `method="post"` and a `csrfmiddlewaretoken` input, or `method="dialog"` (the modal's Keep). There are no GET forms; if filter chips ever need one, allow it by name.
11. Every `<img>` has `alt`, `width` and `height`. Every `<button>` and every link with only an icon has an accessible name. Every visible `<input>`, `<select>` and `<textarea>` has a `<label for>` or `aria-label`. Every inline `<svg>` without a `<title>` is `aria-hidden="true"` and `focusable="false"`.
12. Element `id`s are unique on the page (labels and `aria-describedby` depend on it).
13. Every `<table>` has a `<caption>` or `aria-label`/`aria-labelledby`, and every header cell has `scope`.
14. The response headers include the CSP equal to the policy constant, `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin` and `X-Frame-Options: DENY`.
15. No secret from the test fixtures appears (§5.3), except where §9 allows it.

### 5.3 Shared secret fixtures and scan helper

- Move the secret constants of `tests/web/test_inv23_pages.py:59-71` (`TOKEN`, `SECRET`, their `MASKED` forms, `TOKEN_2`, `TOKEN_3`) into a shared module used by every new test, so every scan looks for the same strings.
- `assert_no_secrets(body, secrets, allow=())` works on `str` and `bytes` (the PNG). It checks the full token, its secret part, the token mask `{bot_id}:••••••••`, the full device key, the key mask `••••••••••••XXXX`, the bare key tail used in the hidden "ending in XXXX" text, and any old key after a regenerate. It also checks every `Location` header.
- `allow` names what a surface may show (for example the token mask in the settings panel). §9 is the table of what each surface allows.

### 5.4 Guard: no assertion on classes (a ratchet)

- A test tokenises every `tests/web/**/*.py` file and fails on any string literal that contains `class=` or a CSS class selector (`.name` inside a selector passed to `pages.py`).
- On day one 164 lines would fail, so the guard starts with an **allowlist of files still to migrate**. Each page plan removes its files from the allowlist when it migrates them. The W3 clean-up plan asserts the allowlist is empty and deletes it.
- `pages.py` itself exposes no class lookup.

### 5.5 Front-end lint tests (they stand in for browser tests)

Without Playwright, most JS and CSP failures would show up only in the browser console. These cheap static checks catch the common ones at test time:

- **Templates** (`powermon/web/templates/**/*.html`): the rewritten template lint (§3.5). Also: every `{% icon "name" %}` names a file in `powermon/web/templates/icons/`; every `{% static %}` path exists in the manifest.
- **Icon SVGs** (`templates/icons/*.svg`): no `<script`, no `on*=`, no `style=`, no `href`/`xlink:href`, no `<foreignObject>`.
- **Alpine under the CSP build** (`@alpinejs/csp` 3.17.4, `FRONTEND-STACK.md` §3):
  - Every `x-data` value in the templates is a name registered with `Alpine.data("<name>"` in `powermon/web/static/web/admin.js`, and every registered name is used.
  - Directive values (`x-on:*`, `@*`, `x-bind:*`, `:*`, `x-show`, `x-text`, `x-model`) contain only what the CSP build evaluates; ban arrow functions, template literals, globals (`window`, `document`, `console`, `JSON`, `Math`) and nested property assignments. Verify the exact grammar against the 3.17.4 docs when writing the test.
  - No `x-html`. No string `:style` bindings (they call `setAttribute('style')`, which the CSP blocks).
  - Fail on `{{` or `{%` inside any attribute whose name starts with `x-`, `@` or `:` in `powermon/web/templates/**/*.html`. Autoescaping does not protect directive values (`FRONTEND-STACK.md` §3 rules); server values reach components only through `data-*` attributes.
- **`admin.js`:**
  - No `eval(`, `new Function`, `document.write`, `setTimeout("`/`setInterval("` with a string, `confirm(`, `alert(`, `import`/`export`.
  - No browser storage beyond the sidebar flag (R4): no `sessionStorage`, `indexedDB`, `caches.` (Cache Storage) or `navigator.serviceWorker`; no `history.pushState` / `replaceState` with a state object, and no `window.name`.
  - `localStorage` only inside `try/catch` and only with the allowlisted key for the sidebar rail (brief §12 Q9).
  - `document.cookie` only in the `theme` component, and it only writes `theme=`.
  - No form-draft persistence: never read `#id_bot_token`, and no `FormData` outside the submit guard.
  - No `innerHTML`, `outerHTML`, `insertAdjacentHTML`, `createContextualFragment` or `document.write`: the modal loader parses the fragment with `DOMParser` and moves the root (`FRONTEND-STACK.md` §3); everywhere else dynamic text uses `textContent`.
  - Every `fetch(` uses a same-origin relative URL taken from a `data-*` attribute or a `{% static %}`/`{% url %}`-rendered value, and passes `redirect: "manual"`, with no `method` other than GET and no `body`; no `XMLHttpRequest` and no `navigator.sendBeacon`.
  - No hard-coded `/static/` path and no `http(s)://`.
- **The CSS entry** (`powermon/web/assets/css/app.css`): starts with `@import "tailwindcss" source(none)`; its `@source` paths are only the templates and `admin.js` (no repo-wide scan that could pick up `.planning/` or env files).

### 5.6 Route coverage tests

New endpoints must not slip past the security tests. Two tests iterate over `django.urls.get_resolver()`:
- **Default deny (R14):** an anonymous GET to every named route except the exempt set redirects to `/login/?next=…` (POST-only routes answer 405 to GET, which is also fine). The exempt set is exactly `{login, logout, heartbeat, healthz}`; a new exempt route fails the test.
- **Matrix completeness:** every named admin route appears in the render matrix (§7.1) and in the secret-scan matrix (§9), or in an explicit list of POST-only routes. A new route without coverage fails the test.

### 5.7 Other Wave 0 items

- Rewrite the policy tests in §3.5 (CSP, script, assets, template lint).
- Change the coverage gate (§10).
- Swap the helpers in `tests/web/test_inv23_pages.py` to `pages.py` and the shared secret module, so later plans can extend it.

---

## 6. Migrating the existing tests

### 6.1 Ownership

- Each page plan migrates the files it owns (§2, "Migrates in"), in the same plan as its template.
- **Files with two owners** collide when Wave 2 plans run in parallel. For each, the planner does one of:
  - split the file in Wave 0 along the plan boundary, keeping every test function name (INV/K/LOC IDs stay searchable; the old VALIDATION maps reference files, so note the move in the Phase 6 VALIDATION map); or
  - give the file to one plan and order the other plan after it.
- Suggested splits: `test_history_pages.py` → outages-card tests (W2c) and remove/reset confirmation tests (W2f); `test_regenerate.py` → confirmation (W2f) and POST response (W2e); `test_delivery_display.py` → list (W2b) and detail (W2c); `test_form_null_chars.py` → sign-in (W2a) and location forms (W2d); `test_security.py` → policy (W0) and error pages (W2a); `test_templates.py` → shell (W1) and list (W2b).

### 6.2 Buckets

- **Edit (75):** replace the helper calls with `pages.py`; the assertions keep their meaning.
- **Roles (22):** replace the role helpers with `messages()`; assert level, region role and exact text.
- **Rewrite (58):**
  - Delete `test_css.py` (14 functions, about 41 cases) and the CSS-literal checks (`test_location_page.py:544`, `test_delivery_display.py:249`).
  - About 10 small form-error tests (`test_locations.py:463-589`) are really helper swaps.
  - About 34 functions whose subject is page content (list rows and labels, panels, crumbs, confirmation pages, setup guidance) are re-authored against the hook contract.
- **Copy amendments A1–A7:** apply them in the plan that owns the string, after its tests import the constant (§3.3).

---

## 7. New tests for the rebuilt pages and the UI requirements

### 7.1 Render matrix (~35 cases)

One parametrized test renders every route × state and runs `assert_page()` (§5.2) plus the secret scan (§5.3) on each:
- **S1:** normal, wrong credentials, throttled 429.
- **S3:** empty; rows of every status (on, off, waiting, maintenance); alerts off and router grace tags; delivery failing today and on an earlier day; ops chat unset.
- **S4 / S6:** GET, invalid POST (with and without a typed token), S6 after a location rename.
- **S5:** outages listed; in-progress row; both empty states; reset possible / refused in progress / no history; delivery failing with each cause line and the migrate line; maintenance on with power on and with power off; each of the 7 Phase 5 flashes and the 12 switch and 8 test-message flashes as toasts; chart card for a location with stored history and for one without.
- **S7, S9 (each of the 4 state blocks), S10 (with and without consequence 2), S11:** full page.
- **S8:** masked; revealed by Reveal; revealed by Regenerate (both flashes).
- **E1, E2, E3:** 404, 403 CSRF, 500 (via `render_to_string("500.html")` with no context).
- **Theme:** one app page and the sign-in page under each theme cookie value.

### 7.2 Assets and supply chain (~8)

- The manifest has entries for the built CSS (`web/build/app.css`), `web/admin.js`, `web/vendor/alpine-csp-3.17.4.min.js`, the four Inter `.woff2` files and the favicon; every `{% static %}` reference in every template resolves (manifest-strict storage raises otherwise).
- The built CSS contains no unprocessed `@tailwind`, `@import`, `@source`, `@theme`, `@plugin` or `@custom-variant` directives; every `url()` is relative or `data:`; no `http(s)://`; it has the `[data-theme=dark]` and `prefers-color-scheme: dark` selectors (smoke check that the dark variant was built).
- No `sourceMappingURL` comment that points to a file which is not shipped (`FRONTEND-STACK.md` §6 pitfall 5).
- **Vendor manifest** (`powermon/web/assets/vendor-manifest.json`): every listed file exists and its sha256 matches; every file under `static/web/vendor/` (except `LICENSES/`), `static/web/fonts/` and `templates/icons/` is listed; every licence named has its text in `static/web/vendor/LICENSES/`; the Dockerfile `--checksum` values for the Tailwind binaries equal the manifest; versions are exact (no ranges).
- Templates and `powermon/web/static/web/**` (excluding `LICENSES/`) contain no third-party URL; only `http://www.w3.org/2000/svg` is allowed.
- The `{% icon %}` tag output has `aria-hidden="true"` and `focusable="false"`, escapes its `class` argument, and raises on an unknown name.
- With DEBUG off, `{% static 'web/admin.js' %}` resolves to a hashed name (`web/admin.<12 hex>.js`).

### 7.3 CSP policy (~6)

- Full-string equality with the brief §8 policy in `tests/web/test_security.py` and `tests/test_walking_skeleton.py`:
  `default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'`
- Negative assertions on the header: no `'unsafe-inline'`, no `'unsafe-eval'`, no `*`, no `http:` or `https:` source, no `'strict-dynamic'`, no nonce.
- The header is present on HTML pages, the status JSON, the chart PNG, the confirmation fragments, the theme redirect, 404, 403 CSRF, 500 and static files, and absent only on the exact path `/hb` (`/hb/` and `/hbx` still get it). For 500, request a test-only URLconf view that raises, with `Client(raise_request_exception=False)`. `server_error(RequestFactory().get("/"))` (`test_security.py:97-99`) bypasses the middleware and never carries the header.
- Why the policy holds: standard Alpine needs `'unsafe-eval'` (so the `@alpinejs/csp` build is used); `img-src data:` exists only for `@tailwindcss/forms` data-URI icons; JS chart libraries inject `<style>` (so none are used). The full-string test is what keeps these choices honest.

### 7.4 Progressive enhancement (the no-JS path)

- Every state-changing control is a submit button inside a `form[method="post"]` with the CSRF input. No state change needs JS.
- Every confirmation entry point (`data-confirm`) is a plain `<a href>` to the GET confirmation page (R7); none is a form that acts directly.
- Every JS-only control carries `data-js-only` and is rendered `hidden`, so a page without JS shows no dead buttons.
- Reveal and Regenerate stay top-level POSTs answered 200 with `no-store` (existing tests).
- All four device examples and both setup states render fully without JS (tabs are an enhancement).

### 7.5 Server-side checks per UI requirement

| Req | Automated checks (Django client) |
|---|---|
| UI-01 shell | Shell hooks present on every app page and absent on bare pages; breadcrumbs per page with `aria-current` on the last item and `aria-hidden` separators; `aria-current="page"` on the active nav item |
| UI-02 theme | §8.4 |
| UI-03 sidebar | §8.5 |
| UI-04 fleet tiles | Counts per `data-metric` match the fixture; maintenance counts as maintenance, never as on or off; delivery failing is counted independently of status (a location can be Off and failing); zero counts render as `0` |
| UI-05 live | §8.1 for the endpoint. Every `[data-live]` element has a `data-location-id`; the list, S5 and S8 render the `live-status` hook; S8 renders `first-heartbeat` for a waiting location; no `[data-live]` element contains `#device-key` |
| UI-06 chart | §8.2 for the route. The S5 card renders `img` with the route URL, `alt`, `width`, `height` for a location with stored history, and `weekly-chart-empty` for one without (`has_history` false) |
| UI-07 modals | §8.3 for the fragments. Every destructive entry point has `data-confirm`; the page contains one `<dialog>` shell with `aria-labelledby` |
| UI-08 copy | Revealed S8: a copy button for the URL, the key and each example, each `data-copy-target` pointing at an existing id. Masked S8: only the URL copy button. The text of each target equals the generated value exactly (compare with `powermon/locations/examples.py`), with no leading or trailing whitespace. In the revealed response the key appears only inside `#device-key` and the four example blocks, never in an attribute. Its count is 4 + the number of cron lines (5 at a period of 60 s or more, 6 at 30 s, 10 at 10 s; `powermon/locations/examples.py:35-56` writes one cron line per offset below 60 s). Assert it structurally: after removing the text of `#device-key` and the four example blocks, the key occurs nowhere in the body |
| UI-09 toasts | Each Django level maps to its `data-level`; error toasts sit in the alert region, the rest in the status region; the two warning flashes of the test message (maybe delivered, rate limited) render `data-level="warning"`, not success (amendment A5); sticky successes carry `data-sticky`; every dismiss button has an accessible name |
| UI-10 toggles | Each switch form posts `value` = the opposite of its `data-state`; `aria-checked` equals the current state; after the POST and redirect the state flips; a repeat answers "… Nothing changed." (existing tests) |
| UI-11 relative times | Every displayed instant (list last heartbeat; S5 on since, outage since, last heartbeat, failing since; S8 meta) is a `<time datetime>` whose value parses to the stored aware instant and whose text is the unchanged `display_time` output, with a sibling `[data-relative]` whose value equals that `datetime` (`DESIGN-DIRECTION.md` §4 Relative time); "Never" has no `<time>` |
| UI-12 a11y | The invariants in §5.2 (labels, names, captions, unique ids, zoom allowed, `aria-hidden` icons); error summary links target existing field ids; 100-character names render in full in `title` and text (01-UAT #7 re-expressed). Contrast, focus, 44 px and 360 px are checked in §11 |
| UI-13 assets | §7.2 and §7.3 |

---

## 8. Tests for the new server surfaces

Common to all five: they sit behind default-deny login (R14), carry the CSP and the framework headers, write nothing on GET, and are in the secret-scan matrix (§9). "Writes nothing" is asserted with `CaptureQueriesContext`: no `INSERT`, `UPDATE` or `DELETE` statement.

### 8.1 Status JSON for live refresh (UI-05)

`GET /locations/status.json` (brief §12 Q6; the URL name is set by the plan).

- **Login required:** anonymous GET → 302 to `/login/?next=…`; the body has no location data.
- **GET only:** POST, PUT, PATCH and DELETE → 405 (HEAD is allowed only if the view uses `require_safe`). GET writes nothing.
- **Never cached:** `Cache-Control` contains `no-store` and `private` (`never_cache`). `Vary` contains `Cookie` (the private browser cache stays per session).
- **Content:** `Content-Type: application/json`; the top level is an object; `X-Content-Type-Options: nosniff`; the query string is ignored and never echoed (no JSONP: `?callback=x` changes nothing).
- **Shape is stable.** The test compares the exact key sets with constants in the test, so any change to the payload is a deliberate change to the test. Starter shape, the one in `DESIGN-DIRECTION.md` §6 "Live status" (the UI-SPEC or plan may adjust it; the test pins whatever is chosen):
  ```json
  {
    "generated_at": "2026-10-04T14:05:09+03:00",
    "ops_configured": true,
    "counts": {"on": 5, "off": 1, "maintenance": 1, "waiting": 1, "failing": 2},
    "locations": {
      "12": {
        "status": "off",
        "label": "Off",
        "power": "off",
        "last_heartbeat": {"iso": "2026-10-04T13:58:40+03:00", "display": "2026-10-04 13:58:40 EEST"},
        "since": {"kind": "outage", "iso": "2026-10-04T13:59:50+03:00", "display": "2026-10-04 13:59:50 EEST"},
        "delivery": {"state": "failing", "text": "Failing since 13:20 (http_403)"}
      }
    }
  }
  ```
  - `status` is in the `data-status` vocabulary and `label` equals `STATUS_LABELS[status]` (`powermon/web/status.py:18`); `power` is the stored power key (`LocationStatus.power_key`).
  - `last_heartbeat` is `null` when there was no heartbeat (the page renders "Never"); `since` is `null` while waiting. Every `display` equals the `display_time` output.
  - `delivery.text` equals `views.delivery_text()` (`powermon/web/views.py:165`) or "OK".
  - `counts` equal the fleet tiles (`data-metric` values are the same keys, §4.2); a location can count as `off` and `failing` at once.
  - Only fixed vocabulary, times formatted from stored instants and short codes. No location name, no chat ID, no Telegram description text (R10).
- **Rows:** every non-deleted location and no soft-deleted one; a location deleted between two polls disappears from the next payload.
- **Query count is constant:** the same number of queries for 1 and for 6 locations (no N+1), pinned with `django_assert_num_queries`.
- **No secrets, not even masked:** the body contains no full token, no token secret part, no token mask, no full key, no key mask, no key tail, and no `•` character at all.
- **No side effects on the session:** the response sets no cookie (polling must not refresh the session or CSRF cookie), and a pending flash message survives a status JSON GET (it shows on the next page view).
- **Manual (§11):** after sign-out in another tab the poll stops with the "Live updates paused" chip instead of parsing the sign-in page.

### 8.2 Chart PNG route (UI-06)

`GET /locations/<pk>/chart.png` (`DESIGN-DIRECTION.md`; the URL name is set by the plan).

- **Login required:** anonymous GET → 302 to sign-in; never PNG bytes.
- **GET only:** POST → 405. GET writes nothing to the database.
- **Content:** `Content-Type: image/png`; the body starts with the PNG signature `\x89PNG\r\n\x1a\n`; Pillow (imported in the test, not the view) opens it, and its size equals the chart's width and height constants in `powermon/chart/render.py`.
- **Same image as the channel:** under a `FakeClock`, the body equals `render.render_png(source.load_week(pk, today=…, now=…, tz=…, live=True), lang=location.language, name=location.name)`, the same call the worker makes at `powermon/chart/lifecycle.py:636-637`. The renderer is deterministic, which the chart goldens already rely on.
- **Cache headers:** `Cache-Control` contains `private` and either `no-store` or a `max-age` of at most 60 seconds; never `public`, never `s-maxage` (brief §6.3 and §12 Q5). `Vary` contains `Cookie`, so a signed-out browser never reuses the cached image. If the plan adds `Cross-Origin-Resource-Policy: same-origin` to the JSON and PNG views, assert it too.
- **Render cache (if the plan adds one, brief §12 Q5):** two GETs within the TTL call `render_png` once (count with a monkeypatched wrapper); after the TTL it renders again; two locations never share an entry. The cache is in-process (Django's default local-memory cache or a module-level dict): the architecture allows no cache service (`.claude/CLAUDE.md`).
- **404:** for an unknown pk and for a soft-deleted location, without calling the renderer, including when that location's PNG is still in the render cache. The view looks up the non-deleted location before the cache.
- **No history.** The worker posts charts only for monitored locations (status on or off; `powermon/chart/lifecycle.py:12`), so a waiting location has no channel chart.
  - The S5 card shows `weekly-chart-empty` and emits no `<img>` for a location with no stored history (`has_history` false; template test). A waiting location with history (after a restore) renders the image.
  - The route for a location with no history never answers 500. The plan pins one behaviour with a test: 200 with the fully not-monitored week (recommended: no special case, if `render_png` draws an empty week; verify), or 404.
- **No secrets:** the raw bytes contain no token, token secret, mask, key or key tail (ASCII and UTF-8); the PNG has no text chunks (`Image.open(...).info` holds no string values beyond what Pillow writes by itself).
- **Lazy import:** the view imports `powermon.chart.render` inside the function, so `tests/chart/test_lifecycle.py:1445` (`test_web_process_never_imports_pillow`) keeps passing and the web process does not carry Pillow until a preview is requested. It does not import `powermon.chart.lifecycle` at module level either; the same test asserts it is absent from `sys.modules` after `import powermon.urls`.
- **Manual (§11):** transient memory of one render on the VPS (≈ +25 MB expected, brief §12 Q5) and the visual match with the pinned chart.

### 8.3 Confirmation fragments for the modal (UI-07)

The four confirmation GETs (S7 delete, S9 regenerate, S10 remove outage, S11 reset) return the `[data-testid="confirm"]` partial alone when the request carries `X-PM-Fragment: 1` (`DESIGN-DIRECTION.md` §3).

- **Same pre-checks as the GET pages.** Run the existing confirmation-GET tests under both variants with a parametrized fixture (`headers` = none or `{"X-PM-Fragment": "1"}`). In both variants:
  - anonymous → 302 to sign-in;
  - unknown or deleted location → 404; invalid or out-of-range `start_us` → 404, never echoed (R12);
  - S10 outage gone → 302 to S5 with the info flash; outage in progress → 302 with the error flash;
  - S11 power off → 302 with the error flash; no history → 302 with the info flash.
  - The status and the `Location` header are identical between the variants. The fragment variant of a refusal queues no flash; the full-page variant queues exactly one. Simulate the JS sequence for each refusal (S10 gone, S10 in progress, S11 power off, S11 no history): a fragment GET, then the full GET with `follow=True`. Exactly one toast with the expected level and text appears.
- **Fragment content:**
  - No `<html>`, `<head>`, `<body>` or `<title>`; no `sidebar`, `topbar`, `toasts-*` or `breadcrumbs` hooks.
  - `text()` of the fragment's `confirm` element equals `text()` of the full page's `confirm` element (one shared partial, one copy source).
  - The same `confirm-form`: the same `action`, a CSRF input, and on S9 the same `marker` value as the full page.
  - Keep first and the destructive button last (`data-variant="danger"`).
  - Inside `confirm-form` (page and fragment), every `<button>` other than `confirm-submit` has `type="button"`.
  - S9 has exactly one `state-block` for each of the 4 states; S10 shows `outage-details` and consequence 2 only when `off_us < span`.
- **Headers:** both the fragment and the full page carry `Vary` containing `X-PM-Fragment`, and `Cache-Control` with `no-store` (`never_cache`). S9 keeps `never_cache` on the page as today. The CSP header is present. The fragment response carries the response header `X-PM-Fragment: 1`; the full page does not (the loader injects only when it is present).
- **Only the exact header value `1` gives a fragment;** absent, `0`, `true` or an empty value give the full page.
- **The header never changes a POST.** A POST with `X-PM-Fragment: 1` gives exactly the response without it: S7, S10 and S11 redirect with their flashes; the S9 POST returns the full revealed setup page with `no-store`, never a fragment. The key is never delivered in a fragment.
- **Only the four confirmation GET handlers (S7, S9, S10, S11) honour `X-PM-Fragment`.** The check lives in those views (or a mixin applied only to them), never in a base template, context processor or middleware. For every other named route, especially S8 GET, the Reveal POST, the S9 POST, S5, the status JSON and the theme POST, the response to a request with `X-PM-Fragment: 1` is the same as the one without: the same status and headers (except `Date` and `Vary`), and for HTML a full page with `<html>` and the shell hooks. Where the same state can be rendered twice (S8 GET, the Reveal POST, S5, the status JSON), the bodies are equal after blanking the `csrfmiddlewaretoken` values, which Django masks differently on every render.
- **No secrets:** no fragment contains the token or its mask (the confirmations have no settings panel); the S9 fragment contains no key, no key mask and no key tail; the marker is the HMAC, not key characters (existing check, applied to the fragment too).
- **Flashes are not consumed:** a pending flash survives a 200 fragment GET and shows on the next page view.
- **JS behaviour, manual UAT only (§11):** the modal loader uses `fetch(url, {redirect: "manual"})`; on an opaque redirect (or any non-200, or a response without the `X-PM-Fragment: 1` header or the `confirm` root) it calls `location.assign(url)`, so the full GET queues the refusal flash once and the browser shows it on the right page. The Django client cannot exercise the browser part; the server part is the fragment-then-full-GET simulation above.

### 8.4 Theme cookie and POST fallback (UI-02)

The cookie is `theme` with values `light`, `dark`, `system` (`FRONTEND-STACK.md` §5). The fallback is a POST to the theme endpoint (for example `/theme/`; the plan sets the path).

**Rendering:**
- No cookie → `<html data-theme="system">`. Each allowlisted value → the same value.
- Any other value (`DARK`, `dark;`, `light `, an empty value, a 4 KB string, `"><script>alert(1)</script>`) → `system`. The raw cookie value never appears in the HTML.
- The sign-in page renders the theme from the cookie too. The error pages hard-code `data-theme="system"` and ignore a `theme=dark` cookie on 404 and 403-CSRF (both are rendered with the request).
- Reading the theme adds no database query.
- No GET response (app page, sign-in, 404, 403-CSRF) carries a `Set-Cookie: theme` header; only the theme POST sets the cookie.

**POST fallback:**
- **CSRF:** with `Client(enforce_csrf_checks=True)` and no token → the 403 Form expired page, and no `theme` cookie is set.
- **POST only:** GET → 405.
- **Login required:** anonymous POST with a valid token → 302 to sign-in, no cookie set.
- **Allowlist:** each valid value → 302 and `Set-Cookie: theme=<value>` with `Path=/`, `SameSite=Lax`, `Max-Age` of about one year, `Secure` exactly when `SESSION_COOKIE_SECURE` is true (production), and not `HttpOnly` (the JS toggle updates it). An invalid or missing value → no cookie, and a fixed answer the plan pins (recommended: 400 with an empty body, like a bad switch value).
- **No open redirect (R8):** the form has no `next` field, and the view ignores one if posted. Test vectors: `next=https://evil.example/`, `next=//evil.example/`, `next=/\evil.example`, `Referer: https://evil.example/locations/1/`, `Referer: javascript:alert(1)`, no `Referer`. Every one ends at the fixed target (`/`). If the plan supports going back to the same-host `Referer` (brief §6.3 R8), a same-host GET page such as `/locations/1/` is honoured, and the check uses `url_has_allowed_host_and_scheme` with the request host and `require_https=request.is_secure()`. A same-host Referer that is a confirmation or POST-result URL maps to its parent page: `/locations/1/setup/regenerate/` → `/locations/1/setup/`, `/locations/1/delete/`, `/locations/1/reset/` and `/locations/1/outages/<start_us>/remove/` → `/locations/1/`.
- **Writes nothing** to the database and adds no flash.
- **Manual (§11):** no flash of the wrong theme on reload in each theme; System follows an OS theme change live.

### 8.5 Sidebar location list context processor (UI-03)

- **Signed-in pages only.** For an anonymous request (sign-in page, anonymous 404, anonymous 403-CSRF) it runs no location query and the page contains no location name (fixture with a distinctive name).
- **Lazy:** it queries only when a template uses it, so the status JSON, the chart PNG, redirects and fragments run no sidebar query (pin their query counts).
- **One query** for the list (locations with `select_related("state")`), the same count for 1 and 6 locations.
- **Rows:** non-deleted locations only, in the list order (`Lower("name")`, then pk, as in `powermon/web/views.py:196-198`); each with `data-status` and the status label as text for screen readers; `aria-current="page"` on the current location on S5, S6, S8 and its confirmation pages, and on none on the list.
- **No secrets.** The context items are a small frozen dataclass, and a test pins its field set (for example `{"pk", "name", "status", "label"}`), so no `Location` instance with its token or key hash ever reaches the context. The HTML scan finds no token, mask or key on any page.
- **Escaping (R1):** a name `<script>alert(1)</script>` renders as text in the sidebar item and its `title`; a 100-character name renders in full in `title`.
- **Error pages (R11).** For an anonymous client and for a signed-in client:
  - `GET /no-such-page-xyz` and a CSRF-failing POST (`Client(enforce_csrf_checks=True)`) each run no query on the location or state tables (`CaptureQueriesContext`);
  - each contains no location name (distinctive fixture name), no `theme` cookie value and no toast;
  - each body equals `render_to_string("404.html")` / `render_to_string("403_csrf.html")` rendered with no context and no request (proof that the layout reads no context variable);
  - a pending flash survives both pages and shows on the next app page.

  The anonymous 403-CSRF case matters because `CsrfViewMiddleware` runs before `LoginRequiredMiddleware` (`powermon/settings.py:58-61`), so an unauthenticated POST to any admin URL renders `403_csrf.html` with every context processor.
- **Ops-chat chip:** present iff the ops chat is not configured.

---

## 9. INV-23 secret-scan matrix (extended)

`tests/web/test_inv23_pages.py` is the INV-23 #2 suite (SEC-04). Phase 6 extends it to every row below. Each plan adds the rows for the surfaces it builds; the W3 clean-up plan checks the matrix is complete (§5.6).

Legend: **—** never present; **mask** only `{bot_id}:••••••••` in the named place; **key** only in the named place; every row also checks every `Location` header and runs `assert_no_injected_script`.

| Surface | Full token or its secret part | Token mask | Full device key (current or old) | Key mask or key tail |
|---|---|---|---|---|
| S1 sign-in, incl. wrong credentials and 429 | — | — | — | — |
| S3 list (with sidebar and fleet tiles) | — | — | — | — |
| S4 add: GET, invalid POST with a typed token | — | — | — | — |
| S5 location page, every state and every flash | — | mask in `settings-panel` only | — | — |
| S6 edit: GET, invalid POST (typed second token), after a valid save | — | mask in the token help only | — | — |
| S7 delete: page and fragment | — | — | — | — |
| S8 setup GET (masked) | — | mask in `settings-panel` only | — | key mask in `#device-key` and the examples; key tail only in the visually hidden "Hidden key ending in XXXX" text |
| S8 Reveal POST (`no-store`) | — | mask in `settings-panel` only | key in `#device-key` and the 4 examples only | — |
| S9 regenerate: page and fragment | — | — | — | — (the marker is an HMAC) |
| S9 Regenerate POST (`no-store`) | — | mask in `settings-panel` only | new key in `#device-key` and the 4 examples only; old key nowhere | — |
| S10 remove outage, S11 reset: pages, fragments, and every refusal redirect | — | — | — | — |
| Every action's redirect and the following page's toasts (switches, test message incl. Telegram errors carrying the token, edit, delete, remove, reset) | — | as on the target page | — | — |
| E1 404, E2 403 CSRF, E3 500 | — | — | — | — |
| **Status JSON** | — | — | — | — (and no `•` at all) |
| **Chart PNG** (bytes and PNG text chunks) | — | — | — | — |
| **Modal fragments** (all four, both outcomes) | — | — | — | — |
| **Theme POST response** (redirect) | — | — | — | — |
| **Sidebar** (on every app page) | — | — | — | — |
| Static assets (built CSS, `admin.js`) | — | — | — | — |

Outside the server responses, R4 also forbids the key in any browser storage (sessionStorage, localStorage, IndexedDB, Cache Storage, a service worker, history state). That is covered by the `admin.js` lint (§5.5: the storage bans, the allowlisted `localStorage` key) and by the manual storage and history check in §11.

---

## 10. Coverage gate and build facts

- **pyproject** (`pyproject.toml:40-44`): `branch = true`, `source = ["powermon"]`, migrations omitted. There is no `fail_under` in pyproject.
- **The real gate** is in `README.md:276` and is duplicated as `test_command` in `.planning/config.json:58`:
  - `coverage report --include='powermon/engine/*,powermon/alerts/*,powermon/i18n/*,powermon/telegram/*,powermon/chart/*,powermon/worker/detection.py,powermon/worker/io_loop.py,powermon/worker/lease.py,powermon/worker/supervision.py' --fail-under=80`
  - It runs after `ruff check`, `ruff format --check` and `mypy powermon`.
- **`powermon/web/*` is outside the gate today.** It is near 100% only as a side effect (`05-VERIFICATION.md:152` reports 100% for `history_views.py`).
- **Change in Wave 0 (brief §10):** add the web package to the `--include` list in **both** places, and keep `powermon/web/gunicorn_conf.py` out (it is covered by the start-up tests in a separate process). List subpackages explicitly (`powermon/web/*.py,powermon/web/templatetags/*.py`) and add `--omit='powermon/web/gunicorn_conf.py'`; check in the report output that the expected files are listed, because coverage's glob rules for `*` across directories have changed between versions. New modules (context processors, the status and chart views, the theme view, the icon tag) are then gated automatically.
- **Templates are not measured.** No `django_coverage_plugin` is installed, and Phase 6 does not add it. The render matrix (§7.1) and the route-coverage tests (§5.6) take its place.
- **Build-time dependency:** `CompressedManifestStaticFilesStorage` (`powermon/settings.py:140-143`) makes `{% static %}` raise when a manifest entry is missing. Tests depend on `collectstatic` having run in the image (`Dockerfile:18`). The Tailwind `css` stage must therefore produce `powermon/web/static/web/build/app.css` **before** `collectstatic` (`FRONTEND-STACK.md` §2), or every page-render test fails with "Missing staticfiles manifest entry". The README gate command builds the image, so it always tests with fresh assets.
- **Traceability today:** the VALIDATION maps reference `tests/web` files 34 times (01: 9, 02: 5, 04: 12, 05: 8). 62 web tests carry requirement IDs in their names (INV/K/LOC). Keep those names when editing or moving tests.

---

## 11. Browser checks and manual UAT (brief §12 Q4: no Playwright in Phase 6)

**Why no Playwright now:** browsers add roughly 300–500 MB to an image, Python 3.14 wheel support needs checking, and it would need a separate Docker target. The Django-client suite plus the lint tests in §5.5 cover the server contract and the common CSP mistakes. Revisit Playwright if a JS regression reaches UAT, or if `admin.js` grows beyond the component list in brief §13. If added later: at most 10 tests under a `browser` marker against pytest-django `live_server` (pytest-socket already allows localhost), outside the default gate at first, and never starting or stopping containers.

**UI review with real screenshots.** `gsd-ui-auditor` probes only http://localhost:3000, :5173 and :8080. It needs an HTTP 200 on `/` and screenshots that one URL at 1440, 768 and 375 px. The admin runs on :8000 and `/` redirects to sign-in, so on its own the auditor falls back to a code-only audit. `workflow.ui_interaction_capture` only adds captures after a successful static capture, so it does not help here. The maintainer captures the evidence by hand instead (README "Before you start" item 4): every screen and state in S1–S13 and E1–E3, in light and dark, at 1440 px and 360 px, saved in `.planning/ui-reviews/06-manual/`. The maintainer then asks `/gsd-ui-review 6` to read them (README step 6). Target: ≥ 21/24 with no pillar below 3 (brief §11). This adds no Playwright to the test suite (Q4 stands). Visual regression screenshots in the test suite: skip.

**Manual UAT checklist** (run in Chrome on the local stack with demo data; record results in the Phase 6 UAT):
1. **Console:** zero CSP violations and zero JS errors on every page (S1–S11, E1–E3) in light and dark, with DEBUG off (Django's technical error pages use inline script and style; `FRONTEND-STACK.md` §2).
2. **No third-party requests:** the Network panel shows only same-origin requests on every page.
3. **JS disabled:** sign in, add, edit, every switch, the test message, reveal and hide, regenerate, remove outage, reset and delete all work through the full pages; no dead buttons are visible.
4. **Theme:** switching Light, Dark and System persists across reloads with no flash of the wrong theme; System follows an OS change.
5. **Live refresh (UI-05):** unplug a demo device; an open Locations page and location page show Off within the detection window plus 35 s without a reload; a new location's setup page flips to "first heartbeat received" on its own; hiding the tab stops polling; sign-out in another tab shows "Live updates paused".
6. **Modals (UI-07):** each of the four opens with focus on Keep; Esc and Keep close it and return focus; the destructive button shows a pending state and blocks a double submit. **Opaque redirect:** remove an outage in a second tab, then click Remove on the same row in the first tab; the browser lands on the location page with exactly one "already gone" info toast (not two). Same for reset after a reset in another tab.
7. **Copy (UI-08):** each copy button copies the exact text and announces "Copied"; there is no key copy while masked.
8. **Storage and history (R4):** after reveal and after regenerate, DevTools shows no key in localStorage, sessionStorage, IndexedDB or Cache Storage. In Chrome, Firefox and Safari, none of these shows the full key: reveal → Hide key → Back; reveal → open the location page → Back; reveal → Sign out → Back. A resubmit prompt or the masked page is fine.
9. **Keyboard only:** skip link, sidebar, drawer, account menu, theme switch, example tabs, modals and forms are all usable, with a visible focus ring.
10. **360 px:** no page-level horizontal scroll; a 100-character name wraps at word boundaries; the drawer opens and closes; touch targets are at least 44 px.
11. **Contrast:** AA in both themes for text, pills, toasts and banners (spot-check with the DevTools contrast picker).
12. **Toasts:** a screen reader (VoiceOver) announces a success toast and an error toast; success auto-dismisses after 10 s and pauses on hover or focus; warnings look different from success.
13. **Chart preview (UI-06):** the S5 image matches the channel's pinned chart; on the VPS, `docker stats` during a preview stays within the memory budget (≈ +25 MB transient).

---

## 12. Traceability: starter for the Phase 6 VALIDATION map

| Req | Automated (Django client and static) | Manual (§11) |
|---|---|---|
| UI-01 | render matrix (§7.1), shell hooks (§7.5), migrated page suites | 3, 9, 10 |
| UI-02 | §8.4 | 4 |
| UI-03 | §8.5 | 9, 10 |
| UI-04 | fleet tile counts (§7.5) | screenshots |
| UI-05 | §8.1, live hooks (§7.5) | 5 |
| UI-06 | §8.2, chart card (§7.5) | 13 |
| UI-07 | §8.3, entry points (§7.4) | 6 |
| UI-08 | copy hooks and exact texts (§7.5) | 7 |
| UI-09 | toast levels and regions (§7.5) | 6, 12 |
| UI-10 | switch forms (§7.5) plus the existing switch suite | 3 |
| UI-11 | `<time datetime>` checks (§7.5) | screenshots |
| UI-12 | page invariants (§5.2) | 9, 10, 11, 12 |
| UI-13 | assets, vendor manifest, CSP (§7.2, §7.3), front-end lint (§5.5) | 1, 2 |
| R1–R16 | the surviving suites (§3.2), route coverage (§5.6), the extended INV-23 matrix (§9) | 1, 2, 8 |

---

## 13. Estimate

| Bucket | Functions | Work |
|---|---|---|
| Keep untouched | ~196 | none |
| Keep if roles and copy are kept | ~22 | swap to `messages()` |
| Edit (helper swap or a few lines) | ~75 | mechanical once `pages.py` exists |
| Rewrite or delete | ~58 | delete ~16 (CSS); re-author ~34; ~10 trivial |
| New: existing surfaces and UI requirements | ~85 cases | render matrix (~35), assets (~8), CSP (~6), front-end lint (~8), route coverage (~3), per-requirement checks (~25) |
| New: new server surfaces | ~85 cases | status JSON (~14), chart PNG (~12), fragments (~24, plus the existing pre-check tests run under both variants), theme (~16), sidebar (~9), extended INV-23 rows (~10) |
| Playwright | 0 | not in Phase 6 (Q4) |

About 38% of the existing web test functions (133 of 351) need changes. The real re-authoring work is about 34 functions plus the new suites (about 170 new cases, many of them parametrized); the rest is helper swaps.
