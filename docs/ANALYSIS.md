# Agent Library — Prototype Analysis & Migration Plan

Source prototype: `agentlibrary_30.html` (3,360 lines; build marker `2026-08-19.4`).
Single file: `<style>` lines 10–663, static `<body>` shell lines 665–708,
`<script>` lines 709–3358.

---

## A. Detailed analysis of the attached HTML

### A.1 Structure

| Region | Lines | Contents |
| --- | --- | --- |
| `<head>` | 1–9 | Meta, title, Google Fonts (`Archivo`, `Montserrat`, `Caveat`, `JetBrains Mono`) |
| `<style>` | 10–663 | ~650 lines of hand-written CSS, no framework |
| `<body>` | 665–708 | Static shell only: sidebar, topbar, `#view-root`, `#detail-overlay`, `#modal-overlay`, `.toast-container` |
| `<script>` | 709–3358 | Whole application, 12 numbered sections |

The body is deliberately empty of content — **every screen is produced by
`innerHTML` from JavaScript**. That matters for the migration: there is no
server-rendered markup to preserve, so `templates/index.html` only needs to
carry the same shell, and the entire view layer stays in `app.js`.

### A.2 Design system

CSS custom properties on `:root` define the whole theme:

* Brand primary — `--navy #0F2D51`, `--blue #87B3E0`, `--cream #F8F7F3`, `--white`.
* Brand secondary — powder, oat, dove, clay, moss, harbour, slate (accents only).
* Functional aliases (`--bg-app`, `--bg-sidebar`, `--text-primary`, …) so the app
  can be re-themed by swapping ~15 variables.
* Nine status colour pairs (approved / pending / draft / deprecated / archived /
  warn / rejected / new / updated).
* Type scale: `--font-heading` Archivo (placeholder for licensed *Europa Grotesk SH*),
  `--font-body` Montserrat, `--font-accent` Caveat (stand-in for *Liana*),
  `--font-mono` JetBrains Mono.
* Radii, three shadow levels, `--sidebar-w 248px`, `--topbar-h 68px`.

Signature visual device: the **boarding-pass stub** on each asset card —
`.ac-body` / `.ac-perforation` / `.ac-stub`, with a coloured left rail per asset
type (`.tl-gpt`, `.tl-skill`, `.tl-agent`, `.tl-other`).

Accessibility already present and **must be preserved**:
`:focus-visible` outlines on every interactive element, `.sr-only`,
`@media (prefers-reduced-motion: reduce)`, `role="button"` + `tabindex="0"` on
clickable divs with Enter/Space handlers, `role="tablist"`/`role="tab"` on the
detail tabs, `aria-label` on icon-only buttons, `aria-label="Primary"` on the nav.

Responsive: `@media (max-width:1080px)` collapses grids; `@media (max-width:860px)`
turns the sidebar into an off-canvas drawer, makes the detail panel full-screen and
hides the rail arrows.

### A.3 JavaScript sections

1. **CONFIG** (744–834) — reference data: `assetTypes`, `platforms`,
   `defaultAdminPassword`, `departments` (display order), `departmentDigitOrder`
   (numbering order — deliberately independent), `tags`, `statuses`,
   `intendedAudience`, `sensitivity`, `reviewFrequency`, `recommendedModels`,
   `gptCapabilities`, `claudeSurfaces`, `resourceCategories`, `agentTriggerTypes`,
   `externalIntegrationOptions`, `fileKinds`, `fileTypeIcons`.
2. **DATA MODEL** (835–1470) — `makeAsset()` factory (~45 fields), `makeFile()`,
   `makeVersion()`, `calcNextReview()`, `departmentDigit()`, `nextWgtCode()`,
   four `config*()` shape helpers, and `buildMockAssets()` (16 demo assets,
   lines 982–1470, currently unused).
3. **DATA STORE** (1471–1591) — `localStorage` persistence with
   `SCHEMA_VERSION = 5`, `STORAGE_KEY = "wgt_agent_library_v1"`,
   `normalizeAsset()` repair pass, and `load/_persist/getAssets/getAsset/
   saveAsset/addAsset/logActivity`.
