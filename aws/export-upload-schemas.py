#!/usr/bin/env python3
"""
One-off export of Bridge Upload Schemas from DynamoDB, ahead of platform shutdown.
Uses a PITR export to S3 (not a live Scan) so it costs zero read capacity against
the source table. --table and --expect-account are both required on every run
(no dev/prod default -- you always say explicitly which environment you mean).
See aws/README.md for full details (--app-id scoping, S3 bucket permission
requirements, audit logging).
"""

import argparse
import base64
import datetime
import gzip
import hashlib
import json
import os
import sys
import time

import boto3
from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    NoCredentialsError,
    ProfileNotFound,
)

INITIAL_POLL_DELAY_SECONDS = 15
MAX_POLL_DELAY_SECONDS = 120

_deserializer = TypeDeserializer()


def friendly_aws_error(exc):
    """Translate common AWS auth/credential failures into an actionable message.
    Falls back to the raw exception for anything else.
    """
    if isinstance(exc, ProfileNotFound):
        return f"{exc}\nCheck that --profile matches a profile in your ~/.aws/config."
    if isinstance(exc, NoCredentialsError):
        return ("No AWS credentials found. Run 'aws sso login --profile <profile>' first, "
                "or check --profile/AWS_PROFILE.")
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("ExpiredToken", "ExpiredTokenException", "RequestExpired", "UnrecognizedClientException"):
            return f"AWS credentials appear expired ({code}). Run: aws sso login --profile <profile>"
        if code == "AccessDeniedException" or code == "AccessDenied":
            return (f"Access denied ({code}): {exc}\n"
                    "Check that your role has the permissions in "
                    "aws/iam-policy-export-upload-schema.json.")
        return f"AWS API call failed ({code or 'unknown error'}): {exc}"
    if isinstance(exc, BotoCoreError):
        return f"AWS SDK error: {exc}"
    return str(exc)


def resolve_profile(args):
    profile = args.profile or os.environ.get("AWS_PROFILE")
    if profile:
        return profile
    if os.environ.get("AWS_EXECUTION_ENV", "").startswith("CloudShell"):
        # CloudShell has no concept of named profiles -- its ambient credentials
        # already are the caller's own federated console role, not an arbitrary
        # local default profile, so there's nothing ambiguous to guard against here.
        return None
    sys.exit(
        "No AWS profile specified. Pass --profile <name> or set AWS_PROFILE. "
        "This script refuses to fall back to a default/ambient profile silently, "
        "since it's meant to be run explicitly under your SSO-assumed admin role. "
    )


def confirm_identity(session, args):
    identity = session.client("sts").get_caller_identity()
    print(f"AWS account: {identity['Account']}")
    print(f"Caller ARN:  {identity['Arn']}")

    if args.expect_account and identity["Account"] != args.expect_account:
        # A hard gate, not a prompt: --yes must never be able to paper over
        # running this against the wrong AWS account.
        sys.exit(
            f"Refusing to continue: resolved account {identity['Account']} does not match "
            f"--expect-account {args.expect_account}."
        )

    if args.yes:
        return identity
    answer = input("Type 'yes' to proceed using this identity: ")
    if answer.strip().lower() != "yes":
        sys.exit("Aborted.")
    return identity


def table_arn(account_id, region, table_name):
    return f"arn:aws:dynamodb:{region}:{account_id}:table/{table_name}"


def cmd_start(args):
    session = boto3.Session(profile_name=resolve_profile(args), region_name=args.region)
    identity = confirm_identity(session, args)

    ddb = session.client("dynamodb")
    arn = table_arn(identity["Account"], session.region_name, args.table)

    backups = ddb.describe_continuous_backups(TableName=args.table)
    pitr_status = backups["ContinuousBackupsDescription"]["PointInTimeRecoveryDescription"][
        "PointInTimeRecoveryStatus"
    ]
    if pitr_status != "ENABLED":
        sys.exit(
            f"Point-in-time recovery is not enabled on {args.table!r} (status: {pitr_status}). "
            f"Enable it first: aws dynamodb update-continuous-backups --table-name {args.table} "
            "--point-in-time-recovery-specification PointInTimeRecoveryEnabled=true "
            "-- then wait a few minutes before starting the export."
        )

    response = ddb.export_table_to_point_in_time(
        TableArn=arn,
        S3Bucket=args.s3_bucket,
        S3Prefix=args.s3_prefix,
        ExportFormat="DYNAMODB_JSON",
    )
    export_arn = response["ExportDescription"]["ExportArn"]
    print(f"Export started: {export_arn}")
    print("Run the 'fetch' subcommand with this --export-arn once it completes "
          "(typically a few minutes for a table this size).")


