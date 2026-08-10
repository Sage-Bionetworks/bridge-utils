#!/usr/bin/env python3
"""get-consents.py

Export signed consent data for a Bridge study.

For every STUDY SUBJECT (account with an empty roles set) in the target
study: read their consent histories, and for each *actively signed*
consent, write a row to a signature CSV (participant id, signed name,
signed-on date, sharing scope, and which consent template version they
signed) and save that template's document content to disk (once per
template version, not once per participant).

Depends on nothing but the stdlib (urllib/csv/json) — no bridgeclient, no
pandas.

HOW IT WORKS (two phases, same shape as export-all-participants.py):
  1. Page POST /v5/studies/{studyId}/participants/search, keeping only
     STUDY SUBJECTS — accounts with an EMPTY roles set. Admin/staff
     accounts are skipped.
  2. For each subject, GET /v5/studies/{studyId}/participants/{userId}
     ?consents=true, returning the full StudyParticipant with embedded
     consent histories. Skip anyone not consented. For each actively
     signed consent, fetch its template document (once per distinct
     version) from /v3/subpopulations/{guid}/consents/{createdOn}.

ROLE NOTE: this script needs TWO separate roles, checked independently:
  - Listing/reading study subjects (phase 1 + phase 2 detail GET) needs
    STUDY_COORDINATOR or STUDY_DESIGNER scoped to the study, or a global
    DEVELOPER/RESEARCHER/ADMIN/SUPERADMIN role.
  - Fetching each consent template's document content
    (/v3/subpopulations/{guid}/consents/{createdOn}) needs a global
    DEVELOPER, ADMIN, or SUPERADMIN role — STUDY_COORDINATOR /
    STUDY_DESIGNER / RESEARCHER alone are NOT enough. A coordinator-only
    account will successfully list subjects and their signature data, but
    every template fetch will 403 and result_count['template_fetch_failed']
    will climb for each distinct template version (the signature CSV is
    still written either way, just without confirming template content).

Configuration comes from environment variables; anything missing is
prompted for interactively (password input is hidden):
  BRIDGE_BASE_URL    API root (default: https://webservices.sagebridge.org)
  BRIDGE_APP_ID      Bridge app you sign in under
  BRIDGE_EMAIL       Your Bridge account email
  BRIDGE_PASSWORD    Your Bridge account password              (hidden prompt)
  BRIDGE_STUDY_ID    Study id to export consents for
  BRIDGE_SUBPOPULATION_GUID
                     Subpopulation whose consent templates to fetch
                     (default: same as BRIDGE_APP_ID, which is correct for
                     apps with a single default subpopulation)
  OUTPUT_DIR         Directory to write the CSV and template files into
                     (default: current directory; created if missing)
  REQUEST_DELAY      Seconds between per-subject calls (default: 0.1)

Usage:
  BRIDGE_APP_ID=my-app BRIDGE_STUDY_ID=my-study ./get-consents.py

Output:
  <OUTPUT_DIR>/<study>-signature-data.csv   one row per active signed consent
  <OUTPUT_DIR>/<study>-<templateCreatedOn>  consent template document content,
                                             one file per distinct template version
"""

import csv
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
    val = os.environ.get(env, "")  # do not strip -- passwords are literal
    if not val and sys.stdin.isatty():
        val = getpass.getpass(f"{label}: ")
    if not val:
        die(f"{env} is required")
    return val


