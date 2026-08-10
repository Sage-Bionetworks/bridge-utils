#!/usr/bin/env python3
"""export-all-participants.py

Export STUDY SUBJECTS from a Bridge app to a rich CSV — the report a study
owner typically wants: participant id, health code, external id, enrollment
dates, sharing scope, consent status, and more. Reproduces (and goes beyond)
the BridgeResearcherUI "export all participants" feature, depending on nothing
but the API.

HOW IT WORKS (two phases):
  1. Page POST /v3/participants/search (app-wide) or the study-scoped
     /v5/studies/{studyId}/participants/search, keeping only STUDY SUBJECTS —
     accounts with an EMPTY roles set. Admin/staff accounts are skipped.
  2. For each subject, GET /v3/participants/{userId}?consents=true (or the
     study-scoped equivalent), returning the full StudyParticipant with
     embedded per-study enrollments and consent histories. One call per
     subject, so this is rate-limited.

OUTPUT SHAPE: one row per (subject x study enrollment). A subject in N studies
yields N rows; a subject with no enrollment yields one row with the study/
enrollment columns blank.

ROLE NOTE: app-wide mode needs RESEARCHER (or ADMIN) to see real participants.
A DEVELOPER passes the endpoint gate but the server silently restricts its
search to test accounts, so app-wide returns ~nothing real for a developer.
STUDY_COORDINATOR can see real participants only via single-study mode
(set BRIDGE_STUDY_ID).

HEALTH CODE: the healthCode column is only populated if the app has
healthCodeExportEnabled=true OR your account is SUPERADMIN; otherwise Bridge
suppresses it and the column is blank (the script warns once).

Configuration comes from environment variables; anything missing is prompted
for interactively (password input is hidden):
  BRIDGE_BASE_URL   API root (default: https://webservices.sagebridge.org)
  BRIDGE_APP_ID     Bridge app you sign in under
  BRIDGE_EMAIL      Your Bridge account email
  BRIDGE_PASSWORD   Your Bridge account password              (hidden prompt)
  BRIDGE_STUDY_ID   Optional: narrow to one study. Blank = ALL app subjects.
  OUTPUT_FILE       CSV path (default: study-subjects-<app|study>.csv)
  REQUEST_DELAY     Seconds between per-subject calls (default: 0.1)

Usage:
  BRIDGE_APP_ID=my-app ./export-all-participants.py
  BRIDGE_APP_ID=my-app BRIDGE_STUDY_ID=my-study ./export-all-participants.py
"""

import csv
import datetime
import getpass
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE_URL = "https://webservices.sagebridge.org"
PAGE_SIZE = 100  # server-enforced maximum

COLUMNS = [
    "id", "healthCode", "studyId", "externalId", "enrolledOn", "withdrawnOn",
    "enrolledBySelf", "withdrawnBySelf", "consentRequired", "consented",
    "sharingScope", "email", "firstName", "lastName", "phone", "status",
    "createdOn", "dataGroups", "languages", "allStudyIds", "consentSummary",
    "notifyByEmail", "synapseUserId", "clientTimeZone", "orgMembership", "note",
]


def err(*args):
    print(*args, file=sys.stderr)


def die(msg):
    err("error: " + msg)
    sys.exit(1)


# --- config ------------------------------------------------------------------
def prompt_value(env, label, required=True):
    val = os.environ.get(env, "").strip()
    if not val and sys.stdin.isatty():
        val = input(f"{label}: ").strip()
    if required and not val:
        die(f"{env} is required")
    return val


def prompt_secret(env, label):
    val = os.environ.get(env, "")  # do not strip — passwords are literal
    if not val and sys.stdin.isatty():
        val = getpass.getpass(f"{label}: ")
    if not val:
        die(f"{env} is required")
    return val


# --- HTTP --------------------------------------------------------------------
def request(method, url, token=None, body=None):
    """Return (status_code, parsed_json_or_None, raw_text). Never raises on a
    non-2xx status — the caller inspects the code (Bridge uses several
    non-standard ones)."""
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Bridge-Session"] = token
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            status = resp.getcode()
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        status = e.code
        raw = e.read().decode("utf-8", "replace")
    except urllib.error.URLError as e:
        die(f"network failure contacting {url}: {e.reason}")
    parsed = None
    if raw:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = None
    return status, parsed, raw


def server_message(parsed, raw):
    if isinstance(parsed, dict) and parsed.get("message"):
        return parsed["message"]
    return raw.strip()


