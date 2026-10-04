# Phase 6 kickoff pack — Admin UI rebuild

Everything needed to start Phase 6 in GSD. Prepared 2026-10-04 from this repo at HEAD `0d76e29`, the running admin (localhost:8000), the TailAdmin free template and external stack research.

| File | What it is | Who reads it |
|---|---|---|
| `PHASE-6-BRIEF.md` | **The phase brief:** goal, scope, requirements UI-01…UI-13, maintainer decisions, the behaviour and security contract, superseded constraints, stack, success criteria, open questions, suggested waves | Every Phase 6 step |
| `ADMIN-INVENTORY.md` | **Parity contract:** every screen, state, action, flash and security rule (R1–R16) the rebuild must keep | ui-phase, planner, verifier |
| `DESIGN-DIRECTION.md` | Layout shell, page-by-page design, components, colour, type and dark-mode tokens, JS interactions | sketch, ui-phase |
| `FRONTEND-STACK.md` | Pinned stack (Tailwind v4 standalone CLI, Alpine CSP build, Inter, Lucide), Dockerfile sketch, CSP, pitfalls | researcher, planner |
| `TEST-STRATEGY.md` | What happens to the 351 web test functions, the hook contract, new suites, coverage gate | planner, Nyquist, verifier |
| `PROJECT-STATUS.md` | What phases 1–5 built, how, and what is still open | you |
| `before/` | Screenshots of the current admin | sketch, ui-review (before/after) |

## Before you start

1. **Commit this folder before any GSD step.** `docs/phase-6/` is untracked. GSD executors run in git worktrees created from HEAD (`workflow.use_worktrees: true` in `.planning/config.json`), and `.worktreeinclude` copies only gitignored paths (`.planning/`, `.env.docker_local`) into them, so an executor cannot open these docs unless they are committed: `git add docs/phase-6 && git commit -m "docs(phase-6): Phase 6 kickoff pack"`. Commit again after every change to this folder. The `before/` screenshots show only demo data with masked secrets, so they are safe to commit.
2. **Free disk space.** The data volume is ~96% full, and Docker Desktop hung under parallel executors (`.planning/WINDOWS.md`). If Docker Desktop hangs again during `/gsd-execute-phase 6`, turn off parallel plan execution with `node .claude/gsd-core/bin/gsd-tools.cjs query config-set parallelization false`, and set it back to `true` afterwards.
3. **Keep the local stack running with demo data:** `powermon-local-*` plus `powermon-demo-devices` at http://localhost:8000. The UI-review screenshots (item 4) need it. `powermon-demo-devices` is an ad-hoc container that no compose file in this repo defines; do not remove it.
4. **Prepare the UI-review screenshots yourself.** `gsd-ui-auditor` only probes http://localhost:3000, :5173 and :8080 (`.claude/agents/gsd-ui-auditor.md:105-136`). It needs an HTTP 200 on `/` and then screenshots that one URL at 1440, 768 and 375 px. The admin runs on :8000 and `/` redirects to sign-in (302, so publishing the stack on :3000 does not help either), and the auditor falls back to a code-only audit. That is why the 04/05 reviews were blind. `workflow.ui_interaction_capture` is not offered by `/gsd-settings` (set it with `node .claude/gsd-core/bin/gsd-tools.cjs query config-set workflow.ui_interaction_capture true`). It only adds captures after a successful static capture, so it does not fix this. After `/gsd-execute-phase 6` and before `/gsd-ui-review 6`: sign in on the local stack with demo data and capture every screen and state in ADMIN-INVENTORY S1–S13 and E1–E3, in light and dark, at 1440 px and 360 px. Save the files in `.planning/ui-reviews/06-manual/` as `<NN>-<screen>-<theme>-<width>.png`. That folder is gitignored, so a revealed demo key never reaches git. Step 6 tells the auditor to use them.
5. **Back up `.planning/`.** It is gitignored (`commit_docs: false`), so the whole design history lives only on this disk. For example: `tar czf ~/powermon-planning-$(date +%F).tgz .planning`.
6. **Phase 6 is independent of the backend UAT.** You can run `/gsd-verify-work 01…05` before or alongside it. Do the DoD drills before connecting real subscriber channels, and before the DST change on 2026-10-25.

