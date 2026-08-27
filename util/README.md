# util/

Scripts that sign in to Bridge itself and call its REST API. Each is
self-contained and independently runnable — there's no shared library, no
build step, and no test suite.

## Requirements

- The Python scripts (`export-all-participants.py`, `get-consents.py`) need
  only Python 3's standard library — no packages to install.
- The shell scripts need `curl` and `jq` on `PATH`.

## Scripts

- **`export-all-participants.py`** / **`export-study-subjects.sh`** — rich
  export of study subjects: participant id, health code, external id,
  enrollment dates, sharing scope, consent status, and more. One row per
  subject × study enrollment.
- **`export-participants-csv.sh`** — faster, simpler export straight from the
  paged search endpoint (no per-subject detail calls). One row per account,
  regardless of study enrollment.
- **`export-participant-roster.sh`** — triggers Bridge's async `emailRoster`
  job, which emails a password-protected CSV zip. Doesn't fetch data itself.
- **`get-consents.py`** — exports signed consent data: a CSV of participant
  id, signed name, signed-on date, sharing scope, and consent template
  version, plus a copy of each distinct signed consent template's document
  content.

## Usage

Set the environment variables a script needs, or just run it and answer the
prompts (passwords use a hidden-input prompt):

```
BRIDGE_APP_ID=my-app BRIDGE_STUDY_ID=my-study ./export-all-participants.py
```

Common environment variables across these scripts: `BRIDGE_BASE_URL`,
`BRIDGE_APP_ID`, `BRIDGE_EMAIL`, `BRIDGE_PASSWORD`, `BRIDGE_STUDY_ID`. See each
script's header comment for its full list and defaults.

See the top-level `CONTRIBUTING.md` for the conventions shared across these
scripts (useful if you're adding a new one).