# --- value formatting (mirrors the jq transform in the shell version) --------
def ts(v):
    """Epoch-ms number -> ISO-8601 UTC; pass strings through; None -> ''."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, (int, float)):
        dt = datetime.datetime.fromtimestamp(v / 1000, tz=datetime.timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(v)


def cell(v):
    """None -> ''; booleans -> lowercase 'true'/'false' (matching jq @csv)."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    return v


def consent_summary(p):
    parts = []
    for guid, hist in (p.get("consentHistories") or {}).items():
        h = hist[-1] if hist else {}
        active = "true" if h.get("hasSignedActiveConsent") else "false"
        parts.append(
            f"{guid}:active={active};signedOn={ts(h.get('signedOn'))};"
            f"withdrewOn={ts(h.get('withdrewOn'))}"
        )
    return " || ".join(parts)


def rows_for(p, study_id):
    """One row per (subject x enrollment). No enrollments -> one blank-study row.
    In single-study mode, keep only the target study's enrollment."""
    enr = p.get("enrollments") or {}
    entries = list(enr.items()) if enr else [("", {})]
    if study_id:
        entries = [(k, v) for (k, v) in entries if k == study_id]
    ext_ids = p.get("externalIds") or {}
    phone = (p.get("phone") or {}).get("number", "") or ""
    study_ids = p.get("studyIds") or p.get("substudyIds") or []
    rows = []
    for skey, v in entries:
        v = v or {}
        ext = v.get("externalId") or ext_ids.get(skey) or ""
        rows.append([
            p.get("id", "") or "",
            p.get("healthCode", "") or "",
            skey,
            ext,
            ts(v.get("enrolledOn")),
            ts(v.get("withdrawnOn")),
            cell(v.get("enrolledBySelf")),
            cell(v.get("withdrawnBySelf")),
            cell(v.get("consentRequired")),
            cell(p.get("consented")),
            p.get("sharingScope", "") or "",
            p.get("email", "") or "",
            p.get("firstName", "") or "",
            p.get("lastName", "") or "",
            phone,
            p.get("status", "") or "",
            ts(p.get("createdOn")),
            "|".join(p.get("dataGroups") or []),
            "|".join(p.get("languages") or []),
            "|".join(study_ids),
            consent_summary(p),
            cell(p.get("notifyByEmail")),
            p.get("synapseUserId", "") or "",
            p.get("clientTimeZone", "") or "",
            p.get("orgMembership", "") or "",
            p.get("note", "") or "",
        ])
    return rows


