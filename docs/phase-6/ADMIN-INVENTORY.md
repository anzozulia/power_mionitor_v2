# Phase 6 admin inventory: what the from-scratch rebuild must keep working

> **Parity contract.** This is the inventory that `PHASE-6-BRIEF.md` §6 refers to: every route, screen, state, action, flash and copy location listed here must exist in the rebuilt admin (UI-01), in light and dark.
> **Binding:** the security rules **R1–R16** in §4 are binding for Phase 6 (brief §6.3). Where a recommendation in §5–§6 differs from the brief, the brief and its maintainer decisions D6-01…D6-07 win.
> Read from the repo on 2026-10-04 (phases 1–5). Paths are relative to the repo root, with line numbers where they help.

---

## 0. Headline facts

- **Size of the surface.** 15 templates (`powermon/web/templates/`), one 287-line stylesheet (`powermon/web/static/web/app.css`), 1 template filter (`templatetags/display_time.py`) and 3 view modules:
  - `views.py`: 470 lines
  - `location_views.py`: 681 lines
  - `history_views.py`: 181 lines

  It has no JS, no images, no fonts and no icons.
- **Views hold the logic; templates hold most of the prose.**
  - The views compute every state: status vocabulary, switch rows, delivery row, outage rows, regenerate state block.
  - The templates are thin. They hold section prose and button labels.
  - Flashes, form copy and status labels live in Python constants.
  - This means the rebuild can replace templates and CSS wholesale and keep the views and their context contracts.
- **Admin is English-only.**
  - `powermon/settings.py:34-35` sets `USE_I18N = False` ("The admin UI is English only").
  - `.planning/PROJECT.md:292` says the same.
  - `.planning/REQUIREMENTS.md:124` lists "admin UI localisation **or themes**" as out of scope. Themes are now approved (D6-03), so the brief §7 amendment removes "or themes" from that row. Localisation stays out of scope.
- **Auth is default-deny.** `LoginRequiredMiddleware` (`settings.py:61`). Only these are exempt:
  - sign-in (`LoginView`, exempt by default)
  - sign-out (`views.py:135`)
  - `/hb` (`views.py:401`)
  - `/healthz` (`views.py:457`)

  Any new endpoint added in Phase 6 (status JSON, chart PNG, modal fragments, theme POST) is login-protected automatically.
- **Tests are tightly coupled to markup.**
  - `tests/web/` has 334 test functions in 21 files (351 together with the 17 in `tests/test_walking_skeleton.py`, the figure the brief §10 uses).
  - About 269 of them are HTML-substring assertions.
  - Class names (`btn--primary`, `status--on`, `callout`, `.num`, `.name`, …) are asserted in 13 files.
  - 20 assertions use `"<script" not in html` as an XSS check, and they break as soon as any `<script src>` exists (list in §4).
  - `tests/web/test_css.py` (14 tests) tests the old stylesheet itself.
  - Rewriting these tests is a large part of Phase 6.
- **Known UX debt, still unfixed in code.**
  - 04-UI-REVIEW scored 17/24 and 05-UI-REVIEW scored 18/24.
  - Open findings:
    - location names break mid-word on phones (`app.css:265`);
    - tag pills split in two (`app.css:211-218`);
    - warning flashes look like success flashes (`base.html:35`);
    - the edit-form Note callout has a 0px gap above it;
    - the Delivery time is not tabular;
    - flashes are not reliably announced by screen readers;
    - the breadcrumb "/" is read aloud;
    - there are no `#recent-outages` / `#reset-history` anchors for round trips ("Keep outage" and "Keep history" should return to them);
    - the outage table has no accessible name.
  - The rebuild fixes these by design.

---

## 1. Screen inventory

### 1a. Route and view map (`powermon/urls.py`)

`<pk>` below is `<int:pk>` in `urls.py`.

| # | Screen | URL name, path | View (file:line) | Template | Methods → status |
|---|---|---|---|---|---|
| S1 | Sign in | `login`, `/login/` | `SignInView` views.py:61 | `web/login.html` | GET 200 (signed in → 302 `/`); POST 302 on success, 200 on wrong credentials, **429** when throttled |
| S2 | Sign out | `logout`, `/logout/` | `SignOutView` views.py:135 | none | POST → 302 `login` + flash; GET 405 |
| S3 | Location list | `location-list`, `/` | `LocationListView` views.py:177 | `web/location_list.html` | GET |
| S4 | Add location | `location-create`, `/locations/new/` | `LocationCreateView` views.py:241 | `web/location_form.html` | GET; POST → 302 to setup, or 200 with errors |
| S5 | Location page | `location-detail`, `/locations/<pk>/` | `LocationDetailView` location_views.py:501 | `web/location_detail.html` (+ `_settings_panel.html`) | GET |
| S6 | Edit location | `location-edit`, `/locations/<pk>/edit/` | `LocationEditView` location_views.py:543 | `web/location_edit.html` | GET; POST → 302 detail, or 200 with errors |
| S7 | Delete confirmation | `location-delete`, `/locations/<pk>/delete/` | `LocationDeleteView` location_views.py:586 | `web/location_delete.html` | GET (confirmation); POST → 302 list |
| S8 | Device setup (masked / revealed) | `location-setup`, `/locations/<pk>/setup/` | `LocationSetupView` views.py:309, `render_setup` views.py:269 | `web/location_setup.html` (+ `_settings_panel.html`) | GET masked; POST = Reveal, answered 200 directly. Always `no-store` |
| S9 | Regenerate-key confirmation | `location-regenerate`, `/locations/<pk>/setup/regenerate/` | `RegenerateKeyView` location_views.py:640 | `web/location_regenerate.html`; the POST renders S8 revealed | GET; POST → **200** setup page revealed. `never_cache` |
| S10 | Remove-outage confirmation | `outage-remove`, `/locations/<pk>/outages/<int:start_us>/remove/` | `OutageRemoveView` history_views.py:85 | `web/outage_remove.html` | GET (or 302 + flash); POST → 302 detail |
| S11 | Reset-history confirmation | `location-reset`, `/locations/<pk>/reset/` | `HistoryResetView` history_views.py:144 | `web/history_reset.html` | GET (or 302 + flash); POST → 302 detail |
| S12 | Switch POSTs ×3 | `location-maintenance` / `location-alerts` / `location-router-grace`, `/locations/<pk>/{maintenance,alerts,router-grace}/` | `SwitchView` subclasses location_views.py:268-343 | none | POST only; GET 405; bad `value` 400 with empty body |
| S13 | Test message POST | `location-test-message`, `/locations/<pk>/test-message/` | `SendTestMessageView` location_views.py:377 | none | POST only; GET 405 |
| E1 | 404 | any unknown path; any unknown or deleted location | Django default handler | `404.html` | fixed copy; never echoes the path; rendered with the request (`page_not_found`, django/views/defaults.py:64), so every context processor runs |
| E2 | 403 CSRF | any POST with a missing or stale token | Django `csrf_failure` | `403_csrf.html` | rendered with the request (Django 5.2.17 `csrf_failure` calls `t.render(request=request)`, django/views/csrf.py:70), so every context processor runs; the failure reason is never in the context |
| E3 | 500 | any unhandled error | Django default handler | `500.html` (standalone, no `extends`) | no context at all |
| — | No admin HTML | `heartbeat` `/hb`, `healthz` `/healthz` | views.py:401, :457 | none | plain text, device- and ops-facing |
| — | No template | 400 (bad switch value, DisallowedHost), 405, non-CSRF 403 | Django defaults | none | empty or Django default body; nothing links to these |