4. **ADMIN AUTH** (1592–1617) — plaintext password compare against
   `localStorage["wgt_admin_password_v1"]`, falling back to
   `CONFIG.defaultAdminPassword` (`"Wings123+"`, in source).
5. **UTILITIES** (1618–1751) — `escapeHtml`, `stripWgtPrefix`, date formatters,
   `freshnessBadge`, `statusBadgeClass`, `typeMeta`, `debounce`, `highlight`
   (wraps matches in `<mark>` **after** escaping — safe), `copyToClipboard`,
   the inline `Icon` SVG set and `Toast`.
6. **STATE & ROUTER** (1752–1890) — `State` object, `promptUnlock`/`attemptUnlock`,
   hash router (`#library|#add|#admin`) with `_suppressHashChange`, `setView()`,
   `render()` with a try/catch error screen.
7. **SIDEBAR** (1891–1922) — three nav items, pending-count pill, lock icon.
8. **LIBRARY VIEW** (1923–2261) — `getLibraryResults()` (Approved-only + platform/
   department/tag filters + full-text search over 9 fields with match reasons),
   filter chips, card renderer (boarding-pass), list-row renderer, sections
   (Featured / Recently Added / one per department), horizontal rails with
   scroll arrows, empty state, `/` keyboard shortcut.
9. **DETAIL PANEL** (2262–2591) — five tabs (Overview, How to Use, Configuration,
   Files & Resources, Version History), per-type configuration panes, expandables,
   copy-to-clipboard, Add-New-Version modal, generic `showModal()`/`closeModal()`.
10. **ADD TO LIBRARY** (2592–3092) — two-step form (type picker → tailored details),
    tag chips (max 4), `nextWgtCode()` preview, duplicate detection heuristic,
    `finalizeWizard()` / `submitNewAsset()` / `finalizeEditAsset()`.
11. **ADMIN / REVIEW** (3093–3284) — Pending Reviews table, Manage Library
    search-and-take-down table, approve / reject / remove-from-library (archive),
    change-admin-password modal.
12. **INIT** (3285–3358) — global chrome wiring, Esc handling, mobile nav,
    `window.onerror` / `unhandledrejection` toast net, hash bootstrap, and a
    `window.AgentLibrary` debug surface.

### A.4 Domain rules encoded in the prototype (must survive migration)

* **WGT code** = `WGT` + department digit (1-based index in `departmentDigitOrder`)
  + zero-padded 2-digit sequence, e.g. Finance's first asset is `WGT201`.
  Assigned once at submission, never changed. Department is therefore immutable.
* The **Library shows `Approved` assets only** — a fixed governance rule, not a
  user-toggleable filter.
* Every submission enters as **`Pending Review`**.
* Status vocabulary: `Pending Review`, `Approved`, `Rejected`, `Deprecated`, `Archived`.
  "Remove from Library" sets `Archived` and keeps the record.
* Approve / reject / edit / archive each **write a version-history entry**.
* Freshness badge: "Updated" if `lastUpdated != createdDate` within 30 days,
  else "New" if submitted within 30 days.
* Edit bumps the version by `+0.1`.
* Max 4 tags per asset.
* Type-specific fields live only inside `configuration` — never as new top-level columns.

### A.5 Notable defects in the prototype worth fixing during migration

* `getLibraryResults()` reads `a.configuration.instructions` without guarding
  `configuration` — a normalisation bug the store patches at load time instead.
* `approveAsset()` / `rejectAssetWithConfirm()` **overwrite** `versions[0]` rather
  than appending, silently destroying the newest history entry.
* `renderDetail()` exposes the internal record id (`AST-1001`) in the UI.
* Search re-renders the whole view on each keystroke and then re-focuses the input —
  works, but loses selection ranges.
* `data-copy-text` embeds full instruction text into an HTML attribute; escaped, but
  large.

---

## B. Every localStorage dependency

