# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A small collection of standalone scripts (no shared library code, no test suite,
no build step) for pulling participant/consent data out of the Sage Bionetworks
**Bridge** research platform (`webservices.sagebridge.org` / `ws.sagebridge.org`).
Each script in `util/` is self-contained, dependency-free, and independently
runnable — there is no shared entrypoint and no Synapse integration.

## Environment / dependencies

- Both Python scripts (`export-all-participants.py`, `get-consents.py`) are
  stdlib-only (`urllib`, `csv`, `json`) and can be run directly with any
  Python 3.
- The `.sh` scripts require only `curl` and `jq` in `PATH`.

## Scripts (all in `util/`)

`export-all-participants.py`, `export-study-subjects.sh`, and `get-consents.py`
all share the same "page-search a study, keep only study subjects, then fetch
per-subject detail" two-phase pattern (see below) — they just do different
things with the detail once fetched. When editing one, check whether the
equivalent behavior/wording should be mirrored in the others:

- `export-all-participants.py` / `export-study-subjects.sh` — richer export:
  two-phase (list accounts filtered to empty-`roles` "study subjects", then
  per-subject detail GET with `consents=true`), one row per subject×enrollment.
- `export-participants-csv.sh` — simpler/faster single-phase export straight
  from the paged search endpoint (no per-subject detail calls), one row per
  account regardless of study enrollment.
- `export-participant-roster.sh` — doesn't fetch data itself; triggers
  Bridge's async `emailRoster` job which emails a password-protected CSV zip.
- `get-consents.py` — consent-specific export: same two-phase study-subject
  pattern as `export-all-participants.py` (page-search a study, filter to
  empty-`roles` accounts, then per-subject detail GET with `consents=true`),
  but instead of a general participant CSV it writes a signature CSV
  (participant id, signed name, signed-on date, sharing scope, template
  version) and downloads each distinct signed consent template's document
  content to disk.

## Conventions used across these scripts (follow when adding new ones)

- **Config via env vars, prompted interactively if missing and a TTY is
  attached** (`BRIDGE_BASE_URL`, `BRIDGE_APP_ID`/`BRIDGE_APP`, `BRIDGE_EMAIL`/
  `BRIDGE_USER`, `BRIDGE_PASSWORD`/`BRIDGE_PASS`, `BRIDGE_STUDY_ID`, etc.).
  Passwords always use a hidden-input prompt and are `unset`/cleared from
  memory as soon as they're no longer needed.
- **No secrets ever hit the command line or `ps` output.** Bash scripts pass
  POST bodies to curl via `--data-binary @<0600 tempfile>`, never as inline
  `-d` args or query strings.
- **Bash HTTP helper pattern**: a `request METHOD URL SESSION [BODY]`
  function sets global `HTTP_STATUS`/`HTTP_BODY` and must be called directly
  (never inside `$(...)` or a pipeline), since a subshell would lose the
  global writes.
- **Bridge's non-standard sign-in status codes are handled explicitly and
  consistently**: `200` ok, `412` = authenticated-but-not-consented (still
  carries a usable `sessionToken`, so proceed), `401`/`403`/`404`/`423`/`429`
  each get a specific, actionable error message. Match this behavior in new
  scripts that sign in to Bridge.
- **Paging**: Bridge search endpoints cap `pageSize` at 100; page via
  `offsetBy` until `offset >= total` or a short page is returned (guards
  against off-by-one on a stale `total`).
- **Atomic output writes**: CSV output is written to a temp file and moved
  into place at the end (`os.replace` / `mv` after a `trap`), so a crash or
  Ctrl-C never leaves a truncated/partial CSV at the real output path.
- **Role requirements differ by scope** and are called out in each script's
  header comment and 403 error text: app-wide participant search needs
  DEVELOPER or RESEARCHER; single-study scope also accepts STUDY_COORDINATOR/
  STUDY_DESIGNER. `healthCode` is only populated if the app has
  `healthCodeExportEnabled=true` or the caller is SUPERADMIN.
- Scripts distinguish **"study subjects"** (accounts with an empty `roles`
  set) from admin/staff accounts, and skip the latter when producing
  participant-facing reports.

## Working in this repo

- There are no automated tests or linters configured — verify changes by
  actually running the script against a real (or sandbox) Bridge app.
- Don't commit real output files: `consent.log`, `get-consents.*.log`, and any
  generated `*.csv` are working output, not source — check `git status` before
  adding files.