def poll_export(ddb, export_arn, wait, max_wait_seconds):
    delay = INITIAL_POLL_DELAY_SECONDS
    elapsed = 0
    while True:
        description = ddb.describe_export(ExportArn=export_arn)["ExportDescription"]
        status = description["ExportStatus"]
        if status == "COMPLETED":
            return description
        if status == "FAILED":
            sys.exit(f"Export failed: {description.get('FailureMessage', 'no details given')}")
        if not wait:
            print(f"Export status: {status} (not done yet -- re-run fetch later, "
                  "or pass --wait to poll here instead).")
            sys.exit(0)
        if elapsed >= max_wait_seconds:
            sys.exit(
                f"Export still {status} after {elapsed}s, exceeding --max-wait-seconds "
                f"({max_wait_seconds}). Re-run fetch --wait later to keep checking."
            )
        print(f"Export status: {status}, waiting {delay}s (elapsed {elapsed}s/{max_wait_seconds}s)...")
        time.sleep(delay)
        elapsed += delay
        delay = min(delay * 2, MAX_POLL_DELAY_SECONDS)


def download_manifest_lines(s3, bucket, key):
    body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    return [json.loads(line) for line in body.decode("utf-8").splitlines() if line]


def download_data_file(s3, bucket, entry):
    """Download one export data file and verify it against the manifest's
    declared md5Checksum and itemCount before trusting its contents -- this is
    meant to become the permanent record of these schemas post-shutdown, so a
    truncated download or bit-flip in transit should fail loudly, not silently
    produce an incomplete export.
    """
    key = entry["dataFileS3Key"]
    body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()

    # md5Checksum covers the raw .json.gz bytes as stored in S3 (same content
    # S3's own "etag" is derived from for a single-part upload), not the
    # decompressed JSON -- confirmed by consistent, reproducible mismatches
    # when this was checked against the decompressed content instead.
    expected_md5 = entry.get("md5Checksum")
    if expected_md5:
        actual_md5 = base64.b64encode(hashlib.md5(body).digest()).decode()
        if actual_md5 != expected_md5:
            sys.exit(
                f"Checksum mismatch on {key}: manifest says {expected_md5}, "
                f"downloaded content hashes to {actual_md5}. Re-run fetch."
            )

    decompressed = gzip.decompress(body)
    items = [json.loads(line)["Item"] for line in decompressed.decode("utf-8").splitlines() if line]

    expected_count = entry.get("itemCount")
    if expected_count is not None and len(items) != expected_count:
        sys.exit(
            f"Item count mismatch on {key}: manifest says {expected_count}, "
            f"downloaded {len(items)}. Re-run fetch."
        )

    return items


def deserialize_item(raw_item):
    """Convert a DynamoDB-JSON item (the {"S": ...}/{"N": ...}/etc wire format
    used by table exports) into plain Python values, and unpack fieldDefinitions
    from its Jackson-serialized JSON string into structured JSON.
    """
    item = {k: _deserializer.deserialize(v) for k, v in raw_item.items()}
    field_defs = item.get("fieldDefinitions")
    if isinstance(field_defs, str):
        try:
            item["fieldDefinitions"] = json.loads(field_defs)
        except json.JSONDecodeError:
            pass  # leave as raw string if it doesn't parse
    return item


def write_audit_log(s3, bucket, summary_dir, record):
    """Write the audit record both locally (next to the output file) and
    durably alongside the export in S3, so there's a record of who ran this,
    when, and what came out of it that outlives the local machine.
    """
    local_path = record["output_file"] + ".audit.json"
    tmp_path = local_path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(record, f, indent=2)
    os.replace(tmp_path, local_path)

    audit_key = f"{summary_dir}/fetch-audit.json"
    s3.put_object(Bucket=bucket, Key=audit_key, Body=json.dumps(record, indent=2).encode("utf-8"))
    print(f"Wrote audit log to {local_path} and s3://{bucket}/{audit_key}")


