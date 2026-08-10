#!/usr/bin/env bash
#
# export-study-subjects.sh
#
# Exports STUDY SUBJECTS from a Bridge app to a rich CSV — the report a study
# owner typically wants: participant id, health code, external/participant id,
# enrollment dates, sharing scope, consent status, and more. Reproduces (and
# goes well beyond) the BridgeResearcherUI "export all participants" feature,
# depending on nothing but the API.
#
# HOW IT WORKS (two phases):
#   1. Page POST /v3/participants/search (app-wide) or the study-scoped
#      /v5/studies/{studyId}/participants/search, and keep only STUDY SUBJECTS
#      — accounts with an EMPTY roles set. Admin/staff accounts (any of
#      DEVELOPER/RESEARCHER/STUDY_COORDINATOR/STUDY_DESIGNER/ORG_ADMIN/ADMIN/
#      WORKER/SUPERADMIN) are skipped.
#   2. For each subject, GET /v3/participants/{userId}?consents=true (or the
#      study-scoped equivalent), which returns the full StudyParticipant with
#      embedded per-study enrollments and consent histories. One call per
#      subject — so this is much slower than a summary pull and is rate-limited.
#
# OUTPUT SHAPE: one row per (subject × study enrollment). A subject enrolled in
# N studies yields N rows; a subject with no enrollment yields one row with the
# study/enrollment columns blank. Participant-level columns repeat across a
# subject's rows.
#
# HEALTH CODE: the healthCode column is only populated if the app has
# healthCodeExportEnabled=true OR your account is SUPERADMIN; otherwise Bridge
# suppresses it server-side and the column is blank (the script warns once).
#
# Requirements:
#   - curl and jq on PATH
#   - App-wide: DEVELOPER or RESEARCHER in the app (search) + rights to read
#     participants. Single-study: coordinator/researcher/designer/developer on
#     the study.
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
#   BRIDGE_STUDY_ID   Optional: narrow to one study. Blank = ALL app subjects.
#   OUTPUT_FILE       CSV path (default: study-subjects-<app|study>.csv)
#   REQUEST_DELAY     Seconds to pause between per-subject calls (default: 0.1)
#
# Usage:
#   BRIDGE_APP_ID=my-app ./export-study-subjects.sh
#   BRIDGE_APP_ID=my-app BRIDGE_STUDY_ID=my-study ./export-study-subjects.sh

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
REQUEST_DELAY="${REQUEST_DELAY:-0.1}"
prompt_value  BRIDGE_APP_ID   "Bridge app id"
prompt_value  BRIDGE_EMAIL    "Bridge account email"
prompt_secret BRIDGE_PASSWORD "Bridge account password"

BRIDGE_STUDY_ID="${BRIDGE_STUDY_ID:-}"
if [ -z "$BRIDGE_STUDY_ID" ] && [ -t 0 ]; then
  read -r -p "Study id (leave blank for ALL app subjects): " BRIDGE_STUDY_ID </dev/tty || true
fi

if [ -n "$BRIDGE_STUDY_ID" ]; then
  search_url="${BRIDGE_BASE_URL}/v5/studies/${BRIDGE_STUDY_ID}/participants/search"
  detail_base="${BRIDGE_BASE_URL}/v5/studies/${BRIDGE_STUDY_ID}/participants"
  scope_desc="study '${BRIDGE_STUDY_ID}'"
  OUTPUT_FILE="${OUTPUT_FILE:-study-subjects-${BRIDGE_STUDY_ID}.csv}"
else
  search_url="${BRIDGE_BASE_URL}/v3/participants/search"
  detail_base="${BRIDGE_BASE_URL}/v3/participants"
  scope_desc="ALL subjects in app '${BRIDGE_APP_ID}'"
  OUTPUT_FILE="${OUTPUT_FILE:-study-subjects-${BRIDGE_APP_ID}-all.csv}"
fi

echo "→ Signing in to ${BRIDGE_BASE_URL} as ${BRIDGE_EMAIL} (app: ${BRIDGE_APP_ID})..." >&2

# --- sign in -----------------------------------------------------------------
signin_body="$(
  jq -n --arg appId "$BRIDGE_APP_ID" --arg email "$BRIDGE_EMAIL" --arg password "$BRIDGE_PASSWORD" \
    '{appId: $appId, email: $email, password: $password}'
)"
request POST "${BRIDGE_BASE_URL}/v3/auth/signIn" "" "$signin_body"
signin_resp="$HTTP_BODY"
unset signin_body BRIDGE_PASSWORD

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

# --- phase 1: page search, collect subject ids (empty roles only) ------------
echo "→ Phase 1: listing ${scope_desc} and filtering to study subjects..." >&2