## Steps

### 1. Add the phase
```
/gsd-phase Admin UI Rebuild
```
This creates `.planning/phases/06-admin-ui-rebuild/` and appends a skeleton entry at the very end of `ROADMAP.md`, after the Progress table, because this roadmap has no milestone marker or `---` separator. Step 2 moves and fills it. Ignore the suggested next command `/gsd-plan-phase 6`.

### 2. Apply the planning amendments (normal message, not a slash command)
Paste this into Claude Code in this repo. It applies the planning amendments of brief §7; where this prompt is more detailed than brief §7, the prompt is authoritative.

```
Read docs/phase-6/PHASE-6-BRIEF.md in full (§4 and §7 especially). This is a documentation-only change and I explicitly ask you to bypass the "GSD Workflow Enforcement" section of .claude/CLAUDE.md: edit the files below directly, start no GSD command, do not commit. Make exactly these edits and nothing else.

1. .planning/REQUIREMENTS.md
   a. Insert a subsection "### Admin UI (UI)" at the end of "## v1 Requirements" (after "### Security (SEC)", before "## v2 Requirements") with UI-01…UI-13 verbatim from brief §4, sub-bullets included, as "- [ ] **UI-NN**: …" lines.
   b. In the paragraph under "## v1 Requirements", replace "Scope is closed: 49 requirements." with "Scope is closed: 49 v1 requirements + 13 Phase 6 UI requirements (UI-01…UI-13, added 2026-10-04 by the maintainer)." Leave the rest of the paragraph unchanged.
   c. Out of Scope: change "admin UI localisation or themes" to "admin UI localisation"; replace the cell "Web charts or analytics in the admin panel (beyond the recent-outages list in DATA-02)" with "Web charts or analytics in the admin panel, beyond the recent-outages list (DATA-02), the weekly chart preview (UI-06) and the fleet summary (UI-04)"; append the row "| Admin search or command palette, notification centre, PWA / offline, JS chart libraries, htmx | One admin and at most 20 locations; the weekly PNG is the only chart (docs/phase-6/PHASE-6-BRIEF.md §3) |".
   d. Traceability: after the SEC-04 row add "| UI-01 | Phase 6 | Pending |" through "| UI-13 | Phase 6 | Pending |". Set the Coverage lines to "- Requirements: 62 total (49 v1 + 13 Phase 6 UI)", "- Mapped to phases: 62 (Phase 1: 13, Phase 2: 13, Phase 3: 8, Phase 4: 11, Phase 5: 4, Phase 6: 13)", "- Unmapped: 0 ✓", and the footer to "*Last updated: 2026-10-04 after adding Phase 6 (UI-01…UI-13)*".
2. .planning/PROJECT.md
   a. Under "### Active", after "#### Security (SEC)", add "#### Admin UI (UI)" with the 13 "- [ ] **UI-NN**: …" headline lines (no sub-bullets).
   b. Make the same Out of Scope edits as 1c (PROJECT.md has its own copy of both rows; add the new row too).
   c. After the KD7 row of Key Decisions add: "| KD8 Admin frontend stack: Tailwind CSS v4 standalone CLI at image build only, TailAdmin-free-derived tokens, @alpinejs/csp plus one first-party admin.js, native dialog and popover, self-hosted Inter Variable and Lucide icons, self-only CSP, every asset pinned by sha256 (docs/phase-6/FRONTEND-STACK.md; D6-01…D6-07 in docs/phase-6/PHASE-6-BRIEF.md §5) (locked) | Maintainer decision 2026-10-04: rebuild the admin from scratch, beautiful and TailAdmin-like; the only surviving v1-lessons §4 rule is no third-party runtime assets | — Pending |".
   d. Under "**Reference docs:**" add "- `docs/phase-6/`: the Phase 6 kickoff pack (brief, parity inventory, design direction, frontend stack, test strategy)." Do not touch the "no paid services / Telegram is the only external dependency" constraint.
3. Supersede notes, worded exactly as below.
   a. Note A = "Superseded for the admin UI by Phase 6 (docs/phase-6/FRONTEND-STACK.md, KD8). The 'avoid Tailwind CDN / third-party runtime assets' rule still holds." Append " — " + Note A to the LAST CELL of these table rows, keeping each row on one line. Never insert a line between table rows: the stack block of .claude/CLAUDE.md is regenerated from STACK.md tables and bullets only. Rows: "Admin UI" (~line 31) and "Tailwind Play CDN or any third-party runtime JS/CSS" (~line 329) in .planning/research/STACK.md; "Admin UI" (~line 64) in .planning/research/SUMMARY.md; the same two rows (~lines 66 and 214) in .claude/CLAUDE.md. Edit STACK.md first, so a later regeneration of the CLAUDE.md block keeps the note.
   b. Note B = "> Visual, interaction and asset rules superseded by 06-UI-SPEC.md (Phase 6). Security-bound rules remain binding via docs/phase-6/PHASE-6-BRIEF.md §6.3." Insert it as its own paragraph directly under the "# Phase NN — UI Design Contract" heading (below the YAML frontmatter, never above its first "---") in .planning/phases/01-walking-skeleton/01-UI-SPEC.md, .planning/phases/04-location-management/04-UI-SPEC.md and .planning/phases/05-history-corrections-and-backups/05-UI-SPEC.md.
   c. Note C = "> Phase 6 note (2026-10-04): every admin-UI rule in this file about no JavaScript, one self-hosted CSS file / no JS build, styling, or confirmation pages without a modal is superseded by docs/phase-6/PHASE-6-BRIEF.md (D6-01…D6-07). The security-bound rules R1–R16 (brief §6.3) still hold." Insert it directly under the H1 of .planning/phases/01-walking-skeleton/01-CONTEXT.md, .planning/phases/04-location-management/04-CONTEXT.md and .planning/phases/05-history-corrections-and-backups/05-CONTEXT.md.
4. .planning/ROADMAP.md
   a. /gsd-phase appended a "### Phase 6: Admin UI Rebuild" skeleton at the END of the file, after the Progress table. Delete it, and insert the "Roadmap entry" block from docs/phase-6/README.md after the Phase 5 section, directly before "## Progress".
   b. Under "## Phases", after the Phase 5 bullet, add "- [ ] **Phase 6: Admin UI Rebuild** - Every admin screen rebuilt from scratch on a self-hosted Tailwind frontend: TailAdmin-like shell, themes, live status, chart preview, modals and toasts; behaviour and security unchanged".
   c. Change "Phases execute in numeric order: 1 → 2 → 3 → 4 → 5" to "Phases execute in numeric order: 1 → 2 → 3 → 4 → 5 → 6" and add the Progress row "| 6. Admin UI Rebuild | 0/0 | Not started |  |".
   d. Append to the first Overview paragraph: "Phase 6, added 2026-10-04 by the maintainer, rebuilds the admin frontend from scratch and maps the 13 Phase 6 UI requirements (UI-01…UI-13)."
5. Check: `node .claude/gsd-core/bin/gsd-tools.cjs query roadmap.get-phase 6` must show found: true and 6 success_criteria; `node .claude/gsd-core/bin/gsd-tools.cjs query init.plan-phase 6` must show phase_req_ids UI-01 … UI-13 and phase_dir .planning/phases/06-admin-ui-rebuild.
Do not change code, tests, templates, STATE.md, config.json or any other file. List every file you changed with a one-line summary.
```

