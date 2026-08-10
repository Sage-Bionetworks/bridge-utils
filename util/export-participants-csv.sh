#!/usr/bin/env bash
#
# export-participants-csv.sh
#
# Synchronously pulls participants from a Bridge app and writes them to a local
# CSV. This reproduces what the BridgeResearcherUI "export all participants"
# feature is supposed to return, but depends on nothing except the API being up
# — no participantroster worker, no SQS, no email.
#
# By DEFAULT it exports EVERY account in the app — the same app-scoped set the
# main Participants page lists — via POST /v3/participants/search. This is the
# true "export all participants" report: it includes accounts enrolled in no
# study, and lists each account exactly once regardless of how many studies it
# is in (so it is NOT the same as concatenating per-study rosters, which would
# omit no-study accounts and duplicate multi-study ones).
#
# If BRIDGE_STUDY_ID is set, it instead narrows to that single study via
# POST /v5/studies/{studyId}/participants/search.
#
# Either way it pages by offsetBy (server caps pageSize at 100) until it has
# read `total`.
#
# Requirements:
#   - curl and jq on PATH
#   - App-wide export: your account needs DEVELOPER or RESEARCHER in the app.
#   - Single-study export: STUDY_COORDINATOR / RESEARCHER / STUDY_DESIGNER /
#     DEVELOPER on that study.
#
# No secrets are hard-coded. Values come from environment variables; anything
# missing is prompted for interactively (password input is hidden). The sign-in
# body is built with jq and streamed over stdin, so credentials never appear in
# `ps` output or your shell history.
#
# Environment variables (optional — you'll be prompted for any that are unset):
#   BRIDGE_BASE_URL   API root (default: https://webservices.sagebridge.org)
#   BRIDGE_APP_ID     Bridge app you sign in under
#   BRIDGE_EMAIL      Your Bridge account email
#   BRIDGE_PASSWORD   Your Bridge account password              (hidden prompt)
#   BRIDGE_STUDY_ID   Optional: narrow to one study. Blank = ALL app participants.
#   OUTPUT_FILE       CSV path (default: participants-<app|study>.csv)
#
# Usage:
#   BRIDGE_APP_ID=my-app ./export-participants-csv.sh                  # all app participants
#   BRIDGE_APP_ID=my-app BRIDGE_STUDY_ID=my-study ./export-participants-csv.sh  # one study

set -euo pipefail

# --- dependency check --------------------------------------------------------
for cmd in curl jq; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "error: required command '$cmd' not found on PATH" >&2
    exit 1
  fi
done

PAGE_SIZE=100  # server-enforced maximum

# --- helpers -----------------------------------------------------------------
prompt_value() {
  local __var="$1" __prompt="$2" __val
  __val="${!__var:-}"
  if [ -z "$__val" ]; then
    read -r -p "$__prompt: " __val </dev/tty
  fi
  if [ -z "$__val" ]; then
    echo "error: $__var is required" >&2
    exit 1
  fi
  printf -v "$__var" '%s' "$__val"
}

prompt_secret() {
  local __var="$1" __prompt="$2" __val
  __val="${!__var:-}"
  if [ -z "$__val" ]; then
    read -r -s -p "$__prompt: " __val </dev/tty
    echo >&2
  fi
  if [ -z "$__val" ]; then
    echo "error: $__var is required" >&2
    exit 1
  fi
  printf -v "$__var" '%s' "$__val"
}

# HTTP helper. MUST be called directly — never in a pipeline or $(...) — so its
# HTTP_STATUS/HTTP_BODY assignments land in the current shell (a function in a
# pipeline runs in a subshell and its global writes are lost). The POST body is
# passed via a 0600 temp file (curl --data-binary @file), not on the command
# line, so secrets never appear in `ps`.
#   request METHOD URL SESSION [JSON_BODY]   → sets HTTP_STATUS and HTTP_BODY
HTTP_STATUS=""
HTTP_BODY=""
request() {
  local method="$1" url="$2" session="$3" body="${4:-}"
  local resp_file body_file=""
  resp_file="$(mktemp)"
  local -a args=(-sS -o "$resp_file" -w '%{http_code}' -X "$method" "$url")
  [ -n "$session" ] && args+=(-H "Bridge-Session: $session")
  if [ "$method" = "POST" ]; then
    body_file="$(mktemp)"          # mktemp creates the file 0600 (owner-only)
    printf '%s' "$body" > "$body_file"
    args+=(-H 'Content-Type: application/json' --data-binary "@$body_file")
  fi
  HTTP_STATUS="$(curl "${args[@]}")"
  HTTP_BODY="$(cat "$resp_file")"
  rm -f "$resp_file" ${body_file:+"$body_file"}
}

