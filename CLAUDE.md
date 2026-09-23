# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

A small collection of standalone scripts (no shared library code, no test suite,
no build step, no shared entrypoint, no Synapse integration) for pulling data out
of the Sage Bionetworks **Bridge** research platform
(`webservices.sagebridge.org` / `ws.sagebridge.org`). See `README.md` for the
full description of the `util/` (Bridge REST API) vs `aws/` (direct AWS access)
split, and `CONTRIBUTING.md` for the conventions specific to each.

## Environment / dependencies

See `util/README.md` and `aws/README.md` for exact requirements. In short:
`util/` is dependency-free; `aws/` needs `boto3` and `awscli` via `pipenv
install` (`Pipfile`).

## Scripts in `util/`

`export-all-participants.py`, `export-study-subjects.sh`, and `get-consents.py`
all share the same "page-search a study, keep only study subjects, then fetch
per-subject detail" two-phase pattern — they just do different things with the
detail once fetched. When editing one, check whether the equivalent
behavior/wording should be mirrored in the others. See `util/README.md` for
what each script does.

## Scripts in `aws/`

- `export-upload-schemas.py` — dumps Bridge Upload Schemas via a DynamoDB
  PITR export to S3 (`start` then `fetch` subcommands). `--table` and
  `--expect-account` are both required on every run (no dev/prod default —
  dev and prod are separate AWS accounts, so you always say explicitly which
  one you mean); `--expect-account` is a hard account-match gate that even
  `--yes` can't bypass. See `aws/README.md` for the full behavior: `--app-id`
  scoping, checksum verification, audit logging, and required IAM/S3
  permissions (`iam-policy-export-upload-schema.json`).

## Conventions and contributing

See `CONTRIBUTING.md` for the conventions shared across these scripts (env
var/prompt pattern, secret handling, atomic writes, role requirements, etc.)
and notes on working in this repo (no tests/linters, don't commit output
files). Follow those conventions when adding a new script or editing an
existing one.