| # | Line(s) | Call | Purpose | Replacement |
| --- | --- | --- | --- | --- |
| 1 | 1540 | `localStorage.getItem("wgt_agent_library_v1")` | Load all assets + activity | `GET /api/assets`, `GET /api/admin/activity` |
| 2 | 1567 | `localStorage.setItem("wgt_agent_library_v1", …)` | Persist whole DB blob after any mutation | Per-mutation `POST`/`PUT` endpoints |
| 3 | 1605 | `localStorage.getItem("wgt_admin_password_v1")` | Read admin password | Server-side `users.password_hash` |
| 4 | 1613 | `localStorage.setItem("wgt_admin_password_v1", …)` | Store new admin password **in plaintext** | `POST /api/auth/change-password` (Argon2/PBKDF2 hash) |
| 5 | 1887 | `localStorage.removeItem(STORAGE_KEY)` in the render-error recovery button | "Reset saved data & reload" | Removed — server is the source of truth; the error screen just reloads |

Indirect dependencies (functions that only work because of the above):
`DataStore.load/_persist/getAssets/getAsset/saveAsset/addAsset/logActivity`,
`AdminAuth.getPassword/checkPassword/setPassword`, `SCHEMA_VERSION`,
`normalizeAsset()`.

---

## C. Every mock-data dependency

| # | Location | Item | Disposition |
| --- | --- | --- | --- |
| 1 | 982–1470 | `buildMockAssets()` — 16 fabricated assets with fake owners, emails and URLs | **Deleted.** Not ported to the server in any form. |
| 2 | 1559 | `this._cache = { … assets: [], activity: [] }` seed | Replaced by an empty database; assets only exist once submitted. |
| 3 | 846–847 | `__idCounter = 1000` / `nextId("AST")` client-generated ids | Replaced by server `BIGINT AUTO_INCREMENT` primary keys. |
| 4 | 856 | `makeFile()` fabricates `size: "${Math.random()*3+0.1} MB"` | Replaced by the real byte size recorded at upload. |
| 5 | 767–770 | `CONFIG.defaultAdminPassword = "Wings123+"` | **Deleted.** Admin created by `flask create-admin` with an interactively supplied password. |
| 6 | 744–834 | The rest of `CONFIG` (departments, tags, statuses, models, file kinds…) | **Kept**, but moved server-side to `app/reference.py` and served by `GET /api/config`. This is reference data, not demo data, so it is legitimately seeded. |
| 7 | 3357 | `window.AgentLibrary = { … buildMockAssets … }` debug surface | Removed. |

---

## D. Every simulated or incomplete feature

| # | Feature | Prototype behaviour | Production behaviour |
| --- | --- | --- | --- |
| 1 | File download / view | `data-mock-download` → toast *"Simulated: … would open from the connected file store."* (line 2544) | `GET /api/files/<id>/download` streaming from a private directory with authorisation + audit log |
| 2 | File upload | None at all — `makeFile()` invents a record from a filename string | `POST /api/assets/<id>/files`, multipart, extension + MIME + size validated, random stored name |
| 3 | Admin authentication | Client-side string compare against a constant in source | Flask-Login session, PBKDF2-SHA256 hashes, `user`/`reviewer`/`admin` roles |
| 4 | Change password | Writes plaintext to localStorage | `POST /api/auth/change-password`, verifies current password, rehashes, audit-logged |
| 5 | Approve / reject / archive | Mutates the local object; **overwrites** `versions[0]` | Transactional status change + appended `AssetVersion` + `ActivityLog` row |
| 6 | Version history | Array on the asset; approve/reject clobber the newest entry | `asset_versions` table, append-only, `is_current` flag maintained in one transaction |
| 7 | WGT code allocation | `Math.max(existing)+1` computed in the browser — two tabs produce the same code | `wgt_counters` row locked `FOR UPDATE` inside the transaction + `UNIQUE` constraint + retry |
| 8 | Duplicate detection | Client-side word-overlap heuristic over the local cache | Server-side `GET /api/assets/duplicate-check` over the real corpus (same heuristic, authoritative data) |
| 9 | Activity log | `DataStore.logActivity()` — capped at 200 entries, written but **never displayed** | `activity_log` table + `GET /api/admin/activity` with a real Admin view |
| 10 | "My Submissions" | Section 10 exists in the file map comment only — **no implementation** | New `#my` view backed by `GET /api/my-submissions` |
| 11 | Featured flag | Editable but nothing enforces who may set it | Admin/reviewer only, server-enforced |
| 12 | Review scheduling | `calcNextReview()` computed but never surfaced | `assets.next_review_date` column, indexed, exposed in Overview and filterable |
| 13 | Deprecated status | In `CONFIG.statuses`, no UI path to reach it | Reachable via admin status change; kept in the vocabulary |
| 14 | Search | In-memory `String.includes` over all assets | Server-side SQL `LIKE` over indexed columns with pagination |
| 15 | Back/forward | Hash-only, no per-asset deep links | Hash router extended with `#asset/<id>` so a detail panel is linkable |
| 16 | Session unlock | `State.unlocked` boolean, lost on refresh, trivially set from the console | Server session cookie, `HttpOnly`, `Secure`, `SameSite=Lax` |