# --- gather config -----------------------------------------------------------
BRIDGE_BASE_URL="${BRIDGE_BASE_URL:-https://webservices.sagebridge.org}"
prompt_value  BRIDGE_APP_ID   "Bridge app id"
prompt_value  BRIDGE_EMAIL    "Bridge account email"
prompt_secret BRIDGE_PASSWORD "Bridge account password"

# BRIDGE_STUDY_ID is OPTIONAL. Blank means "all participants in the app". Only
# prompt (allowing an empty answer) when running interactively.
BRIDGE_STUDY_ID="${BRIDGE_STUDY_ID:-}"
if [ -z "$BRIDGE_STUDY_ID" ] && [ -t 0 ]; then
  read -r -p "Study id to export (leave blank for ALL app participants): " BRIDGE_STUDY_ID </dev/tty || true
fi

# Select scope: app-wide (default) vs single study.
if [ -n "$BRIDGE_STUDY_ID" ]; then
  search_url="${BRIDGE_BASE_URL}/v5/studies/${BRIDGE_STUDY_ID}/participants/search"
  scope_desc="study '${BRIDGE_STUDY_ID}'"
  OUTPUT_FILE="${OUTPUT_FILE:-participants-${BRIDGE_STUDY_ID}.csv}"
else
  search_url="${BRIDGE_BASE_URL}/v3/participants/search"
  scope_desc="ALL participants in app '${BRIDGE_APP_ID}'"
  OUTPUT_FILE="${OUTPUT_FILE:-participants-${BRIDGE_APP_ID}-all.csv}"
fi

echo "→ Signing in to ${BRIDGE_BASE_URL} as ${BRIDGE_EMAIL} (app: ${BRIDGE_APP_ID})..." >&2

# --- sign in -----------------------------------------------------------------
signin_body="$(
  jq -n --arg appId "$BRIDGE_APP_ID" --arg email "$BRIDGE_EMAIL" --arg password "$BRIDGE_PASSWORD" \
    '{appId: $appId, email: $email, password: $password}'
)"
request POST "${BRIDGE_BASE_URL}/v3/auth/signIn" "" "$signin_body"
signin_resp="$HTTP_BODY"
unset signin_body BRIDGE_PASSWORD  # done with the password

# Bridge uses non-standard sign-in codes. 412 (ConsentRequiredException) means
# authenticated-but-not-consented, but the 412 body still carries a valid
# sessionToken; the search endpoint is role-based, so we accept it and proceed.
case "$HTTP_STATUS" in
  200) ;;
  412)
    echo "warning: account authenticated but not consented (HTTP 412) — proceeding" >&2
    echo "         with the session token from the response." >&2
    ;;
  401) echo "error: authentication failed — wrong email or password (HTTP 401)"      >&2; exit 1 ;;
  403) echo "error: sign-in forbidden — account email/phone not verified (HTTP 403)" >&2; exit 1 ;;
  423) echo "error: account is disabled/locked — contact user support (HTTP 423)"    >&2; exit 1 ;;
  404) echo "error: no such account under app '${BRIDGE_APP_ID}' (HTTP 404)"         >&2; exit 1 ;;
  429) echo "error: rate limited on sign-in — wait and retry (HTTP 429)"             >&2; exit 1 ;;
  *)
    echo "error: sign-in failed (HTTP $HTTP_STATUS)" >&2
    printf '%s' "$signin_resp" | jq -r '.message // .' >&2 2>/dev/null || printf '%s\n' "$signin_resp" >&2
    exit 1 ;;
esac