**Roadmap entry** (used by the prompt above; its success criteria match brief §11):

```markdown
### Phase 6: Admin UI Rebuild

**Goal**: As the admin, I want a beautiful, modern, TailAdmin-like admin panel rebuilt from scratch on a self-hosted Tailwind frontend (sidebar, cards, light/dark themes, modals, toasts, copy buttons, live status, chart preview), so that daily work is fast and pleasant — with every behaviour and security guarantee of phases 1–5 unchanged.
**Depends on**: Phase 5
**Requirements**: UI-01, UI-02, UI-03, UI-04, UI-05, UI-06, UI-07, UI-08, UI-09, UI-10, UI-11, UI-12, UI-13
**Canonical refs**: `docs/phase-6/PHASE-6-BRIEF.md` (all; §5 decisions, §6 contract, §7 superseded constraints, §8 stack, §12 open questions), `docs/phase-6/ADMIN-INVENTORY.md` (parity contract, security rules R1–R16), `docs/phase-6/DESIGN-DIRECTION.md`, `docs/phase-6/FRONTEND-STACK.md`, `docs/phase-6/TEST-STRATEGY.md` (§4 test hook contract, §11 manual UAT), `docs/phase-6/before/`, `docs/v1-lessons.md` section 4 (Admin UI and web), `docs/chart-spec.md` section 5 (status colours), `.claude/skills/sketch-findings-*/SKILL.md` (written by `/gsd-sketch --wrap-up`; the chosen visual direction)
**Success Criteria** (what must be TRUE):
  1. Parity (UI-01, UI-07): every screen and state in ADMIN-INVENTORY.md renders in the new design in light and dark. Every action works with JS on; every form and destructive flow also works with JS off. The behaviour and security suites pass with unchanged meaning.
  2. Security (UI-13; R1–R16): R1–R16 hold; the CSP header equals the brief §8 policy exactly. There are zero CSP violations and JS errors in the console on every page in both themes, and no third-party origin anywhere. The secret scans pass on every page, fragment, JSON response and the chart PNG route.
  3. Live data and preview (UI-05, UI-06): an unplugged device shows Off on an open Locations page and location page within the detection window + 35 s, without a reload. The setup page flips to "first heartbeat received" on its own. The chart preview matches the channel's chart.
  4. Mobile and accessibility (UI-01, UI-12): at 360 px there is no page-level horizontal scroll, names wrap at word boundaries, the drawer works and touch targets are ≥ 44 px. The menus, modals, tabs and theme switch are usable by keyboard only. Contrast meets AA in both themes.
  5. Build and quality (UI-13; maintainer decision D6-01): the image builds reproducibly with pinned, checksummed assets, and hashed files are served with DEBUG off. The full gate is green with powermon/web at ≥ 80% coverage. The old app.css, the old templates and test_css.py are gone.
  6. New capabilities (UI-02, UI-03, UI-04, UI-07, UI-08, UI-09, UI-10, UI-11): the theme choice (Light/Dark/System) persists per browser with no wrong-theme flash, and System follows an OS change; the sidebar lists every location with its status and opens it in one click; the Locations page counts on/off/maintenance/waiting/delivery-failing locations matching the table; the four destructive actions confirm in a modal loaded from the server and still work as pages; copy buttons copy the URL, the revealed key and each example (none while masked); every flash is an announced toast in its own tone and submits cannot double-fire; the three switches are toggles posting the target value; times show a relative time next to the unchanged absolute time.

**Plans:** 0 plans

Plans:
- [ ] TBD (run /gsd-plan-phase 6 to break down)

**UI hint**: yes
**Research flag**: Yes, narrow. Re-verify every version, sha256 and licence in `docs/phase-6/FRONTEND-STACK.md` §0 and §7, and pass each new binary or file through the INV-26 legitimacy checkpoint before it enters the repo or the Dockerfile.
**Discuss-phase inputs**:
- Confirm brief §12 Q3–Q11 with their recommended defaults; Q1–Q2 (brand colour, neutrals) come from the `/gsd-sketch` winner (D6-06).
- Record D6-01…D6-07, the brief §6 contract and the 06-UI-SPEC obligations from `docs/phase-6/README.md` step 4 as locked decisions.
**Acceptance notes**:
- `/gsd-ui-review 6` scores ≥ 21/24 with no pillar below 3, judged from real screenshots of every screen in light and dark at 1440 px and 360 px (README "Before you start" item 4).
- The manual checklist in `docs/phase-6/TEST-STRATEGY.md` §11 is recorded in 06-UAT.md.
- 04-UAT #5 and 05-UAT #5 are marked superseded by Phase 6; 01-UAT #7 is checked here as UI-12 (brief §15).
```

