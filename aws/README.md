# aws/

Scripts that go around the Bridge API and hit AWS directly, for cases the
REST API can't serve efficiently (e.g. a bulk cross-app dump with no bulk
endpoint). Meant to be run manually by account admins using their SSO-assumed
AWS role — an explicit `--profile`/`AWS_PROFILE` is required, never an
ambient default. Prefer AWS-managed/serverless operations over anything
needing its own compute; there's no Lambda or server in this repo, and these
scripts are meant to run from a plain terminal or AWS CloudShell.

## Requirements

`pipenv install` (see `../Pipfile`) — pulls in `boto3` (used by the scripts)
and `awscli` (for the one-time setup commands below), unlike the
dependency-free scripts in `util/`.

## Scripts

### `export-upload-schemas.py`

Dumps every Upload Schema (all apps, all revisions) from a Bridge
`UploadSchema` DynamoDB table via a point-in-time (PITR) export to S3, rather
than a live table Scan or the Bridge REST API — the export costs zero read
capacity against the source table and runs as a fully AWS-managed job (there's
no compute to keep running locally). Two subcommands:

- **`start`** kicks off the PITR export and exits immediately with an export
  ARN. Requires PITR already enabled on the table; fails with an actionable
  message if it isn't.
- **`fetch`** checks on (or `--wait`s for, with a `--max-wait-seconds` cap and
  exponential backoff) that export ARN and, once complete, downloads the S3
  export's data files, verifies each one against the manifest's declared
  `md5Checksum`/`itemCount` before trusting it, deserializes DynamoDB-JSON
  items to plain Python, decodes `fieldDefinitions` out of its
  Jackson-serialized JSON string, filters out `deleted=true` schemas by
  default (`--include-deleted` to keep them), and writes the result to a
  single JSON file. Refuses to overwrite an existing
  `--out` file unless `--force` is passed, and writes an audit record (caller
  identity, export/table ARNs, item counts, output path, timestamp) both next
  to the output file and into S3 alongside the export.

**No default environment.** `--table` and `--expect-account` are both required
on every run `--expect-account` is a hard gate that even `--yes` can't bypass —
the script exits if the resolved caller identity's account doesn't match 
(pass an empty string to disable the check entirely).

**Scoping to one app.** `fetch --app-id <id>` filters the result down to a
single app, matched against the table's legacy `studyId` attribute (a name
left over from before Bridge renamed studies to apps — the export itself
always dumps the whole table; there's no server-side app filter for PITR
exports, so this is applied client-side after download). Upload Schemas are
strictly app-wide in Bridge's data model — an app can contain multiple
(modern) Study objects, but schemas have no per-Study association at all, so
`--app-id` already covers every Study under that app; there's no narrower
single-Study scope to ask for.

**AWS API errors** (expired SSO session, access denied, etc.) are caught at
the top level and translated into an actionable message instead of a raw
traceback — see `friendly_aws_error()` in the script.

### `iam-policy-export-upload-schema.json`

**Optional reference, not a required setup step.** This script is meant to be
run by an account admin using their existing SSO-assumed role, which almost
always already has broad enough DynamoDB/S3/STS access to run it as-is — you
don't need to create or attach anything for that case. This file documents
the least-privilege permission set (scoped to one table + one export
bucket/prefix; fill in `<REGION>`, `<ACCOUNT_ID>`, `<TABLE_NAME>`,
`<BUCKET_NAME>`, `<S3_PREFIX>`) only for the case where you're deliberately
running this under a narrower role that doesn't already have that access.
Keep it in sync if the script's AWS API surface changes.

**The `--s3-bucket` itself needs no special bucket policy** in the common
case (same AWS account and region as the table) — DynamoDB writes the export
using the calling identity's own S3 permissions (the policy above), not a
separate service-linked role. Two exceptions:
- A bucket in a *different* AWS account needs a bucket policy explicitly
  granting the exporting identity/account `s3:PutObject`/`s3:GetObject` on
  the export prefix — an identity policy alone isn't sufficient
  cross-account.
- A bucket enforcing default SSE-KMS encryption means the exporting identity
  additionally needs `kms:GenerateDataKey` (for `start`) and `kms:Decrypt`
  (for `fetch`) on that key. Default SSE-S3 needs no extra permissions.

## One-time AWS setup

Before the first run against a given account, someone needs to create the
export bucket (once per account — dev and prod each need their own, since
they're separate AWS accounts). `pipenv install` also pulls in the AWS CLI
(`awscli`), so this can run as `pipenv run aws ...` with no separate CLI
install:

```
pipenv install

# Create the export bucket (choose a name; must be globally unique).
pipenv run aws s3 mb s3://my-export-bucket --region us-east-1

# Block public access and turn on default (SSE-S3) encryption -- belt and
# suspenders, since neither is required for this script to work.
pipenv run aws s3api put-public-access-block --bucket my-export-bucket \
    --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
pipenv run aws s3api put-bucket-encryption --bucket my-export-bucket \
    --server-side-encryption-configuration '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
```

That's it for the common case (running as an account admin under your
existing SSO-assumed role). Only if you're deliberately running this under a
narrower role that doesn't already have DynamoDB/S3/STS access do you need
the extra step of creating and attaching `iam-policy-export-upload-schema.json`
(see that file's description above):

```
# Fill in the placeholders in the JSON file first -- <REGION>, <ACCOUNT_ID>,
# <TABLE_NAME>, <BUCKET_NAME>, <S3_PREFIX> -- then:
pipenv run aws iam create-policy --policy-name upload-schema-export \
    --policy-document file://iam-policy-export-upload-schema.json
pipenv run aws iam attach-role-policy --role-name <narrower-role-name> \
    --policy-arn arn:aws:iam::<ACCOUNT_ID>:policy/upload-schema-export
```

## Usage

```
pipenv install

# 1. Kick off the export; get back an export ARN.
pipenv run python export-upload-schemas.py start \
    --profile AWS_PROFILE --table TABLE_NAME \
    --expect-account ACCOUNT_ID --s3-bucket BUCKET_NAME

# 2. Once it's done (check back after a few minutes, or pass --wait to poll):
pipenv run python export-upload-schemas.py fetch \
    --profile AWS_PROFILE --expect-account ACCOUNT_ID \
    --export-arn <arn from step 1>

# ...or scope the fetch to one app instead of all of them:
pipenv run python export-upload-schemas.py fetch \
    --profile AWS_PROFILE --expect-account ACCOUNT_ID \
    --export-arn <arn from step 1> --app-id MY_APP
```

No local setup at all is needed if you run this from **AWS CloudShell**
instead of a local terminal — it inherits your federated console role
automatically, so `--profile`/`AWS_PROFILE` isn't needed there.

See the top-level `CONTRIBUTING.md` for the conventions shared across this
repo's scripts (useful if you're adding a new one).
