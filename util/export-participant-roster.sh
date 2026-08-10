#!/usr/bin/env bash
#
# export-participant-roster.sh
#
# Triggers the Bridge "participant roster" export for a study — the same
# asynchronous job the BridgeResearcherUI "export all participants" button is
# supposed to fire. Bridge builds a password-protected CSV zip and emails a
# pre-signed S3 download link (valid ~3 days) to the signed-in account.
#
# Requirements:
#   - curl and jq on PATH
#   - Your account must have a VERIFIED email and the STUDY_COORDINATOR role
#     on the target study (app-scoped fallback needs RESEARCHER).
#
# No secrets are hard-coded. Values come from environment variables; anything
# missing is prompted for interactively (password input is hidden). Request
# bodies are built with jq and streamed over stdin, so credentials never appear
# in `ps` output or your shell history.
#
# Environment variables (all optional — you'll be prompted for any that are unset):
#   BRIDGE_BASE_URL      API root (default: https://webservices.sagebridge.org)
#   BRIDGE_APP_ID        Bridge app/study identifier you sign in under
#   BRIDGE_EMAIL         Your Bridge account email
#   BRIDGE_PASSWORD      Your Bridge account password        (hidden prompt)
#   BRIDGE_STUDY_ID      The study whose participants to export
#   ROSTER_ZIP_PASSWORD  Password that will protect the emailed zip (hidden prompt)
#
# Usage:
#   ./export-participant-roster.sh
#   BRIDGE_APP_ID=my-app BRIDGE_STUDY_ID=my-study ./export-participant-roster.sh
#
# For non-interactive / CI use, export all six vars beforehand (ideally sourced
# from a secret manager, not written to disk).

set -euo pipefail

# --- dependency check --------------------------------------------------------
for cmd in curl jq; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "error: required command '$cmd' not found on PATH" >&2
    exit 1
  fi
done

# --- helpers -----------------------------------------------------------------
# prompt_value VAR_NAME "Prompt text"        -> visible input
# prompt_secret VAR_NAME "Prompt text"       -> hidden input
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

# The server validates the zip password against PasswordPolicy(8, true, false,
# true, true): >=8 chars, a digit, a lowercase letter, an uppercase letter
# (symbol NOT required). Enforce it here so a weak password fails fast with a
# clear message instead of a confusing server-side 400.
validate_zip_password() {
  # Use POSIX character classes, not ranges like [A-Z]: in many locales a range
  # follows collation order (aAbB...) and would match the wrong case.
  local pw="$1"; local -a missing=()
  [ "${#pw}" -ge 8 ]           || missing+=("at least 8 characters")
  [[ "$pw" == *[[:digit:]]* ]] || missing+=("a digit")
  [[ "$pw" == *[[:lower:]]* ]] || missing+=("a lowercase letter")
  [[ "$pw" == *[[:upper:]]* ]] || missing+=("an uppercase letter")
  if [ "${#missing[@]}" -gt 0 ]; then
    echo "error: the zip password does not meet Bridge's policy. It must contain:" >&2
    local m; for m in "${missing[@]}"; do echo "         - $m" >&2; done
    exit 1
  fi
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
prompt_value  BRIDGE_APP_ID       "Bridge app id"
prompt_value  BRIDGE_EMAIL        "Bridge account email"
prompt_secret BRIDGE_PASSWORD     "Bridge account password"
prompt_value  BRIDGE_STUDY_ID     "Study id to export"
prompt_secret ROSTER_ZIP_PASSWORD "Password to protect the emailed zip"
validate_zip_password "$ROSTER_ZIP_PASSWORD"

echo "→ Signing in to ${BRIDGE_BASE_URL} as ${BRIDGE_EMAIL} (app: ${BRIDGE_APP_ID})..." >&2

# --- sign in -----------------------------------------------------------------
signin_body="$(
  jq -n --arg appId "$BRIDGE_APP_ID" --arg email "$BRIDGE_EMAIL" --arg password "$BRIDGE_PASSWORD" \
    '{appId: $appId, email: $email, password: $password}'
)"