There is deliberately no `**Mode:** mvp` line: Phase 6 is not an MVP slice (see brief §13 for the planner shape).

### 3. Sketch the look (decision D6-06)
Run the sketch **before** discuss, so discuss reads the chosen direction instead of locking a brand colour of its own.
```
/gsd-sketch Power Monitor admin rebuild. Read docs/phase-6/PHASE-6-BRIEF.md, docs/phase-6/DESIGN-DIRECTION.md and docs/phase-6/ADMIN-INVENTORY.md, and look at docs/phase-6/before/*.jpg. Target stack, for realism only (the sketches stay plain HTML and CSS as the sketch workflow requires): Django templates with Tailwind CSS v4 utilities and the Alpine CSP build (docs/phase-6/FRONTEND-STACK.md). Make 3 sketches, each with the same 3 variants:
- 001 app shell: sidebar with the location list and status dots; top bar with breadcrumbs, theme switch and account menu; mobile drawer.
- 002 Locations page: fleet summary tiles and the table; stacked cards on phones.
- 003 location page as a card grid: status header, delivery-failing banner, controls with toggle switches, weekly chart card using docs/assets/chart-mock-en.png, recent outages, settings, device setup, danger zone.
Variant A: TailAdmin-faithful (indigo #465FFF, cool grays, rounded-2xl cards). Variant B: calm neutral, shadcn-like, with a different accent. Variant C: denser 'ops console'. Show every variant in light AND dark: put the tokens in .planning/sketches/themes/light.css and dark.css so the toolbar theme switcher flips them, and make the phone viewport button 360 px; show the Locations page and the location page at 360 px too. Use the status colours from DESIGN-DIRECTION §5. Keep each variant to at most 4 font sizes and 2 weights, and keep gaps on 4/8/16/24/32/48/64 px (brief §12 Q10). Use no external URLs at all: no Google Fonts and no Tailwind or icon CDN. Inline Lucide-style SVGs. Load Inter with @font-face from ../../../powermon/chart/fonts/Inter-Regular.ttf and Inter-SemiBold.ttf, falling back to system-ui. Realistic data: 8 locations (on, off, maintenance, waiting, router grace, alerts off, two with failing delivery) with Ukrainian place names as in before/01-locations-list.jpg.
```
Optional: add `--quick` right after `/gsd-sketch` to skip the three mood questions the argument already answers.