**New surfaces Phase 6 adds** (not part of the parity list; their rules are in brief §6.3 and §4 below): the status JSON for live refresh (UI-05), the weekly chart PNG (UI-06), the confirmation fragments loaded into the modal (UI-07), and the theme fallback POST (UI-02).

**Shared shell, `base.html`.**
- `<html lang="en">`, viewport meta, `robots noindex,nofollow`, title `{block} · Power Monitor`, one `<link>` to `{% static 'web/app.css' %}`.
- Signed-in header:
  - wordmark link to `/`;
  - nav with one item, "Locations" (`aria-current="page"` only on the list page);
  - "Sign out" POST form with CSRF (`base.html:15-28`).
- Signed-out and error pages override the header with the plain-text wordmark only.
- Order inside `<main>`: `{% block crumbs %}` → flashes → content (`base.html:29-40`).
- Flash markup (`base.html:32-38`): error level gets `callout--error` + `role="alert"`; every other level gets `role="status"`. The warning level is not distinguished; that is a bug. Phase 6 turns flashes into toasts with four distinct tones (UI-09).

### 1b. Per-screen detail

**S1 Sign in, `web/login.html`**
- **Context:** `form` (SignInForm, forms.py:64), `next`, and when throttled: `throttled` + `throttle_message`.
- **Fields:**
  - Username: `autocomplete=username`, `autofocus`.
  - Password: `current-password`, never rendered back.
  - Hidden `next`.
  - Button "Sign in".
- **States:**
  - Normal.
  - Wrong, blank, inactive or NUL credentials: one callout, "Wrong username or password. Check both and try again." (forms.py:14). The username is kept.
  - **Throttled (429):**
    - Only the throttle callout shows: "Too many failed sign-ins. Try again in 5 minutes." (`powermon/throttle/rules.py:30`).
    - `Retry-After: 300`.
    - The form is unbound and the credentials are never checked (views.py:94-111).
    - The trigger is 5 failures in 60 s per client IP, giving a 5-minute cool-down (rules.py:19-21). Throttled POSTs are not recorded; a GET stays 200.
  - `next` is honoured only when same-host.
- **Flashes landing here:** "You are signed out." (info, views.py:52).

**S3 Location list, `web/location_list.html`**
- **Context:**
  - `rows`: a list of `LocationRow` (views.py:147): pk, name, status, status_label, last_heartbeat_at, alerts_off, router_grace, delivery.
  - `ops_configured`.
- **Rows:** non-deleted locations, sorted case-insensitively by name, then pk. No pagination (at most about 20).
- **Columns:** Name (links to the detail page) · Status · Last heartbeat · Delivery.
  - Status: a status dot and label, then the tags "Alerts off" and "Router grace", in that order.
  - Last heartbeat: `display_time`, or "Never".
  - Delivery: "OK", or "Failing since {HH:MM | YYYY-MM-DD HH:MM} (http_NNN)". The date is added when the incident did not start today (status.py:60-74).
- **States:**
  - Empty: h1, plus a panel "No locations yet" / "Add a location to get its heartbeat URL, device key and setup examples." / "Add location".
  - Populated: h1 row with the "Add location" primary button, then the table.
  - **Ops-chat banner** above either state while `OPS_BOT_TOKEN`/`OPS_CHAT_ID` are unset (location_list.html:17): "Warning: The ops chat is not configured. Set OPS_BOT_TOKEN and OPS_CHAT_ID in the env file and run the deploy command; until then, ops notices (…) go to the worker log only."
  - Today there is no live refresh, and `tests/web/test_templates.py:229` (`test_list_has_no_live_refresh`) pins that. UI-05 reverses this, so that test is rewritten in the plan that adds polling.
  - A database error renders the 500 page with no partial table.
- **Phase 6 additions:** fleet summary tiles (UI-04) above the table; the same rows also feed the sidebar location list (UI-03).
- **Flashes landing here:**
  - "Location deleted. Its alerts have stopped. …" (success)
  - "This location was already deleted." (info)

**S4 Add location, `web/location_form.html`**
- **Context:** `form` (LocationForm, forms.py:129).
- **Fields, in order:**
  - Name: max 100, autofocus.
  - Heartbeat period (seconds): initial 60, 10–3600, step 1.
  - Grace period (seconds): initial 30.
  - Bot token: password input, `render_value=False`, `autocomplete=off`, `spellcheck=false`.
  - Channel chat ID: text input, no assist.
  - Language: select uk/en/ru, initial uk.
  - Each field has help text (forms.py:30-48).
  - `novalidate`: every message comes from the server.
- **Buttons:** "Create location" (primary) and "Back to locations" (link).
- **States on an invalid submit:**
  - Form-level callout: "The location was not saved. Fix the fields marked below."
  - Per-field errors.
  - Token input empty again, with the note "Paste the token again: it is never sent back to the browser."
  - `aria-describedby` wiring: `<id>_error`, `<id>_note`, `<id>_helptext` (forms.py:115-126).