# --- HTTP --------------------------------------------------------------------
def request(method, url, token=None, body=None):
    """Return (status_code, parsed_json_or_None, raw_text). Never raises on a
    non-2xx status -- the caller inspects the code (Bridge uses several
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
    study_id = prompt_value("BRIDGE_STUDY_ID", "Study id to export consents for")
    subpopulation_guid = os.environ.get("BRIDGE_SUBPOPULATION_GUID", "").strip() or app_id
    output_dir = os.environ.get("OUTPUT_DIR", "").strip() or "."

    try:
        os.makedirs(output_dir, exist_ok=True)
    except OSError as exc:
        die(f"could not create OUTPUT_DIR '{output_dir}': {exc}")

    search_url = f"{base}/v5/studies/{urllib.parse.quote(study_id)}/participants/search"
    detail_base = f"{base}/v5/studies/{urllib.parse.quote(study_id)}/participants"

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
        err("warning: account authenticated but not consented (HTTP 412) -- "
            "proceeding with the session token from the response.")
    elif status == 401:
        die("authentication failed -- wrong email or password (HTTP 401)")
    elif status == 403:
        die("sign-in forbidden -- account email/phone not verified (HTTP 403)")
    elif status == 423:
        die("account is disabled/locked -- contact user support (HTTP 423)")
    elif status == 404:
        die(f"no such account under app '{app_id}' (HTTP 404)")
    elif status == 429:
        die("rate limited on sign-in -- wait and retry (HTTP 429)")
    else:
        die(f"sign-in failed (HTTP {status}): {server_message(body, raw)}")

    token = body.get("sessionToken") if isinstance(body, dict) else None
    if not token:
        die(f"signed in (HTTP {status}) but no session token in response")

    # --- phase 1: collect subject ids (empty roles only) ---
    err(f"-> Phase 1: listing study '{study_id}' and filtering to study subjects...")
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
            die(f"forbidden (HTTP 403) listing study '{study_id}'. Need "
                "researcher/coordinator/designer/developer on that study.")
        if status == 404:
            die(f"not found (HTTP 404) -- study '{study_id}' does not exist "
                f"under app '{app_id}'.")
        if status == 429:
            die(f"rate limited (HTTP 429) at offset {offset} -- wait and retry.")
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
        err("Nothing to export.")
        return

    # --- phase 2: fetch detail per subject, collect consents ---
    err(f"-> Phase 2: fetching detail for {len(subjects)} subjects "
        f"(delay {delay}s)...")

    result_csv = [
        ["ParticipantID", "SignedName", "SignedOnDate", "SharingScope", "TemplateCreatedOn"],
    ]
    consent_templates = {}
    result_count = {
        "not_consented": 0,
        "unknown_consented": 0,
        "active_consent_rows": 0,
        "inactive_consent_rows": 0,
        "template_fetch_failed": 0,
        "row_build_failed": 0,
        "detail_fetch_failed": 0,
    }

    for i, sid in enumerate(subjects, start=1):
        url = f"{detail_base}/{urllib.parse.quote(sid)}?consents=true"
        status, detail, raw = request("GET", url, token=token)
        if status != 200:
            err(f"  warning: skipping subject {sid} (HTTP {status})")
            result_count["detail_fetch_failed"] += 1
            continue

        if not isinstance(detail, dict) or "consented" not in detail:
            result_count["unknown_consented"] += 1
            continue
        if not detail["consented"]:
            result_count["not_consented"] += 1
            continue

        for history in (detail.get("consentHistories") or {}).values():
            for consent in history:
                if not consent.get("hasSignedActiveConsent"):
                    result_count["inactive_consent_rows"] += 1
                    continue

                created_on = consent["consentCreatedOn"]
                if created_on not in consent_templates:
                    tstatus, template_body, template_raw = request(
                        "GET",
                        f"{base}/v3/subpopulations/{urllib.parse.quote(subpopulation_guid)}"
                        f"/consents/{urllib.parse.quote(created_on)}",
                        token=token,
                    )
                    if tstatus == 200 and isinstance(template_body, dict):
                        consent_templates[created_on] = template_body.get("documentContent", "")
                    elif tstatus == 403:
                        err(f"  warning: forbidden (HTTP 403) fetching consent template "
                            f"{created_on} — this step needs a global DEVELOPER/ADMIN/"
                            f"SUPERADMIN role; STUDY_COORDINATOR/STUDY_DESIGNER/RESEARCHER "
                            f"alone are not enough. The signature CSV will still be written, "
                            f"just without this template's document content.")
                        result_count["template_fetch_failed"] += 1
                    else:
                        err(f"  warning: could not fetch consent template {created_on} "
                            f"(HTTP {tstatus}): {server_message(template_body, template_raw)}")
                        result_count["template_fetch_failed"] += 1

                try:
                    result_csv.append([
                        sid,
                        consent["name"],
                        consent["signedOn"],
                        detail["sharingScope"],
                        created_on,
                    ])
                    result_count["active_consent_rows"] += 1
                except KeyError as exc:
                    err(f"  warning: could not build CSV row for {sid}: missing {exc}")
                    result_count["row_build_failed"] += 1

        if i % 25 == 0:
            err(f"  ...{i}/{len(subjects)}")
        if delay > 0:
            time.sleep(delay)

    token = None
    err(f"-> {result_count}")

    # --- write outputs ---
    for created_on, template in consent_templates.items():
        template_path = os.path.join(output_dir, f"{study_id}-{created_on}")
        try:
            with open(template_path, "w") as fh:
                fh.write(template)
        except OSError as exc:
            err(f"  warning: could not write template {template_path}: {exc}")

    csv_path = os.path.join(output_dir, f"{study_id}-signature-data.csv")
    fd, tmp_path = tempfile.mkstemp(prefix=".get-consents-", suffix=".csv", dir=output_dir)
    try:
        with os.fdopen(fd, "w", newline="") as fh:
            csv.writer(fh).writerows(result_csv)
        os.replace(tmp_path, csv_path)
    except OSError:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise

    err(f"OK: wrote {result_count['active_consent_rows']} signed-consent rows and "
        f"{len(consent_templates)} template file(s) to {output_dir}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        err("\ninterrupted")
        sys.exit(130)