Open the variants through `python3 -m http.server 8765` run in the repo root (http://localhost:8765/.planning/sketches/…/index.html); Chrome blocks `@font-face` loads from `file://` pages. Pick one (or mix them: "A's shell + B's cards"), then:
```
/gsd-sketch --wrap-up
```
This saves the chosen design as `.claude/skills/sketch-findings-<project>/`. `/gsd-ui-phase` (and `/gsd-discuss-phase`, run afterwards) loads it automatically. During curation, ask it to record the winner's light AND dark tokens as hex values, and to add to every reference file's "What to Avoid": "Sketch-only mechanics (inline `<style>` and `<script>`, `on*=` handlers, `style=` attributes, the sketch toolbar, CDN or Google Fonts links) are never ported; production markup uses Tailwind utilities and `Alpine.data()` components per docs/phase-6/FRONTEND-STACK.md §3." The skill lives in the untracked `.claude/`, so executors in worktrees will not see it: 06-UI-SPEC.md must carry every visual decision.

### 4. Discuss
```
/gsd-discuss-phase 6
```
The brief already answers most questions. Expect discuss to confirm brief §12 Q3–Q11: test parser, Playwright, chart preview caching, live-refresh transport, toast lifetime, filter chips, rail collapse, the GSD UI-checker limits and the optional sign-out `Clear-Site-Data` header. Q1 (brand colour) and Q2 (neutrals) are already answered by the sketch winner, which discuss loads from `.claude/skills/sketch-findings-*/SKILL.md`. Answer with the recommended defaults unless you want otherwise.

After discuss writes `.planning/phases/06-admin-ui-rebuild/06-CONTEXT.md`, check it for the items below. `/gsd-ui-phase` gives the UI researcher only STATE, ROADMAP, REQUIREMENTS, 06-CONTEXT.md and the sketch-findings skill (plus CLAUDE.md), never this folder directly, so anything missing from 06-CONTEXT.md does not reach the UI-SPEC. If something is missing, paste:
```
Add to .planning/phases/06-admin-ui-rebuild/06-CONTEXT.md, keeping everything already there:
- Under Implementation Decisions, a subsection "Locked by the maintainer (docs/phase-6/PHASE-6-BRIEF.md)":
  - D6-01…D6-07 (brief §5) with their IDs; the brief §6 behaviour and copy contract; R1–R16 (docs/phase-6/ADMIN-INVENTORY.md §4).
  - 06-UI-SPEC.md must: (1) state under its title that it supersedes 01/04/05-UI-SPEC for visual, interaction and asset rules; (2) contain a "Security-bound rules" section restating R1–R16; (3) contain a "Test hooks" section adopting docs/phase-6/TEST-STRATEGY.md §4, binding once approved; (4) contain the copy table for template-owned copy (TEST-STRATEGY §1 rule 5); (5) give every light AND dark token as a hex value, because executor worktrees do not get .claude/skills/; (6) treat powermon/web/static/web/app.css and the current templates as code to delete, never as an existing design system; (7) set Design System Tool to none and, if it lists component partials, open that list with "Could not enumerate: first-party Django template partials built in this phase; no installed package."
  - Type and spacing contract: the brief §12 Q10 answer.
  - Brand colour and neutrals: the /gsd-sketch winner (D6-06).
- Under Canonical References: every file in docs/phase-6/ with its path, docs/v1-lessons.md §4, docs/chart-spec.md §5, and .claude/skills/sketch-findings-*/SKILL.md.
```

### 5. UI design contract
```
/gsd-ui-phase 6
```
This produces `06-UI-SPEC.md`.

`gsd-ui-checker` BLOCKs more than 4 font sizes, more than 2 weights, and spacing outside 4/8/16/24/32/48/64 px. `DESIGN-DIRECTION.md` §5 as written breaks all three. Follow brief §12 Q10. With the default, the researcher applies the Q10 mapping. With the alternative, choose "Force approve" when the loop stops after 2 revisions. If one of the three checks below fails, add the missing item to 06-CONTEXT.md (the step 4 block) and re-run `/gsd-ui-phase 6` → "Update". "Update" re-runs the researcher with no extra instructions, so a request typed only in chat is lost.

Check three things:
- It says it **supersedes** 01/04/05-UI-SPEC for visual, interaction and asset rules.
- It keeps R1–R16.
- It includes the **test hook table** (`TEST-STRATEGY.md` §4).

### 6. Plan, execute, verify
```
/gsd-plan-phase 6
/gsd-execute-phase 6
/gsd-ui-review 6
/gsd-secure-phase 6
/gsd-verify-work 6
```
Type each command with nothing after the phase number: `/gsd-ui-review` builds its phase argument from the whole argument string, so a trailing comment breaks the phase lookup.

- **Planning check.** The first plan must be the tracer from brief §13. It delivers the asset pipeline, the CSP and policy-test rewrite and the test hook infrastructure together with S1 and E1–E3, and no other page template is planned before it lands. (Use `/gsd-plan-phase 6 --no-tracer` only if you want the literal horizontal waves.) Also check that every page plan migrates its own tests.
- **UI review.** First capture the screenshots (Before you start, item 4). Then, in the same session, send: "For the next /gsd-ui-review 6: the admin runs at http://localhost:8000 behind a login, so the auditor's localhost:3000/5173/8080 probe will find nothing. Put every file in .planning/ui-reviews/06-manual/ in the auditor's <required_reading> as its screenshot evidence, and docs/phase-6/before/ for comparison." Then run `/gsd-ui-review 6`.
- **Security audit.** `/gsd-secure-phase 6` covers the new surfaces: the status JSON, the chart PNG, the modal fragments and the `X-PM-Fragment` allowlist, the theme cookie and its POST, the sidebar context processor (including 404 and 403-CSRF), client storage, the back/forward cache and the clipboard.

## Scope, in one breath

**Phase 6 = every existing admin screen rebuilt from scratch, TailAdmin-like:**
- a Tailwind v4 build, self-hosted Inter, Lucide icons and Alpine (CSP build);
- light, dark and system themes;
- a sidebar with a location list, fleet summary tiles, live status refresh and a weekly chart preview;
- confirmation modals backed by the existing server confirmations, plus toasts, copy buttons, toggle switches and relative times;
- WCAG AA, and mobile down to 360 px.

**Unchanged:** every URL, POST/redirect flow, behaviour and security rule. **Not included:** search, notifications, admin localisation, analytics or JS charts, multiple users.