---

## E. UI action → Flask route map

### Chrome / navigation

| UI action | Prototype | Route |
| --- | --- | --- |
| App boot | `initApp()` | `GET /` (renders `index.html`) |
| Load reference lists | `CONFIG` constant | `GET /api/config` |
| Who am I / nav gating | `State.unlocked` | `GET /api/auth/me` |
| Sidebar pending count | `assets.filter(status==="Pending Review").length` | `GET /api/assets?status=Pending%20Review&per_page=1` → `data.total` |

### Authentication

| UI action | Route |
| --- | --- |
| Sign in | `POST /api/auth/login` |
| Sign out | `POST /api/auth/logout` |
| Change password | `POST /api/auth/change-password` |
| Refresh CSRF token | `GET /api/auth/csrf` |

### Library view

| UI action | Prototype | Route |
| --- | --- | --- |
| Initial list | `DataStore.getAssets().filter(Approved)` | `GET /api/assets?status=Approved&page=1&per_page=…` |
| Search box | `getLibraryResults()` | `GET /api/assets?q=…` |
| Platform pill | `f.platform` | `GET /api/assets?asset_type=gpt` |
| Department select | `f.department` | `GET /api/assets?department=Finance` |
| Tag input | `f.tag` | `GET /api/assets?tag=Reporting` |
| Sort | implicit | `GET /api/assets?sort=-created_at` |
| Card ⇄ list toggle | `State.library.viewMode` | client-only (no route) |
| "Open Tool" | `window.open(asset.directUrl)` | client-only; URL comes from the asset payload |

### Detail panel

| UI action | Route |
| --- | --- |
| Open detail (all five tabs) | `GET /api/assets/<id>` |
| Files tab list | `GET /api/assets/<id>/files` |
| File "View" / "Download" | `GET /api/files/<id>/download` (`?disposition=inline` for View) |
| Upload a file | `POST /api/assets/<id>/files` |
| Delete a file | `DELETE /api/files/<id>` |
| Add New Version | `POST /api/assets/<id>/versions` |
| Version History tab | included in `GET /api/assets/<id>` |
| Copy instructions | client-only (clipboard) |

### Add to Library wizard

| UI action | Route |
| --- | --- |
| Step 1 type picker | client-only |
| Department → WGT preview | `GET /api/assets/next-code?department=…` (preview only; the real code is allocated server-side at insert) |
| Duplicate check before submit | `GET /api/assets/duplicate-check?department=…&name=…` |
| Submit for Review | `POST /api/assets` |
| Save Changes (edit) | `PUT /api/assets/<id>` |

### My Submissions

| UI action | Route |
| --- | --- |
| List my assets bucketed by status | `GET /api/my-submissions` |
| Edit my own pending/rejected asset | `PUT /api/assets/<id>` |

### Admin / Review

| UI action | Route |
| --- | --- |
| Pending Reviews table | `GET /api/assets?status=Pending%20Review` |
| Manage Library search | `GET /api/assets?status=Approved&q=…` |
| Approve | `POST /api/assets/<id>/approve` |
| Reject | `POST /api/assets/<id>/reject` |
| Remove from Library | `POST /api/assets/<id>/archive` |
| Edit | `PUT /api/assets/<id>` |
| Activity feed | `GET /api/admin/activity` |
| Change Admin Password | `POST /api/auth/change-password` |