# --- main --------------------------------------------------------------------
def main():
    base = os.environ.get("BRIDGE_BASE_URL", "").strip() or DEFAULT_BASE_URL
    base = base.rstrip("/")
    try:
        delay = float(os.environ.get("REQUEST_DELAY", "0.1"))
    except ValueError:
        die("REQUEST_DELAY must be a number of seconds")

    app_id = prompt_value("BRIDGE_APP_ID", "Bridge app id")
    email = prompt_value("BRIDGE_EMAIL", "Bridge account email")
    password = prompt_secret("BRIDGE_PASSWORD", "Bridge account password")

    study_id = os.environ.get("BRIDGE_STUDY_ID", "").strip()
    if not study_id and sys.stdin.isatty():
        study_id = input("Study id (leave blank for ALL app subjects): ").strip()

    if study_id:
        search_url = f"{base}/v5/studies/{urllib.parse.quote(study_id)}/participants/search"
        detail_base = f"{base}/v5/studies/{urllib.parse.quote(study_id)}/participants"
        scope_desc = f"study '{study_id}'"
        default_out = f"study-subjects-{study_id}.csv"
    else:
        search_url = f"{base}/v3/participants/search"
        detail_base = f"{base}/v3/participants"
        scope_desc = f"ALL subjects in app '{app_id}'"
        default_out = f"study-subjects-{app_id}-all.csv"
    output_file = os.environ.get("OUTPUT_FILE", "").strip() or default_out

    # --- sign in ---
    err(f"-> Signing in to {base} as {email} (app: {app_id})...")
    status, body, raw = request(
        "POST", f"{base}/v3/auth/signIn",
        body={"appId": app_id, "email": email, "password": password},
    )
    password = None  # done with it
    if status == 200:
        pass
    elif status == 412:
        err("warning: account authenticated but not consented (HTTP 412) — "
            "proceeding with the session token from the response.")
    elif status == 401:
        die("authentication failed — wrong email or password (HTTP 401)")
    elif status == 403:
        die("sign-in forbidden — account email/phone not verified (HTTP 403)")
    elif status == 423:
        die("account is disabled/locked — contact user support (HTTP 423)")
    elif status == 404:
        die(f"no such account under app '{app_id}' (HTTP 404)")
    elif status == 429:
        die("rate limited on sign-in — wait and retry (HTTP 429)")
    else:
        die(f"sign-in failed (HTTP {status}): {server_message(body, raw)}")

    token = body.get("sessionToken") if isinstance(body, dict) else None
    if not token:
        die(f"signed in (HTTP {status}) but no session token in response")

    # --- phase 1: collect subject ids (empty roles only) ---
    err(f"-> Phase 1: listing {scope_desc} and filtering to study subjects...")
    subjects = []
    seen = 0
    offset = 0
    total = 0
    while True:
        status, body, raw = request(
            "POST", search_url, token=token,
            body={"pageSize": PAGE_SIZE, "offsetBy": offset},
        )
        if status == 403:
            need = ("researcher/coordinator/designer/developer on that study"
                    if study_id else
                    "RESEARCHER (or ADMIN) in the app — DEVELOPER only sees test accounts")
            die(f"forbidden (HTTP 403) listing {scope_desc}. Need {need}.")
        if status == 404:
            die(f"not found (HTTP 404) — study '{study_id}' does not exist "
                f"under app '{app_id}'.")
        if status == 429:
            die(f"rate limited (HTTP 429) at offset {offset} — wait and retry.")
        if status != 200:
            die(f"participant search failed at offset {offset} "
                f"(HTTP {status}): {server_message(body, raw)}")

        items = body.get("items", []) if isinstance(body, dict) else []
        total = body.get("total", 0) if isinstance(body, dict) else 0
        for it in items:
            if not it.get("roles"):  # empty/absent roles => study subject
                subjects.append(it["id"])
        seen += len(items)
        offset += PAGE_SIZE
        err(f"  ...scanned {seen}/{total} accounts, {len(subjects)} subjects so far")
        if offset >= total or len(items) < PAGE_SIZE:
            break

    admins_skipped = seen - len(subjects)
    err(f"-> {len(subjects)} study subjects ({admins_skipped} admin/staff skipped).")
    if not subjects:
        if not study_id:
            err("Nothing to export. If your account is DEVELOPER-only, this is "
                "expected — the server restricts developer searches to test "
                "accounts. Use a RESEARCHER account for app-wide export, or set "
                "BRIDGE_STUDY_ID with a STUDY_COORDINATOR role.")
        else:
            err("Nothing to export.")
        return

    # --- phase 2: fetch detail per subject, write CSV ---
    err(f"-> Phase 2: fetching detail for {len(subjects)} subjects "
        f"(delay {delay}s)...")
    out_dir = os.path.dirname(os.path.abspath(output_file)) or "."
    fd, tmp_path = tempfile.mkstemp(prefix=".export-", suffix=".csv", dir=out_dir)
    failed = 0
    checked_hc = False
    rows_written = 0
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(COLUMNS)
            for i, sid in enumerate(subjects, start=1):
                url = f"{detail_base}/{urllib.parse.quote(sid)}?consents=true"
                status, detail, raw = request("GET", url, token=token)
                if status != 200:
                    err(f"  warning: skipping subject {sid} (HTTP {status})")
                    failed += 1
                    continue

                if not checked_hc:
                    checked_hc = True
                    if not (isinstance(detail, dict) and detail.get("healthCode")):
                        err("  note: healthCode is empty — the app likely has "
                            "healthCodeExportEnabled=false and you are not "
                            "SUPERADMIN, so Bridge suppresses it. Other fields "
                            "are unaffected.")

                for row in rows_for(detail, study_id):
                    writer.writerow(row)
                    rows_written += 1

                if i % 25 == 0:
                    err(f"  ...{i}/{len(subjects)}")
                if delay > 0:
                    time.sleep(delay)
        os.replace(tmp_path, output_file)
    except BaseException:
        # Never leave a partial CSV behind on error / interrupt.
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise

    token = None
    err(f"OK: wrote {rows_written} enrollment rows for {len(subjects)} subjects "
        f"to {output_file}")
    if failed:
        err(f"  ({failed} subject(s) could not be fetched and were skipped — "
            "see warnings above)")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        err("\ninterrupted")
        sys.exit(130)