def cmd_fetch(args):
    if os.path.exists(args.out) and not args.force:
        sys.exit(f"{args.out} already exists. Pass --force to overwrite it, or use a different --out.")

    session = boto3.Session(profile_name=resolve_profile(args), region_name=args.region)
    identity = confirm_identity(session, args)

    ddb = session.client("dynamodb")
    s3 = session.client("s3")

    description = poll_export(ddb, args.export_arn, args.wait, args.max_wait_seconds)
    expected_table = table_arn(identity["Account"], session.region_name, args.table)
    actual_table = description.get("TableArn")
    if actual_table and actual_table != expected_table:
        sys.exit(
            f"Refusing to continue: export ARN table {actual_table} does not match --table {expected_table}."
        )
    bucket = description["S3Bucket"]
    manifest_key = description["ExportManifest"]  # .../manifest-summary.json

    summary_dir = manifest_key.rsplit("/", 1)[0]
    manifest_files_key = f"{summary_dir}/manifest-files.json"
    data_file_entries = download_manifest_lines(s3, bucket, manifest_files_key)

    raw_items = []
    for entry in data_file_entries:
        raw_items.extend(download_data_file(s3, bucket, entry))

    expected_total = sum(entry.get("itemCount", 0) for entry in data_file_entries)
    print(f"Verified {len(raw_items)} items across {len(data_file_entries)} data file(s) "
          f"against manifest checksums (manifest declared {expected_total} total).")

    schemas = []
    for raw_item in raw_items:
        item = deserialize_item(raw_item)
        if not (args.include_deleted or item.get("deleted") is not True):
            continue
        if args.app_id and item.get("studyId") != args.app_id:
            continue
        schemas.append(item)

    tmp_out = args.out + ".tmp"
    with open(tmp_out, "w") as f:
        json.dump(schemas, f, indent=2, default=str)
    os.replace(tmp_out, args.out)

    if args.app_id:
        if not schemas:
            print(f"Warning: no schemas matched app/study {args.app_id!r} -- check for a typo.")
        print(f"Wrote {len(schemas)} schema revisions for app/study {args.app_id!r} to {args.out}")
    else:
        print(f"Wrote {len(schemas)} schema revisions (all apps/studies) to {args.out}")

    write_audit_log(s3, bucket, summary_dir, {
        "fetched_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "aws_account": identity["Account"],
        "caller_arn": identity["Arn"],
        "export_arn": args.export_arn,
        "table_arn": description.get("TableArn"),
        "app_id_filter": args.app_id,
        "s3_bucket": bucket,
        "s3_manifest": manifest_key,
        "manifest_item_count": expected_total,
        "items_verified": len(raw_items),
        "schemas_written": len(schemas),
        "include_deleted": args.include_deleted,
        "output_file": args.out,
    })


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--profile", help="AWS SSO profile name (or set AWS_PROFILE)")
    common.add_argument("--region", default="us-east-1", help="AWS region (default: us-east-1)")
    common.add_argument("--yes", action="store_true",
                         help="Skip the interactive identity confirmation prompt")
    common.add_argument("--expect-account", required=True,
                         help="(required) Bridge's known AWS account ID. Refuses to proceed "
                              f"(regardless of --yes) unless the resolved caller identity's "
                              f"account matches -- a hard gate against a stale/wrong SSO profile. "
                              "Pass an empty string to disable this check entirely.")

    start = subparsers.add_parser("start", parents=[common], help="Start the PITR export")
    start.add_argument("--table", required=True,
                        help="(required) DynamoDB table name")
    start.add_argument("--s3-bucket", required=True,
                        help="(required) S3 bucket to export into")
    start.add_argument("--s3-prefix", default="bridge-upload-schema-export",
                        help="S3 key prefix for the export (default: bridge-upload-schema-export)")
    start.set_defaults(func=cmd_start)

    fetch = subparsers.add_parser("fetch", parents=[common], help="Fetch a completed export")
    fetch.add_argument("--export-arn", required=True,
                        help="(required) Export ARN printed by 'start'")
    fetch.add_argument("--table", required=True,
                        help="(required) DynamoDB table name (sanity-check against the export ARN)")
    fetch.add_argument("--out", default="upload_schemas.json",
                        help="Output JSON file (default: upload_schemas.json)")
    fetch.add_argument("--include-deleted", action="store_true",
                        help="Include schemas marked deleted=true (default: excluded)")
    fetch.add_argument("--app-id",
                        help="Only include schemas for this Bridge app/study ID (default: all "
                             "apps/studies). Matches the table's 'studyId' attribute -- a legacy "
                             "name from before Bridge renamed studies to apps, still the physical "
                             "attribute name in this table.")
    fetch.add_argument("--wait", action="store_true",
                        help="Poll (with exponential backoff) until the export completes, "
                             "instead of exiting immediately if it's still in progress")
    fetch.add_argument("--max-wait-seconds", type=int, default=1800,
                        help="With --wait, give up after this many seconds (default: 1800)")
    fetch.add_argument("--force", action="store_true",
                        help="Overwrite --out if it already exists (default: refuse)")
    fetch.set_defaults(func=cmd_fetch)

    args = parser.parse_args()
    try:
        args.func(args)
    except (ClientError, BotoCoreError, NoCredentialsError, ProfileNotFound) as exc:
        sys.exit(friendly_aws_error(exc))


if __name__ == "__main__":
    main()