### Operations

| Action | Route |
| --- | --- |
| Liveness / readiness | `GET /health` |

---

## F. Proposed database schema

MySQL 8, `utf8mb4` / `utf8mb4_0900_ai_ci`, InnoDB.

### `users`
| Column | Type | Notes |
| --- | --- | --- |
| `id` | BIGINT PK AI | |
| `email` | VARCHAR(254) | **UNIQUE**, indexed |
| `full_name` | VARCHAR(160) | |
| `password_hash` | VARCHAR(255) | PBKDF2-SHA256 (Werkzeug); never plaintext |
| `role` | ENUM(`user`,`reviewer`,`admin`) | indexed, default `user` |
| `is_active` | BOOL | default true |
| `created_at` / `last_login_at` | DATETIME | |
| `failed_login_count`, `locked_until` | INT / DATETIME | throttling |

### `assets`
| Column | Type | Notes |
| --- | --- | --- |
| `id` | BIGINT PK AI | server-generated |
| `wgt_code` | VARCHAR(16) | **UNIQUE**, indexed |
| `name` | VARCHAR(200) | indexed |
| `asset_type` | VARCHAR(16) | `gpt`/`skill`/`agent`/`other`, indexed |
| `platform` | VARCHAR(40) | indexed |
| `department` | VARCHAR(80) | indexed, **immutable after creation** |
| `status` | VARCHAR(20) | indexed |
| `owner_id` | BIGINT FK→users | indexed, nullable |
| `creator_id` | BIGINT FK→users | indexed |
| `description`, `use_case`, `problem_solved`, `input_requirements`, `expected_output`, `example_use` | TEXT | |
| `owner_name`, `owner_email`, `backup_owner` | VARCHAR | contact of record (may differ from the account) |
| `intended_audience`, `sensitivity`, `access_level` | VARCHAR(40) | |
| `review_frequency` | VARCHAR(40) | |
| `next_review_date` | DATE | indexed |
| `current_version` | VARCHAR(16) | |
| `direct_url` | VARCHAR(2048) | |
| `featured` | BOOL | indexed |
| `configuration` | **JSON** | type-specific fields |
| `additional_departments` | JSON | |
| `created_at`, `last_updated_at` | DATETIME | both indexed |
| `published_at`, `last_verified_at`, `submitted_at` | DATETIME/DATE | |
| `rejection_reason` | TEXT | |

Composite indexes: `(status, department)`, `(status, asset_type)`, `(status, last_updated_at)`.

### `asset_versions`
`id`, `asset_id` FK (indexed, `ON DELETE CASCADE`), `version_number` VARCHAR(16),
`summary` TEXT, `updated_by_id` FK→users, `updated_by_name` VARCHAR(160),
`is_current` BOOL, `instructions_snapshot` LONGTEXT, `configuration_snapshot` JSON,
`created_at`. Unique `(asset_id, version_number)`. **Append-only.**

### `asset_files`
`id`, `asset_id` FK (indexed), `asset_version_id` FK nullable (indexed),
`original_name` VARCHAR(255), `stored_name` VARCHAR(64) **UNIQUE** (random hex),
`relative_path` VARCHAR(255) (shard dir + stored name — never sent to the client),
`kind` (`source`/`deployable`/`knowledge`/`documentation`), `category`,
`extension` VARCHAR(16), `mime_type` VARCHAR(120), `size_bytes` BIGINT,
`sha256` CHAR(64), `scan_status` (`pending`/`clean`/`infected`/`skipped`),
`uploaded_by_id` FK, `created_at`.

### `tags` / `asset_tags`
`tags(id, name UNIQUE, slug UNIQUE, created_at)`;
`asset_tags(asset_id, tag_id)` composite PK, both sides indexed.

### `activity_log`
`id`, `ts` (indexed), `actor_id` FK nullable, `actor_email`, `action` (indexed),
`object_type`, `object_id` (indexed), `ip`, `user_agent` VARCHAR(255),
`detail` JSON.