- **On success:** 302 to S8 with the flash "Location created. Reveal the key below, then copy an example to the device." (views.py:53).
- **Double submit:** creates a duplicate location. This is accepted behaviour (UI-09's pending button state makes it less likely).

**S5 Location page, `web/location_detail.html`**
- **Context:**
  - `location`
  - `status`: `LocationStatus`, status.py:30
  - `delivery`: `DeliveryRow`, location_views.py:416
  - `switch_rows`
  - `outage_rows`: `OutageRow`, location_views.py:460
  - `has_history`, `outage_in_progress`
  - settings-panel values (location_views.py:208)
- **Breadcrumbs:** Locations / {name}. The h1 is the name.
- **Sections, in this order** (test-pinned): Status · Switches · Test message · Recent outages · Settings · Device setup · Reset history · Delete location. The brief §9 card grid keeps this DOM order.

1. **Status panel** (`dl`):
   - Status.
   - **Power state** row, only while maintenance is on. It has help text:
     - when power is on: "OFF is not detected during maintenance.";
     - when power is off: "The outage goes on. When power returns, the ON alert is sent as usual.".
   - On since (power on) or Outage since (power off); neither while waiting.
   - Last heartbeat.
   - Delivery:
     - OK: "OK" plus a help line;
     - Failing: "Failing since {full display_time} (http_NNN)" plus a cause line and the retry line.
     - Cause lines exist for 400, 401/404, 403 and any other code; a supergroup-migrate line with the new chat ID replaces the cause line when Telegram reported one (location_views.py:119-148, 427-445).
2. **Switches:** three rows (Maintenance, Alerts, Router grace).
   - h3 = the current state ("Maintenance is off").
   - A fixed help text.
   - A POST form with hidden `value` = the target state, and a button that names the action ("Turn maintenance on") (location_views.py:222-265).
   - Phase 6 renders these as toggle switches (UI-10) with the same POST of the target value.
3. **Test message:** a paragraph, then a POST form with the page's only primary button, "Send test message".
4. **Recent outages:** the last 14 local days, newest first. Exactly one of:
   - a table: Outage (`start – end` / `in progress`; the end date is shown only when it is a different day) · Off time ("1h 30m", "<1m") · a visually hidden "Action" column holding a "Remove" link with a hidden suffix " the outage from {start}". The in-progress row has no link.
     - Always followed by the off-time note.
     - Followed by the in-progress note only when the current outage is listed.
   - "No outages in the last 14 days."
   - "No power history yet. Outages are listed here once the device has sent its first heartbeat."
5. **Settings:** the `_settings_panel.html` include, then the "Edit location" link-button.
   - Panel rows: Language · Heartbeat period "{P} s" · Grace period · Reported OFF after "{P+G} s without a heartbeat", extended with "(… {P+G+180} s right after power returns, router grace on)" · Channel chat ID · Bot token **masked** `{bot_id}:••••••••`.
6. **Device setup:** a sentence and the "Open device setup" link-button.
7. **Reset history:** the description, then exactly one of:
   - the in-progress refusal line (when power is off);
   - "There is no power history to reset.";
   - the "Reset history" link-button.
8. **Delete location:** a sentence and the "Delete location" link-button (opens S7).

- **Phase 6 additions:** the weekly chart card (UI-06), live status (UI-05), relative times next to absolute ones (UI-11); "Remove", "Reset history" and "Delete location" open the confirmation modal (UI-07) and stay plain links to S10, S11 and S7.
- **Never on this page:** the device key, not even masked.
- **Flashes landing here:** switches (12), test message (8), edit (2), remove (4), reset (3), plus the GET-refusal redirects from S10 and S11. Full list in §2.

**S6 Edit location, `web/location_edit.html`**
- **Context:** `location` (the stored values feed the title, crumbs and h1) and `form` (LocationEditForm, forms.py:203).
- **Fields:** the S4 fields with stored initial values, except:
  - "New bot token": optional, always empty;
  - its help shows the current token masked via `format_html`;
  - empty keeps the current token.
- **Note callout** before the buttons: thresholds, chart move on a chat or token change, and where the switches live (location_edit.html:44).
- **Buttons:** "Save changes" (primary) and "Discard changes" (link to S5).
- **Invalid submit:**
  - "The changes were not saved. Fix the fields marked below."
  - The token re-paste note shows **only if a non-empty token was submitted** (forms.py:236-240).
- **Valid submit:** `actions.update_config`, which writes the configuration columns only (stale-form safe, INV-02 #3). Then 302 to S5 with:
  - "Changes saved."; or
  - "Changes saved. The weekly chart is posted again with the new bot or chat. …" when the chat ID or token changed.
- A location deleted mid-save answers 404.

**S7 Delete confirmation, `web/location_delete.html`**
- Crumbs: Locations / {name} / Delete. h1: "Delete {name}?".
- Body:
  - "This cannot be undone. Deleting this location:";
  - 5 consequences;
  - the pause alternative (maintenance on or alerts off);
  - the re-create paragraph.
- **One form:** a POST with the danger button "Delete location", next to the link "Keep location". No autofocus. No secrets on the page.

**S8 Device setup, `web/location_setup.html`** (`render_setup`, views.py:269)
- **Context:**
  - `location`, `status`, `language_label`
  - `revealed`, `shown_key`, `key_tail`
  - `heartbeat_url`: from `PUBLIC_BASE_URL`, never from the Host header (test `test_setup_page.py:270`)
  - `curl_example`, `cron_example`, `wget_gnu_example`, `wget_busybox_example`: generated by `powermon/locations/examples.py` and run verbatim by `tests/web/test_examples_verbatim.py`
  - `period_s`, `grace_s`, `off_after_s`, `masked_token`
- Crumbs: Locations / {name} / Device setup. h1: the name. Meta line: status + Last heartbeat.
- **Sections:**
  - Before you start: 3 paragraphs (UPS warning, power + internet, timing).
  - Heartbeat URL: code block.
  - Device key.
  - Examples: curl (header), Cron, GNU wget, BusyBox/uclient-fetch; each a `pre.copy` block with captions.
  - Link-previewer **Warning** callout.
  - A responses paragraph.
  - Location settings: the panel, then "Edit location".
- **Masked state (GET):**
  - Key shown as `••••••••••••XXXX` (12 bullets + last 4; `keys.py:14-31`), `aria-hidden`.
  - Visually hidden text "Hidden key ending in XXXX".
  - The "Reveal key" POST form (the primary button).
  - The examples contain the **masked** key, with the note "Reveal the key above to fill it into these examples."
  - Phase 6: no copy button for the key while it is masked (UI-08).
- **Revealed state** (Reveal POST, or the Regenerate POST):
  - Full key in a `pre.copy`.
  - "Hide key" link (a GET of the same URL).
  - "The key is hidden again the next time you open this page."
  - Examples filled with the real key.
- **Both states:** "If the key has leaked, regenerate it. …" and the "Regenerate key" link-button (opens S9).
- **Every response:** `Cache-Control: no-store` (views.py:305, :309).
- **Phase 6 additions:** copy buttons for the URL, the revealed key and each example (UI-08); a live "waiting for first heartbeat" → "first heartbeat received" state (UI-05).
- **Kept by 01-UAT #9:** the curl example labelled "recommended", the `?key=` examples labelled, and the link-previewer warning.
- **Flashes landing here:**
  - "Location created. …" (success, after a redirect from S4);
  - "New key saved. The old key no longer works. …" (success);
  - "The key was already regenerated. The key below is the current one." (info). Both regenerate flashes render in the same 200 response.

**S9 Regenerate confirmation, `web/location_regenerate.html`**
- **Context:** `location`, `state_block`, `off_after_s`, `marker` (HMAC-SHA256 of the current key, salted, location_views.py:618-625).
- Crumbs: Locations / {name} / Device setup / Regenerate key. h1: "Regenerate the device key?".
- Lead: the old key stops at once; the device gets 401 until updated; the history is kept.
- **Exactly one state block** (location_views.py:628-637, location_regenerate.html:25-34):

  | State | Block shown |
  |---|---|
  | Maintenance off, power on | **Warning** callout (an OFF alert can fire after {P+G} seconds), then the link "Open the location page" |
  | Power off | Note: the return of power is not seen until the device has the new key |
  | Waiting | Note: no heartbeat yet, so nothing is reported |
  | Maintenance on | Note: OFF is not detected; turn maintenance off afterwards |

- **One form:** hidden `marker` + the danger button "Regenerate key", next to the link "Keep current key" (to S8). No key on this page, not even masked.

**S10 Remove-outage confirmation, `web/outage_remove.html`**
- **Context:** `location`, `start_us` (taken from the stored outage, never from the request), `outage`, `off_text`, `shows_unmonitored`.
- **GET checks:**
  - an unknown or deleted location, or an invalid instant → 404;
  - outage gone → 302 to S5 with an info flash;
  - outage in progress → 302 to S5 with an error flash (history_views.py:103-123).
- Crumbs: Locations / {name} / Remove outage. h1: "Remove this outage?".
- **Details panel:** Start, End (full `display_time`), Off time.
- Lead, then consequences 1–4. Consequence 2 ("keeps the time … not monitored") shows only when `off_us < span`.
- Chart sentence: "The chart updates within 15 minutes. …".
- **One form:** the danger button "Remove outage", next to the link "Keep outage".

**S11 Reset-history confirmation, `web/history_reset.html`**
- **GET checks:**
  - power off → 302 to S5 with the error "An outage is in progress. Reset the history after power returns, or delete the location.";
  - no history → 302 to S5 with the info "There is no power history to reset. Nothing changed." (history_views.py:159-167).
- Crumbs: Locations / {name} / Reset history. h1: "Reset the history of {name}?".
- Body: lead, 5 consequences, and the alternative ("To remove a single false outage instead, use Recent outages …").
- **One form:** the danger button "Reset history", next to the link "Keep history".

**S7, S9, S10, S11 in Phase 6.** Each becomes the body of the confirmation modal (UI-07, D6-05): the modal loads the same server GET, so the pre-checks, refusal redirects, state block and HMAC marker are unchanged. The full pages stay as the no-JS and deep-link fallback.

**E1, E2, E3 error pages**
- Wordmark-only header, h1, one paragraph, and the link "Back to locations" (`/`).
- Fixed copy:
  - 404: "Page not found" / "This page does not exist. Check the address, or go back to the location list."
  - 403: "Form expired" / "This form was open too long, or the browser blocked cookies. Go back, reload the page and try again." The word "CSRF" never appears.
  - 500: "Something went wrong" / "The server could not finish this request. Details are in the web container logs. Try again in a moment."
- `500.html` is rendered with no context and no request (`server_error` → `template.render()`; see also 500.html:11-14). `404.html` (`page_not_found` → `template.render(context, request)`, whose context also holds `request_path` and `exception`) and `403_csrf.html` are rendered WITH the request. So every context processor runs for them, for anonymous and signed-in visitors alike. Phase 6 therefore gives E1–E3 one error layout that reads no context variable at all: no `request`, `user`, `messages`, `request_path`, `exception`, theme or sidebar value. It renders byte-identical with or without a request. The sign-in page uses a separate auth layout (theme cookie and toasts, no sidebar). Do not add `400.html` or `403.html`.

---

## 2. Every user action (POST endpoints)

All POSTs carry CSRF. All location URLs answer 404 for an unknown or deleted location, except a delete POST for an already-deleted location (UI-D8). No action redirects to a value taken from the request.

| Action | Endpoint and form | Effect | Confirmation | Result and flashes (level) | Double submit or stale page |
|---|---|---|---|---|---|
| Sign in | POST `/login/` (username, password, next) | auth; a success clears the IP's failures | — | 302 to `next` (same host) or `/`. Error callout. 429 + throttle callout | 6th+ POST during the cool-down: 429 even with the right password |
| Sign out | POST `/logout/` (header button) | session flushed | — | 302 `/login/` + "You are signed out." (info) | a stale tab gets the 403 Form expired page |
| Create location | POST `/locations/new/` | location + waiting state in one transaction; key generated; **no Telegram I/O** | — | 302 to S8 + "Location created. Reveal the key below, then copy an example to the device." (success) | creates a duplicate (accepted) |
| Reveal key | POST `/locations/<pk>/setup/` (CSRF only) | none; renders the full key | — (non-destructive) | **200 directly**, `no-store` | a reload resubmits (harmless) |
| Maintenance on/off | POST `/locations/<pk>/maintenance/` `value=on\|off` | `maintenance.set_maintenance` (engine writes the timeline) | none, one click | 302 to S5. On: "Maintenance is on. OFF is not detected and no OFF alert is sent; the chart shows this time as not monitored." Off: "Maintenance is off. OFF detection starts again now; silence during maintenance does not count." (success). Already: "Maintenance was already on/off. Nothing changed." (info) | posts the target value, never "toggle", so a repeat is idempotent (UI-D3) |
| Alerts on/off | POST `/locations/<pk>/alerts/` `value=…` | `actions.set_flag(alerts_enabled)` | none | On: "Alerts are on. Subscribers get alerts for changes recorded from now on." Off: "Alerts are off. Subscribers get no new alerts; alerts already queued still go out. The chart keeps updating." Already: "Alerts were already …" (info) | idempotent |
| Router grace on/off | POST `/locations/<pk>/router-grace/` `value=…` | `actions.set_flag(router_grace)` | none | On: "Router grace is on. From now on, OFF waits 180 seconds longer right after power returns." Off: "Router grace is off. From now on, OFF is reported after {P+G} seconds without a heartbeat." Already: "… Nothing changed." (info) | idempotent |
| Send test message | POST `/locations/<pk>/test-message/` | **The only admin action with network I/O**: one silent `sendMessage`, no retry, 5 s/10 s timeouts (up to about 15 s wait). An `ok` records the success and closes an open failing incident | none | 302 to S5 with 1 of 8 flashes (location_views.py:346-374): success "Test message sent. Check that it arrived in the channel." or "… Delivery is marked OK again, and any queued alerts go out next."; **warning** maybe-delivered "No answer from Telegram in time ({code}). …" or rate-limited "Telegram asks to wait before the next message. Try again in {N} seconds."; error not-in-chat (400/403), bad token (401/404), refused (other), unreachable (`not_sent`/5xx) | a double click sends 2 messages (accepted). Today there is no spinner, only the browser's own loading indicator; UI-09 adds a pending state that blocks double submits |
| Save edit | POST `/locations/<pk>/edit/` (6 fields) | `actions.update_config`: configuration columns only; no I/O | — | 302 to S5 + "Changes saved." or the channel-changed variant (success). Invalid: 200 + errors | a stale form never reverts the status, switches or key |
| Delete location | GET `/locations/<pk>/delete/`, then POST the same URL | `actions.delete_location`: soft delete in one transaction; the worker unpins later | **GET page S7** (modal in Phase 6) | 302 to the **list** + "Location deleted. Its alerts have stopped. Its weekly chart is unpinned when the bot can do so; if the pin stays, unpin it by hand in Telegram." (success), or "This location was already deleted." (info) | already deleted → info (UI-D8); never-existed id → 404 |
| Regenerate key | GET `/setup/regenerate/`, then POST with `marker` | rotates only if the marker matches the current key (`constant_time_compare`) **and** the conditional UPDATE matches | **GET page S9** (modal in Phase 6) | **200 S8 revealed**, `no-store`: "New key saved. The old key no longer works. Copy the new key or an example below to the device." (success), or "The key was already regenerated. The key below is the current one." (info) | a resubmit, stale page or race never rotates twice (UI-D7) |
| Remove outage | GET `/locations/<pk>/outages/<start_us>/remove/`, then POST | `history.remove_outage` under the row lock: off pieces become on; queued alerts dropped per D-04; no I/O | **GET page S10** (modal in Phase 6); the GET pre-checks with the POST's flash | 302 to S5. Success: "Outage from {YYYY-MM-DD HH:MM} removed: its time now counts as power on. No message was sent. If it is within the last 7 days, the pinned chart shows the change within 15 minutes." Error: "This outage is still in progress. It can be removed after power returns." Info: "This outage is no longer in the history: it was already removed, or the history was reset. Nothing changed." Info, POST only: "An alert about this outage is being sent to the channel right now. Nothing changed. Try again in a minute." (history_views.py:45-59) | second click → "already gone" (info) |
| Reset history | GET `/locations/<pk>/reset/`, then POST | `history.reset_history` under the row lock: deletes the intervals, back to waiting, chart unpinned by the worker | **GET page S11** (modal in Phase 6); GET pre-checks | 302 to S5. Success: "History reset. The location waits for its next heartbeat, which restarts monitoring without an alert. The old weekly chart is unpinned when the bot can do so; if the pin stays, unpin it by hand in Telegram." Error: "An outage is in progress. Reset the history after power returns, or delete the location." Info: "There is no power history to reset. Nothing changed." (history_views.py:61-69) | second click → "nothing to reset" (info) |

**Navigation-only links (GET), which the rebuild must keep reachable:**
- "Add location", "Back to locations"
- the name link in the list
- "Edit location" (from S5 and S8), "Open device setup", "Hide key"
- the "Delete location", "Regenerate key", "Reset history" and "Remove" entry points
- "Keep location / current key / outage / history", "Discard changes"
- "Open the location page"
- breadcrumbs, the wordmark and nav "Locations"

---

## 3. Where the copy lives

**Python constants**, each a single source that tests import or compare verbatim:

| Copy | Location |
|---|---|
| Sign-out and create flashes | `powermon/web/views.py:52-55` |
| Regenerate, edit, delete, test-message flashes (8); delivery OK help, 4 cause lines, migrate line, retry line; switch help texts and 12 switch flashes | `powermon/web/location_views.py:64-198` |
| Remove and reset flashes (7) | `powermon/web/history_views.py:43-69` |
| Sign-in error, form-level errors, field labels, help texts, field errors, re-paste note, "New bot token" help | `powermon/web/forms.py:13-58` |
| Token and chat-ID validation errors | `powermon/locations/validators.py:30-41` |
| Status labels ("On" / "Off" / "Waiting for first heartbeat" / "Maintenance"), "Failing since" time format | `powermon/web/status.py:18-23`, `:60-74` |
| Throttle message | `powermon/throttle/rules.py:30` |
| "Never" | `powermon/web/templatetags/display_time.py:17` |
| Language labels | `powermon/locations/models.py:15` |
| Setup example commands | `powermon/locations/examples.py` |
| Off-time format ("1h 30m", "<1m") | `powermon/i18n/duration.format_total_duration(…, "en")` |
| Token and key masks | `validators.mask_token` (validators.py:71), `keys.mask_key` (keys.py:23) |

**Template literals** (inline in the HTML):
- h1, h2 and h3 headings, `<title>`s, breadcrumbs, button labels;
- the ops-chat banner (`location_list.html:17`);
- the empty state;
- the test-message paragraph (`location_detail.html:82`), the Recent-outages intro and off-time note (`:89`, `:111-112`), the reset and delete descriptions (`:128-139`);
- the setup page's prose, warnings and notes (`location_setup.html:25-80`);
- the edit Note callout (`location_edit.html:44`);
- the regenerate state blocks (`location_regenerate.html:24-34`);
- the delete, reset and remove consequence lists;
- the copy on the three error pages.

Template prose may be restructured (for example moved into a disclosure or shortened) as long as its meaning is kept, especially warnings and consequences (brief §6.2). Python-owned copy is kept, except for the amendments below.

**Copy decisions to keep through the redesign:**

| Decision | Detail |
|---|---|
| Status vocabulary | Maintenance overrides the stored status as the label, and the stored status stays visible as "Power state". A location with no state row counts as waiting. |
| Delivery | "OK", or "Failing since {time} (http_NNN)". The list uses `HH:MM`, adding the date when the incident did not start today; the detail page uses the full timestamp. |
| Time formats | Full: `YYYY-MM-DD HH:MM:SS TZ`, with the zone abbreviation via `%Z` so the DST fall-back hour stays unambiguous. **Never Django's `date` filter** (display_time.py:1-6). Compact table form: `YYYY-MM-DD HH:MM`. All in the display TZ (`settings.TIME_ZONE` = `DISPLAY_TZ`). Relative times (UI-11) are added next to these, never instead of them. |
| Units | "seconds" spelled out everywhere except the settings panel and the computed setup sentences ("{P} s"). |
| Switches | The heading states the current state; the button names the action. |
| Idempotent no-ops | Always end "Nothing changed." |
| Flash content | Never user-typed text, never Telegram's description text, never a token or key. Allowed: integers, short codes (`http_403`, `read_timeout`) and times formatted from stored instants. |
| Tests pin the copy | 149 Phase 4 strings and 54 Phase 5 strings are pinned word for word (04-UI-REVIEW Pillar 1, 05-UI-REVIEW Pillar 1). |

**Pending copy amendments** (proposed by the UI-REVIEWs, none applied yet; brief §6.2 says to apply them in Phase 6 and update the tests that pin the old strings):

| # | Where | Today | Amendment | Source |
|---|---|---|---|---|
| A1 | Test-message flash for `transient` (HTTP 5xx), `location_views.py:108-111`, mapped at `:364-365` | "Telegram could not be reached ({code}), so the test message was not sent. Try again in a minute." (shared with `not_sent`) | Split `transient` from `not_sent`: "Telegram had a server error ({code}), so the test message was not sent. Try again in a minute." Keep the current text for `not_sent`. | 04-UI-REVIEW Pillar 1 |
| A2 | Off-time note, `location_detail.html:111` | 3 sentences, about 75 words, under every table | "Off time counts only time recorded as power off, as the chart's daily totals do. Time that was not monitored is left out, so off time and the end shown can differ from the alerts." | 05-UI-REVIEW Fix 3 |
| A3 | Remove-outage Consequence 3, `outage_remove.html:40` | opens "sends no new message to the channel", then says a queued ON alert is still sent | "sends nothing itself, and drops its queued OFF and ON alerts if the OFF alert was never sent (if it already went out, its queued ON alert is still sent, so the channel is not left at power off);" | 05-UI-REVIEW Fix 3 |
| A4 | Removal-deferred flash, `history_views.py:56-59` | "An alert about this outage is being sent to the channel right now. Nothing changed. Try again in a minute." | Open with the outcome: "Not removed: an alert about this outage is being sent to the channel right now. Try again in a minute." | 05-UI-REVIEW Pillar 1 |
| A5 | Warning-level flashes | look like success | Distinct warning tone (UI-09). The 04-UI-REVIEW fix was a leading "Warning:"; a toast tone with an icon and label satisfies it. | 04-UI-REVIEW Fix 3 |
| A6 (minor) | Edit Note callout, `location_edit.html:44` | 5 sentences on 4 topics after the fields | Optionally move the two channel-change sentences into the Channel chat ID and New bot token help. | 04-UI-REVIEW Pillar 1 |
| A7 (minor) | Remove-outage success flash, `history_views.py:45-48` | "No message was sent." and a hedged "If it is within the last 7 days…" | Optionally "The removal sent no message.", and two fixed variants instead of the hedge. | 05-UI-REVIEW Pillar 1 |

The throttle copy ("Try again in 5 minutes." on every 429) is locked by D-16 and is not amended.

---

## 4. Security-bound UI rules to keep, with the tests that pin them

These rules are **binding** for Phase 6 (brief §6.3). Source IDs refer to the security rules in each UI-SPEC: P1 = 01-UI-SPEC.md, P4 = 04-UI-SPEC.md, P5 = 05-UI-SPEC.md. Test paths are under `tests/web/` unless shown otherwise.

| # | Rule | Source | Pinned by | Phase 6 extension (brief §6.3) |
|---|---|---|---|---|
| R1 | Auto-escaping everywhere: no `\|safe`, `mark_safe` or `autoescape off`. An XSS name renders as text in cells, crumbs, h1s and `<title>`s | P1 rule 1, P4 rule 1, P5 rule 1 | `test_security.py:289`; `test_templates.py:328`; `test_location_page.py:403`; `test_edit.py:877`; `test_delete.py:533`; `test_history_pages.py:886, 1267`; `test_regenerate.py:409` | Also the sidebar list, toasts, modal fragments and the status JSON (render with `textContent`, never `innerHTML`, for user data); never inside Alpine directive values |
| R2 | Every state change or secret reveal is a POST with CSRF; a GET never writes. Switches and test message answer GET with 405 | P1 r2, P4 r2, P5 r2 | `test_switches.py:224, 252`; `test_test_message.py:380, 403`; `test_edit.py:404`; `test_delete.py:518`; `test_regenerate.py:468`; `test_setup_page.py:320`; `test_history_pages.py:857, 1238` | Toggles (UI-10) stay POST forms; the theme fallback is a CSRF POST |
| R3 | The bot token never enters HTML: not as an input value (`render_value=False`), not in errors, not in flashes; only as `{bot_id}:••••••••` | P1 r3, P4 r3, SEC-04, INV-23 #2 | `test_inv23_pages.py:184` (every page and flash); `test_locations.py:609`; `test_edit.py:460, 514`; `test_setup_page.py:289` | No "show" eye on the token field |
| R4 | The full device key appears **only** in the Reveal POST and Regenerate POST responses, both `Cache-Control: no-store`. Never on S5, S6, S7, S9, S10, S11 or in flashes. The regenerate marker is an HMAC, not key characters | P1 r4, P4 r4, UI-D7 | `test_setup_page.py:126, 154`; `test_inv23_pages.py:259, 338`; `test_regenerate.py:230, 253, 295, 390` | Also never in fragments, JSON, the sidebar or the live endpoint, and never in any browser storage (`sessionStorage`, `localStorage`, IndexedDB, Cache Storage, a service worker, history state). Reveal and Regenerate stay top-level navigations, never a `fetch` |
| R5 | No third-party runtime assets; CSP on every response except exact `/hb` | P1 r5, P4 r8, P5 r7, v1-L §4 | `tests/test_walking_skeleton.py:85-102`; `test_security.py:182, 274`; `test_heartbeat.py:470` | The header equals the brief §8 policy exactly (see §5) |
| R6 | Framework headers: nosniff, `Referrer-Policy: same-origin`, `X-Frame-Options: DENY`, Secure + HttpOnly + SameSite=Lax session cookie, Secure CSRF cookie, HSTS 1 year (production) | P1 r6, SEC-02, INV-22 #3 | `test_security.py:118, 142, 153, 162, 182, 191-203` | Unchanged; the theme cookie is a UI preference, not HttpOnly, and holds only `light`/`dark`/`system` |
| R7 | Confirmation step for destructive actions (delete, regenerate, reset, remove): a GET page, then a POST. Entry points never act directly | v1-L §2, P4 D-17 and UI-D5, P5 UI5-D7 | `test_delete.py`, `test_regenerate.py`, `test_history_pages.py:662, 1099` | The modal loads the server GET (UI-07); a client-side `confirm()` is never acceptable |
| R8 | No open redirect: action redirects go to fixed routes; sign-in `next` is same-host only; no form carries a return field | P4 r5, P5 r4, v1-L §4 | `test_auth.py:261, 275`; `test_throttle.py:290` | The theme fallback POST redirects to the fixed `/`, or, if the plan supports it, to the path of a same-host Referer that is a GET page other than the four confirmation routes (brief §6.3 R8) |
| R9 | The throttled sign-in (429) never checks credentials and shows only the throttle message | P4 r6, UI-D13, INV-21 #2 | `test_throttle.py:66, 91` | Restyle freely |
| R10 | Short error causes only: fixed copy plus a short code; no exception text or Telegram text | P4 r7, OPS-08, v1-L §4 | `test_test_message.py`, `test_form_null_chars.py:181, 191`, `test_locations.py:368` | Also toasts and the status JSON |
| R11 | Error pages show fixed copy and never echo the path or the CSRF failure reason; 500 renders without context | P1 Error pages | `test_security.py:213, 228, 246, 256` | Error layout: no sidebar, account menu, toasts or theme cookie; it reads no context variable (404 and 403-CSRF run every context processor, 500 runs none) |
| R12 | The `start_us` path value is parsed, never echoed; an out-of-range value answers 404, not 500 | P5 r5 | `test_history_pages.py:803` | Also in the modal fragment |
| R13 | The heartbeat URL comes from `PUBLIC_BASE_URL`, never from the Host header | P1 setup page | `test_setup_page.py:270` | Also the copy button's value |
| R14 | Every admin URL requires login (default deny) | LOC-01 | `test_templates.py:294`; `test_location_page.py:526`; `test_setup_page.py:333`; `test_locations.py:742`; `test_delete.py:505`; `test_regenerate.py:454`; `test_history_pages.py:870, 1251`; `test_edit.py:404`; `test_test_message.py:403` | Covers the new endpoints automatically; add one anonymous test per new endpoint |
| R15 | `<meta name="robots" content="noindex, nofollow">` (the admin lives on a public domain) | P1 Page Shell | `test_security.py:274` (assertion at `:286`) | Every layout, including the bare one |
| R16 | A stale edit form never reverts status, switches or the key (configuration-only write) | D-07, INV-02 #3 | `test_edit.py:278, 837` | Unchanged |

**New surfaces** (status JSON, chart PNG, modal fragments, theme POST): login-required; GET-only for reads; `never_cache`, or a short `private` cache for the PNG; **no secrets** (no key or token, not even masked); added to the INV-23 secret-scan matrix. The fragment header `X-PM-Fragment` is honoured only by the S7, S9, S10 and S11 GET views (brief §6.3).

**Assertions that break on any self-hosted `<script src>` and need rewriting.** Each of these 20 assertions does `"<script" not in …`. Rewrite them to assert that the injected payload (`<script>alert(1)</script>`) is absent and that no inline script body exists:
- `test_security.py:277`
- `test_auth.py:190`
- `test_throttle.py:283`
- `test_templates.py:233` (inside `test_list_has_no_live_refresh`, :229, which UI-05 retires), `:341`
- `test_delivery_display.py:216, 232`
- `test_location_page.py:420, 505`
- `test_edit.py:889`
- `test_delete.py:544`
- `test_regenerate.py:205, 383`
- `test_history_pages.py:420, 629, 901, 1039, 1091, 1284`
- `test_inv23_pages.py:139`

**Related policy tests:**
- `test_security.py:274-286` also asserts exactly one `<link>` (the hashed `app.css`) and no `http(s)://`. Rewrite it as "every asset is a hashed same-origin manifest path, and no third-party origin appears".
- `test_security.py:289-309` (template lint) forbids `<script`, `<style`, `style=`, `on*=`, `|safe`, `autoescape off` and URLs to other hosts in templates, and `mark_safe` in `powermon/web/**/*.py`. Keep the `|safe`, `autoescape off`, `mark_safe`, `<style`, `style=` and `on*=` bans (one exception: the `{% icon %}` tag module may `mark_safe` the repo's own SVG files, read by allowlisted name; `TEST-STRATEGY.md` §3.5); allow `<script src="{% static … %}">` but not an inline script body; allow the SVG namespace `http://www.w3.org/2000/svg`, which the "URL to another host" pattern would otherwise match in inline icons.
- `test_location_page.py:469` (one accent button at most) and `tests/web/test_css.py` are dropped with the old design rules (§5).

---

## 5. Security-essential vs stylistic constraints, and what to do with each

### Security-essential: keep, adapting the mechanism where needed

| Constraint today | Why it is essential | Phase 6 rule |
|---|---|---|
| No third-party runtime assets (no CDN, no Google Fonts, no remote icons or analytics) | v1-L §4; v1 loaded the Tailwind Play CDN without SRI on pages showing secrets | **Keep.** Vendor everything (Tailwind output, JS, fonts, icons) into `static/`, each file pinned with version, URL, sha256 and licence in a vendor manifest (brief §8). TailAdmin free 2.4.0 loads Outfit from Google Fonts (`@import url("https://fonts.googleapis.com/…")` at `src/css/style.css:1`) and depends on ApexCharts, flatpickr, dropzone, FullCalendar, jsvectormap, Swiper, i18next and `@alpinejs/persist` (`package.json`). Drop all of it. Outfit has no Cyrillic anyway; the font is self-hosted Inter Variable. |
| CSP header on every response except `/hb` | Turns the lesson above into an enforced guarantee | **Keep the middleware and set the policy to exactly the brief §8 string:** `default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'`. `data:` in `img-src` is there only because `@tailwindcss/forms` draws the select chevron and the checkbox and radio marks as `data:image/svg+xml` backgrounds (`@tailwindcss/forms@0.5.11/src/index.js:165,261,274,305`); data-URI images cannot run script. `connect-src 'self'` is only for UI-05 polling and the modal fragments. No `unsafe-inline`, no `unsafe-eval`, no nonces. |
| No inline `<script>`, no `on*=` handlers, no `javascript:` URLs (no `'unsafe-inline'` or `'unsafe-eval'` in `script-src`) | Defence in depth against XSS on pages that reveal device keys | **Keep.** JS lives in hashed static files: `@alpinejs/csp` 3.17.4 plus one first-party `admin.js`, wired by `data-*` and `x-data="name"` attributes. The standard Alpine build needs `'unsafe-eval'`; the 3.17.4 CSP build contains no `eval(` or `new Function`. Its expression subset rejects TailAdmin's inline Alpine (`window.innerWidth`, `localStorage`, `JSON.parse`, arrow functions in `$watch`, `$t()`), so all logic lives in `Alpine.data()` in `admin.js`. Pass data only via `data-*` attributes (no `json_script`: it emits a `<script>` element with a body, which `TEST-STRATEGY.md` §5.2 invariant 7 and the rewritten template lint forbid), never put a Django variable inside an Alpine directive value (`FRONTEND-STACK.md` §3), and render user data with `textContent`. **No htmx** (brief §8): one library is enough, and htmx 2 history snapshots copy pages into `sessionStorage`, which would leak the setup page. |
| No `style=""` attributes or `<style>` blocks (`style-src 'self'`, no `'unsafe-inline'`) | Blocks CSS-injection exfiltration on secret-bearing pages; costs nothing with utility classes | **Keep.** Tailwind needs neither. JS may still set `el.style.prop` through the CSSOM (CSP allows that), so Alpine `x-show` and `x-transition` work; Alpine **string** `:style` bindings call `setAttribute('style')` and are blocked, so use classes or the object syntax. ApexCharts injects its own `<style>` element, which this policy blocks; it is also no longer MIT (dual-licensed: free only for organisations under $2M, OEM licence when embedded in a platform used by others, `apexcharts@7.8.0/LICENSE`), and JS chart libraries are out of scope (brief §3). |
| Autoescape, no `mark_safe` | R1 | **Keep.** Watch component or partial libraries that call `mark_safe` (the existing test greps `powermon/web/**/*.py`); the `{% icon %}` tag reads trusted SVG files from `templates/icons/` by allowlisted name and must not take user data. It is the one module allowed to call `mark_safe`; `SafeString(`, `SafeText`, `@html_safe`, `__html__` and `format_html(` with a non-literal first argument are banned everywhere (`TEST-STRATEGY.md` §3.5). |
| POST + CSRF; GET never writes | R2 | **Keep.** Every state change stays a plain `<form method="post">` with `{% csrf_token %}` and POST → redirect → GET; JS only enhances it. Phase 6 has no JS-initiated write. Every POST is a native form submission: toggles, modal confirmations, Reveal, Regenerate and the theme fallback. `fetch` is used only for GETs (the status JSON and the confirmation fragments), so no request needs `X-CSRFToken`, and `admin.js` never reads the CSRF cookie or token. |
| Server-side confirmation step with GET pre-checks (UI5-D7) and the regenerate HMAC marker (UI-D7) | R7, R4 | **Keep the server contract.** Approved as UI-07: a native `<dialog>` modal that **loads the server's confirmation GET** (marker, state block, refusal redirects). The `fetch` uses `redirect: "manual"`; an opaque redirect leads to `location.assign(url)`, and that full GET re-runs the check. A refusing view therefore queues its flash only for the full-page variant: with `X-PM-Fragment: 1` it returns the same 302 without calling `messages.*`, so the flash is shown exactly once (`DESIGN-DIRECTION.md` §3). Only these four GET views honour `X-PM-Fragment`. A client-side `confirm()` is never acceptable. The four pages stay as the no-JS and deep-link fallback. |
| Reveal/Regenerate render the key directly with `no-store`; key never cached or in other responses | R4, SEC-04 | **Keep.** Reveal and Regenerate stay top-level POST navigations answered 200 with `no-store`; there is no in-place `fetch` reveal (brief §6.1). The key never goes to any browser storage (R4), and `admin.js` clears a revealed page on `pagehide` so the back/forward cache cannot restore it (`DESIGN-DIRECTION.md` §3 S8). Copy-to-clipboard is fine (`navigator.clipboard` needs HTTPS or localhost) and is unavailable while the key is masked (UI-08). Accepted: Copy puts the key in the OS clipboard (and any clipboard history or sync), as a manual copy already does. The "Copied" live message is fixed text and never contains the value; there is no auto-clear and no clipboard read. |
| Token write-only: password input, never prefilled, masked in help | R3 | **Keep.** No "show token" eye toggle that would need the value. Browsers may still offer to save the token from a password input despite `autocomplete="off"`. Keep the "saved but never shown again" help, and consider password-manager ignore hints on the token input (`data-1p-ignore`, `data-lpignore="true"`, `data-bwignore`). This behaviour predates Phase 6 and is accepted. |
| Error pages: fixed copy, standalone 500 | R11 | **Keep.** The new error pages use an error layout that reads no context variable (no sidebar, no account menu, no toasts); context processors still run on 404 and 403-CSRF, so each must be lazy and return nothing for an anonymous request. The theme follows the system media query. |
| Throttle 429 page | R9 | **Keep the semantics;** restyle freely. |
| Live-refresh endpoint | New attack surface | **Approved as UI-05.** Login-required (default deny), `never_cache`, GET-only, **no secrets** in the payload (no key, no token, not even masked), included in the INV-23 scans. |

### Stylistic or self-imposed: drop

| Constraint | Where it was set | Phase 6 decision |
|---|---|---|
| System fonts only, no `@font-face` | P1 Design System; `test_css.py:385`; CSP `default-src 'none'` blocks fonts | **Drop.** Self-host Inter Variable woff2 (latin, cyrillic and their -ext subsets) from `@fontsource-variable/inter` 5.3.0, OFL-1.1 (brief §8). It matches the chart's Inter (`powermon/chart/fonts/Inter-*.ttf`, `docs/chart-spec.md:77`) and covers the Ukrainian and Russian location names. `font-src 'self'` allows it. |
| No JavaScript | P1 D-01, STACK.md:31, UI-SPEC Interaction Rule 6 | **Drop.** Native `<dialog>`, invoker commands and the Popover API, plus `@alpinejs/csp` 3.17.4 and one `admin.js`, for the sidebar drawer, theme switch, copy buttons, modals, toasts, tabs, submit guard and polling. Keep the server-rendered no-JS paths working (PRG flows). |
| No build step | P1 Styling decision; STACK.md:31 | **Drop.** Tailwind CSS v4.3.3 standalone CLI, pinned by sha256, in a Docker `css` stage **before** `collectstatic`, plus a local dev watcher; see §6. No Node in the runtime image. |
| One CSS file ≤ 300 lines; only `:root` tokens; hex and px allowlist | P1 file rules; `test_css.py:380-387` (300-line cap, no `@font-face`/`url()`, px allowlist `ALLOWED_PX` at `:32`) | **Drop** `test_css.py` entirely; define design tokens in the Tailwind `@theme` instead. |
| 4 font sizes / 2 weights / no `text-transform` / light only / no dark mode | P1 Typography and Color | **Drop.** Light/Dark/System themes are approved (UI-02, D6-03), server-rendered from a cookie so there is no flash; brief §7 amends `REQUIREMENTS.md:124`. Keep the chart-matching status hues (on `#62C28A`, off `#CC3434`, not-monitored `#A09D94`) as semantic tokens so admin and chart agree. |
| No icons (8px CSS dot + label only) | P1 Design System | **Drop.** Inline SVG icons from vendored Lucide (`lucide-static` 1.52.0, ISC), about 30 files, rendered by an `{% icon %}` tag. Keep a text label next to every status colour (accessibility). |
| 960px single column, top header, one nav item | P1 Page Shell | **Drop.** Sidebar and top bar layout, TailAdmin-like (UI-01, UI-03). Keep breadcrumbs and `aria-current`. |
| No live refresh | P1 E3 loading; `test_templates.py:229` | **Approved as UI-05,** under the rules in the essential table above. |
| No JS confirm dialogs | P4 Interaction Rule 6 | **Approved as UI-07** (the modal loads the server GET; the pages stay as fallback). |
| One accent button per page; accent and danger reservation rules | P4 Color, UI-D15; `test_location_page.py:469` | **Drop** as a rule; the visual hierarchy is set by the Phase 6 UI-SPEC. |
| `user-select: all` code blocks as the copy mechanism | P1 Code block | **Replace** with a Copy button (UI-08) and keep `user-select: all` as a fallback. |
| Exactly one `<link>` in the head | `test_security.py:282-285` | **Drop;** allow several hashed same-origin assets. |
| Destructive button before "Keep …" in the button row | UI5-D11 (kept for consistency) | **Decided (brief §9):** "Keep …" comes first and gets initial focus; the destructive button is last and shows a spinner. Applies to all four confirmations, page and modal. |
| 44px minimum touch targets, visible focus ring, `<label for>`, `aria-describedby`, `aria-invalid`, `th scope`, heading order, `.visually-hidden` labels | P1 accessibility floor | **Not security, but keep;** now UI-12. Fix the open findings: announce flashes, hide the breadcrumb separators from screen readers, give tables a caption. |

---

## 6. Static pipeline and CSP as built

**Settings** (`powermon/settings.py`):
- `STATIC_URL = "static/"`, `STATIC_ROOT = BASE_DIR / "staticfiles"` (:138-139).
- `STORAGES["staticfiles"] = whitenoise.storage.CompressedManifestStaticFilesStorage` (:140-143):
  - hashed names (`web/app.<12hex>.css`);
  - gzip variants;
  - **no Brotli** (no `brotli` package in `pyproject.toml`/`uv.lock`);
  - manifest-strict: a missing entry raises `ValueError` at render time (`test_templates.py:367`).
- There is no `STATICFILES_DIRS`; files are found through the `powermon.web` app's `static/`.
- There are no `WHITENOISE_*` settings, so the WhiteNoise 6.12 defaults apply (`uv.lock:704-705`):
  - `WHITENOISE_USE_FINDERS` and `AUTOREFRESH` equal `DEBUG`;
  - non-hashed files get `max-age=60` when `DEBUG` is off;
  - hashed files are recognised as immutable and get `Cache-Control: max-age=315360000, public, immutable`.
  - These come from the WhiteNoise defaults, not from a live response; a Wave 0 asset test should assert them with `DEBUG` off.

**Middleware order** (`settings.py:50-64`), relevant parts:
- `ContentSecurityPolicyMiddleware` is **outermost**, then `SecurityMiddleware`, then `WhiteNoiseMiddleware`, then sessions, common, CSRF, auth, `LoginRequiredMiddleware`, messages, clickjacking.
- Consequences:
  - Static responses carry the CSP and nosniff headers.
  - Static files are served before auth, so they are public. Fine for CSS, JS and fonts; never put anything secret in `static/`.

**Docker** (`Dockerfile:9-19`):
- The `base` stage runs `uv sync --locked --no-dev`, `COPY . /app` (:16), then `RUN APP_BUILD=1 python manage.py collectstatic --noinput` (:18). `APP_BUILD=1` is build mode, where settings skip the secret checks and the database (`settings.py:91-94`).
- `staticfiles/` is in `.gitignore` and `.dockerignore`.
- The `dev` and `runtime` stages inherit the collected files.
- **Phase 6 build (brief §8):**
  - A `css` stage downloads the Tailwind v4.3.3 standalone binary with `ADD --checksum=sha256:…` (per-arch, from the release's `sha256sums.txt`), copies `powermon/web/assets/` and `powermon/web/templates/`, and builds the entry `powermon/web/assets/css/app.css` (`@import "tailwindcss" source(none)` plus explicit `@source` paths) with `--minify`.
  - `base` does `COPY --from=css` into `powermon/web/static/web/build/app.css` after `COPY . /app` and **before** `collectstatic`; otherwise pages fail with "Missing staticfiles manifest entry".
  - No Node anywhere; the binary includes `@tailwindcss/forms` and `@tailwindcss/typography`.
  - Add `powermon/web/static/web/build/` to `.gitignore`.
  - Local dev: a compose override with a `css` watcher (`--watch=always`; without `=always` the watcher exits when stdin closes in a container) and a `web` service with `DEBUG=1` and `./powermon:/app/powermon` mounted (not `.:/app`, which hides `/app/.venv`).
- **Pitfalls:**
  - `ManifestStaticFilesStorage` rewrites `url()` in CSS (good for fonts), but the Tailwind CLI does not rewrite `url()` paths, so font URLs must be written relative to the **output** file.
  - It does not rewrite ES-module `import` paths, so `admin.js` stays one file with no imports.
  - Do not build with `--map` unless the `.map` file ships too; the manifest storage follows `sourceMappingURL` and fails on a missing file.
  - Never hard-code `/static/`; use `{% static %}`, and pass static URLs to JS through `data-*`.
- Python dependency policy: pinned upper bounds, `exclude-newer = "7 days"` (`pyproject.toml:32`). The vendor manifest with sha256 per file plays the same role for frontend assets.

**How `app.css` is served today:** `base.html:8` and `500.html:8` use `{% static 'web/app.css' %}`, which resolves to `/static/web/app.<hash>.css` and is served by WhiteNoise from `STATIC_ROOT`. gunicorn uses the gthread worker (2×4); Caddy proxies in production (`docker/Caddyfile`, no caching layer). gunicorn's access log is **off** on purpose, because `?key=` would leak (`gunicorn_conf.py:11-13`).

**CSP middleware** (`powermon/web/middleware.py:13-27`):
- A static header string (`CSP`, :13), set on every response whose `request.path != "/hb"` exactly (`/hb/` and `/hbx` still get it):
  `default-src 'none'; style-src 'self'; img-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'`
- Phase 6 replaces it with the brief §8 policy quoted in §5.
- The same literal is duplicated in `tests/test_walking_skeleton.py:30-33` and `tests/web/test_security.py:32-35`; update all three together, and add negative assertions (no `unsafe-inline`, no `unsafe-eval`, no `*`, no `http(s):`).
- Django 5.2 (`uv.lock:201-202`, 5.2.17) has **no built-in CSP**. Built-in CSP with nonces arrives in Django 6.0, which is outside the `<5.3` pin, so keep the custom middleware.
- `X_FRAME_OPTIONS = "DENY"` (`settings.py:135`) duplicates `frame-ancestors 'none'`.
- There is no favicon today: `/favicon.ico` renders the 404 HTML page. Add a self-hosted icon (allowed by `img-src 'self'`).