SESSION_TOKEN="$(printf '%s' "$signin_resp" | jq -r '.sessionToken // empty')"
unset signin_resp
if [ -z "$SESSION_TOKEN" ]; then
  echo "error: signed in (HTTP $HTTP_STATUS) but no session token in response" >&2
  exit 1
fi

# --- page through participants, writing CSV ----------------------------------
# Write to a temp file and move into place at the end, so a mid-run failure
# never leaves a truncated CSV behind. (search_url / scope_desc set above.)
out_tmp="$(mktemp)"
trap 'rm -f "$out_tmp"' EXIT

# CSV header — keep column order in sync with the jq row projection below.
echo 'id,firstName,lastName,email,phone,externalId,status,createdOn,studyIds,dataGroups,synapseUserId,clientTimeZone,note' > "$out_tmp"

echo "→ Fetching ${scope_desc} (page size ${PAGE_SIZE})..." >&2

offset=0
total=""
written=0
while : ; do
  page_req="$(jq -n --argjson pageSize "$PAGE_SIZE" --argjson offsetBy "$offset" \
      '{pageSize: $pageSize, offsetBy: $offsetBy}')"
  request POST "$search_url" "$SESSION_TOKEN" "$page_req"
  page_body="$HTTP_BODY"

  case "$HTTP_STATUS" in
    200) ;;
    403)
      echo "error: forbidden (HTTP 403) — insufficient role for ${scope_desc}." >&2
      if [ -n "$BRIDGE_STUDY_ID" ]; then
        echo "       Need researcher/coordinator/designer/developer on that study." >&2
      else
        echo "       App-wide export needs DEVELOPER or RESEARCHER in the app." >&2
      fi
      printf '%s' "$page_body" | jq -r '.message // empty' >&2 2>/dev/null || true
      exit 1 ;;
    404)
      echo "error: not found (HTTP 404) — study '${BRIDGE_STUDY_ID}' does not exist under" >&2
      echo "       app '${BRIDGE_APP_ID}'." >&2
      exit 1 ;;
    429)
      echo "error: rate limited (HTTP 429) at offset ${offset} — wait and retry." >&2
      exit 1 ;;
    *)
      echo "error: participant search failed at offset ${offset} (HTTP $HTTP_STATUS)" >&2
      printf '%s' "$page_body" | jq -r '.message // .' >&2 2>/dev/null || printf '%s\n' "$page_body" >&2
      exit 1 ;;
  esac

  total="$(printf '%s' "$page_body" | jq -r '.total // 0')"

  # Project each AccountSummary to a CSV row. externalIds is a study->id map:
  # in single-study mode emit that study's id; app-wide, emit all as
  # "study=id" pairs. Array fields join with '|'. @csv handles quoting.
  printf '%s' "$page_body" | jq -r --arg study "$BRIDGE_STUDY_ID" '
    .items[] | [
      .id,
      (.firstName // ""),
      (.lastName // ""),
      (.email // ""),
      (.phone.number // ""),
      (if $study != "" then (.externalIds[$study] // "")
       else ((.externalIds // {}) | to_entries | map("\(.key)=\(.value)") | join("|")) end),
      (.status // ""),
      (.createdOn // ""),
      ((.studyIds // []) | join("|")),
      ((.dataGroups // []) | join("|")),
      (.synapseUserId // ""),
      (.clientTimeZone // ""),
      (.note // "")
    ] | @csv
  ' >> "$out_tmp"

  page_count="$(printf '%s' "$page_body" | jq -r '.items | length')"
  written=$((written + page_count))
  offset=$((offset + PAGE_SIZE))

  echo "  ...${written}/${total}" >&2

  # Stop when we've covered the reported total, or a short page / empty page
  # tells us there's nothing more (guards against an off-by-one on `total`).
  if [ "$offset" -ge "$total" ] || [ "$page_count" -lt "$PAGE_SIZE" ]; then
    break
  fi
done

unset SESSION_TOKEN
mv "$out_tmp" "$OUTPUT_FILE"
trap - EXIT
echo "✓ Wrote ${written} participants to ${OUTPUT_FILE}" >&2