### `wgt_counters`
`department_digit` VARCHAR(8) PK, `last_sequence` INT, `updated_at`.
Locked `SELECT … FOR UPDATE` inside the allocation transaction — this is the
race-condition fix.

### `config_settings`
`key` VARCHAR(64) PK, `value` JSON, `updated_at` — reference lists that admins may
edit later without a deploy.

---

## G. Migration plan

1. **Schema** — `flask db upgrade` applies `0001_initial` (all nine tables, all
   indexes, all constraints).
2. **Reference seed** — `flask seed-config` writes departments, digit order, tags,
   statuses, models, file kinds into `config_settings`, and pre-creates one
   `wgt_counters` row per department digit at `last_sequence = 0`. Idempotent.
   **No demo assets, ever** — the command refuses to seed assets and there is no
   code path that creates them.
3. **First admin** — `flask create-admin --email …` prompts for the password on a
   TTY (or reads `ADMIN_INITIAL_PASSWORD` from the deployment secret store when
   non-interactive). Fails if the account exists unless `--force`.
4. **Legacy data (optional)** — if any browser holds real prototype data, export it
   from the console (`JSON.stringify(localStorage.getItem("wgt_agent_library_v1"))`)
   and run `flask import-legacy dump.json --dry-run` first. WGT codes in the dump
   are preserved verbatim and the counters are advanced past them.
5. **Cutover** — take the prototype HTML out of circulation, deploy, verify
   `/health`, log in as admin, submit one throwaway asset end-to-end, approve it,
   archive it, then check `activity_log`.
6. **Rollback** — `flask db downgrade -1` plus the documented restore procedure.

Forward schema changes always ship as a new Alembic revision; the `configuration`
JSON column absorbs type-specific field additions without a migration.

---

## H. Deployment checklist

See `README.md` § *Production readiness checklist* for the executable version. In
brief: OS packages → service account → app dir + venv → `.env` (mode 0640, owned by
the service account) → MySQL DB + least-privilege user → `flask db upgrade` →
`flask seed-config` → `flask create-admin` → gunicorn socket smoke test → systemd
unit → nginx server block + TLS → firewalld → SELinux contexts and booleans →
`/health` green → backups scheduled → log rotation → `DEBUG=0` confirmed.

---

## I. Assumptions that need confirmation

1. **Identity provider.** Local accounts with password hashes are implemented. If
   WGT uses Entra ID / SAML SSO, the `User` model and `app/api/auth.py` are the only
   places to change; roles would then come from group claims.
2. **Self-registration.** Assumed **off** — accounts are created by an admin
   (`POST /api/admin/users`). The prototype allowed anonymous submission; that is
   now impossible because submissions must be attributable.
3. **Anonymous browsing.** Assumed **allowed** for the approved Library (read-only),
   matching the prototype. Set `REQUIRE_LOGIN_TO_BROWSE=1` to require a session for
   every page.
4. **Email domain.** Owner emails are validated as well-formed; restriction to
   `@wingsglobaltravel.com` is available via `ALLOWED_EMAIL_DOMAINS` but is off by
   default.
5. **No outbound email.** The prototype states rejection notices are handled
   manually. No SMTP is configured.
6. **Malware scanning.** ClamAV integration is wired but disabled by default
   (`CLAMAV_ENABLED=0`); files are marked `scan_status='skipped'` until enabled.
7. **Department list is stable.** New departments must be **appended** to
   `departmentDigitOrder`, never inserted — inserting renumbers existing codes.
8. **Department migration.** Assumed rare and admin-only; implemented as an explicit
   audited action that mints a new WGT code and records the old one, rather than
   silently editing the field.
9. **Retention.** `activity_log` grows unbounded; a 24-month pruning job is
   suggested but not scheduled.
10. **TLS.** Assumed an organisation-issued certificate; Certbot instructions are
    provided as the alternative.
11. **Version numbering.** The prototype's `+0.1` bump is preserved. Confirm whether
    semantic versioning is wanted instead.
12. **Fonts.** Google Fonts is a third-party CDN request. Self-hosting instructions
    are in `static/fonts/README.md`; switch if the CSP must forbid external hosts.