subjects=()
seen=0
offset=0
total=0
while : ; do
  page_req="$(jq -n --argjson pageSize "$PAGE_SIZE" --argjson offsetBy "$offset" \
      '{pageSize: $pageSize, offsetBy: $offsetBy}')"
  request POST "$search_url" "$SESSION_TOKEN" "$page_req"
  page_body="$HTTP_BODY"
  case "$HTTP_STATUS" in
    200) ;;
    403)
      echo "error: forbidden (HTTP 403) listing ${scope_desc}." >&2
      if [ -n "$BRIDGE_STUDY_ID" ]; then
        echo "       Need researcher/coordinator/designer/developer on that study." >&2
      else
        echo "       App-wide export needs DEVELOPER or RESEARCHER in the app." >&2
      fi
      printf '%s' "$page_body" | jq -r '.message // empty' >&2 2>/dev/null || true
      exit 1 ;;
    404)
      echo "error: not found (HTTP 404) — study '${BRIDGE_STUDY_ID}' does not exist" >&2
      echo "       under app '${BRIDGE_APP_ID}'." >&2
      exit 1 ;;
    429) echo "error: rate limited (HTTP 429) at offset ${offset} — wait and retry." >&2; exit 1 ;;
    *)
      echo "error: participant search failed at offset ${offset} (HTTP $HTTP_STATUS)" >&2
      printf '%s' "$page_body" | jq -r '.message // .' >&2 2>/dev/null || printf '%s\n' "$page_body" >&2
      exit 1 ;;
  esac

  total="$(printf '%s' "$page_body" | jq -r '.total // 0')"
  page_count="$(printf '%s' "$page_body" | jq -r '.items | length')"

  # A study subject has an empty roles set.
  while IFS= read -r sid; do
    [ -n "$sid" ] && subjects+=("$sid")
  done < <(printf '%s' "$page_body" | jq -r '.items[] | select((.roles // []) | length == 0) | .id')

  seen=$((seen + page_count))
  offset=$((offset + PAGE_SIZE))
  echo "  ...scanned ${seen}/${total} accounts, ${#subjects[@]} subjects so far" >&2
  if [ "$offset" -ge "$total" ] || [ "$page_count" -lt "$PAGE_SIZE" ]; then
    break
  fi
done

subject_count=${#subjects[@]}
admins_skipped=$((seen - subject_count))
echo "→ ${subject_count} study subjects (${admins_skipped} admin/staff accounts skipped)." >&2
if [ "$subject_count" -eq 0 ]; then
  echo "Nothing to export." >&2
  exit 0
fi

# --- phase 2: fetch each subject's detail, write CSV -------------------------
# jq transform: one row per (subject × enrollment). See prototype in review.
PROJECTION='
def ts: if . == null then "" elif type=="number" then ((./1000)|floor|todate) else tostring end;
def E: if . == null then "" else . end;
def consentSummary:
  (.consentHistories // {}) | to_entries
  | map(
      .key as $g
      | (.value | last) as $h
      | "\($g):active=\(($h|.hasSignedActiveConsent)//false);signedOn=\(($h|.signedOn)|ts);withdrewOn=\(($h|.withdrewOn)|ts)"
    )
  | join(" || ");
. as $p
| ($p.enrollments // {}) as $enr
| (if ($enr|length) > 0 then ($enr|to_entries) else [{key:"",value:{}}] end)
| (if $study != "" then map(select(.key==$study)) else . end)
| .[]
| (.value) as $v
| [
    $p.id, ($p.healthCode // ""), .key,
    ($v.externalId // ($p.externalIds[.key]) // ""),
    ($v.enrolledOn | ts), ($v.withdrawnOn | ts),
    ($v.enrolledBySelf | E), ($v.withdrawnBySelf | E), ($v.consentRequired | E),
    ($p.consented | E), ($p.sharingScope // ""),
    ($p.email // ""), ($p.firstName // ""), ($p.lastName // ""), ($p.phone.number // ""),
    ($p.status // ""), ($p.createdOn | ts),
    (($p.dataGroups // []) | join("|")), (($p.languages // []) | join("|")),
    (($p.studyIds // []) | join("|")),
    ($p | consentSummary),
    ($p.notifyByEmail | E), ($p.synapseUserId // ""), ($p.clientTimeZone // ""),
    ($p.orgMembership // ""), ($p.note // "")
  ] | @csv'

out_tmp="$(mktemp)"
trap 'rm -f "$out_tmp"' EXIT
echo 'id,healthCode,studyId,externalId,enrolledOn,withdrawnOn,enrolledBySelf,withdrawnBySelf,consentRequired,consented,sharingScope,email,firstName,lastName,phone,status,createdOn,dataGroups,languages,allStudyIds,consentSummary,notifyByEmail,synapseUserId,clientTimeZone,orgMembership,note' > "$out_tmp"

echo "→ Phase 2: fetching detail for ${subject_count} subjects (delay ${REQUEST_DELAY}s)..." >&2

i=0
failed=0
checked_hc=0
for sid in "${subjects[@]}"; do
  i=$((i + 1))
  request GET "${detail_base}/${sid}?consents=true" "$SESSION_TOKEN"
  detail="$HTTP_BODY"
  if [ "$HTTP_STATUS" != "200" ]; then
    echo "  warning: skipping subject ${sid} (HTTP ${HTTP_STATUS})" >&2
    failed=$((failed + 1))
    continue
  fi

  # One-time notice if health codes are being suppressed server-side.
  if [ "$checked_hc" -eq 0 ]; then
    checked_hc=1
    if [ -z "$(printf '%s' "$detail" | jq -r '.healthCode // ""')" ]; then
      echo "  note: healthCode is empty — the app likely has healthCodeExportEnabled=false" >&2
      echo "        and you are not SUPERADMIN, so Bridge suppresses it. Other fields are unaffected." >&2
    fi
  fi

  printf '%s' "$detail" | jq -r --arg study "$BRIDGE_STUDY_ID" "$PROJECTION" >> "$out_tmp"

  if [ $((i % 25)) -eq 0 ]; then
    echo "  ...${i}/${subject_count}" >&2
  fi
  # Politeness pause between calls (skip if set to 0).
  if [ "$REQUEST_DELAY" != "0" ]; then
    sleep "$REQUEST_DELAY"
  fi
done

unset SESSION_TOKEN
rows=$(($(wc -l < "$out_tmp") - 1))
mv "$out_tmp" "$OUTPUT_FILE"
trap - EXIT

echo "✓ Wrote ${rows} enrollment rows for ${subject_count} subjects to ${OUTPUT_FILE}" >&2
if [ "$failed" -gt 0 ]; then
  echo "  (${failed} subject(s) could not be fetched and were skipped — see warnings above)" >&2
fi
