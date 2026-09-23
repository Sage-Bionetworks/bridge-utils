# Contributing

Conventions used across these scripts — follow them when adding a new one or
editing an existing one.

## Conventions shared across all scripts

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
- **Atomic output writes**: output is written to a temp file and moved into
  place at the end (`os.replace` / `mv` after a `trap`), so a crash or Ctrl-C
  never leaves a truncated/partial file at the real output path.
- **Role requirements differ by scope** and are called out in each script's
  header comment and 403 error text: app-wide participant search needs
  DEVELOPER or RESEARCHER; single-study scope also accepts STUDY_COORDINATOR/
  STUDY_DESIGNER. `healthCode` is only populated if the app has
  `healthCodeExportEnabled=true` or the caller is SUPERADMIN.
- Scripts distinguish **"study subjects"** (accounts with an empty `roles`
  set) from admin/staff accounts, and skip the latter when producing
  participant-facing reports.

## Conventions specific to `aws/`

- New `aws/` scripts are the one exception to the dependency-free rule above
  — add their dependencies to the top-level `Pipfile`.
- Prefer AWS-managed/serverless operations (e.g. a DynamoDB PITR export) over
  anything that needs its own compute to run. There's no Lambda/ECS project
  in this repo, and these scripts are meant to be run directly from a
  terminal or AWS CloudShell — don't add one to solve a one-off problem.

## Working in this repo

- There are no automated tests or linters configured — verify changes by
  actually running the script against a real (or sandbox) Bridge app.
- Don't commit real output files: `consent.log`, `get-consents.*.log`, any
  generated `*.csv`, and any `aws/` script output (`*.json`, `*.audit.json`)
  are working output, not source — check `git status` before adding files.