request POST "${BRIDGE_BASE_URL}/v3/auth/signIn" "" "$signin_body"
signin_resp="$HTTP_BODY"
unset signin_body BRIDGE_PASSWORD  # done with the password

# Bridge uses several non-standard sign-in status codes. The important one is
# 412 (ConsentRequiredException): the account is authenticated but not consented
# to a study, yet the 412 body STILL carries a valid sessionToken. Roster export
# is role-based and does not require consent, so we accept that token and proceed.
case "$HTTP_STATUS" in
  200) ;;
  412)
    echo "warning: account authenticated but not consented (HTTP 412) — proceeding" >&2
    echo "         with the session token from the response (roster export is" >&2
    echo "         role-based and does not require consent)." >&2
    ;;
  401) echo "error: authentication failed — wrong email or password (HTTP 401)"        >&2; exit 1 ;;
  403) echo "error: sign-in forbidden — account email/phone not verified (HTTP 403)"   >&2; exit 1 ;;
  423) echo "error: account is disabled/locked — contact user support (HTTP 423)"      >&2; exit 1 ;;
  404) echo "error: no such account under app '${BRIDGE_APP_ID}' (HTTP 404)"           >&2; exit 1 ;;
  429) echo "error: rate limited on sign-in — wait and retry (HTTP 429)"               >&2; exit 1 ;;
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

# --- request the roster ------------------------------------------------------
echo "→ Requesting participant roster for study '${BRIDGE_STUDY_ID}'..." >&2

roster_body="$(jq -n --arg password "$ROSTER_ZIP_PASSWORD" '{password: $password}')"
unset ROSTER_ZIP_PASSWORD

roster_url="${BRIDGE_BASE_URL}/v5/studies/${BRIDGE_STUDY_ID}/participants/emailRoster"
request POST "$roster_url" "$SESSION_TOKEN" "$roster_body"
roster_resp="$HTTP_BODY"
unset roster_body SESSION_TOKEN

case "$HTTP_STATUS" in
  202)
    echo "✓ Roster export accepted (HTTP 202)." >&2
    echo "  $(printf '%s' "$roster_resp" | jq -r '.message // "Preparing participant roster."')" >&2
    echo "  A password-protected CSV zip will be emailed to ${BRIDGE_EMAIL} shortly" >&2
    echo "  (download link valid ~3 days). Unzip it with the zip password you entered." >&2
    ;;
  400)
    echo "error: bad request (HTTP 400). Likely one of:" >&2
    echo "       - the requesting account ('${BRIDGE_EMAIL}') has no VERIFIED email address" >&2
    echo "         (the roster is emailed to you, so your account must be able to receive it)" >&2
    echo "       - the zip password was rejected by policy (8+ chars, upper, lower, digit)" >&2
    echo "       server said:" >&2
    printf '%s' "$roster_resp" | jq -r '.message // .' >&2 2>/dev/null || printf '%s\n' "$roster_resp" >&2
    exit 1
    ;;
  403)
    echo "error: forbidden (HTTP 403) — your account needs the STUDY_COORDINATOR role" >&2
    echo "       on study '${BRIDGE_STUDY_ID}' to export its roster." >&2
    printf '%s' "$roster_resp" | jq -r '.message // empty' >&2 2>/dev/null || true
    exit 1
    ;;
  404)
    echo "error: not found (HTTP 404) — study '${BRIDGE_STUDY_ID}' does not exist under" >&2
    echo "       app '${BRIDGE_APP_ID}'." >&2
    printf '%s' "$roster_resp" | jq -r '.message // empty' >&2 2>/dev/null || true
    exit 1
    ;;
  429)
    echo "error: rate limited (HTTP 429) — wait a bit and retry." >&2
    exit 1
    ;;
  *)
    echo "error: roster request failed (HTTP $HTTP_STATUS)" >&2
    printf '%s' "$roster_resp" | jq -r '.message // .' >&2 2>/dev/null || printf '%s\n' "$roster_resp" >&2
    exit 1
    ;;
esac
