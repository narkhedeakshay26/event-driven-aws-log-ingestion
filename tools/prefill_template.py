#!/usr/bin/env python3
"""Pre-fill the onboarding template for one source, so the account owner only
uploads it and clicks through. Nothing to type means nothing to mistype.

  python3 tools/prefill_template.py \
      --source-tag acme-prod \
      --bucket acme-cloudtrail-logs:AWSLogs/ \
      --bucket acme-vpc-flow-logs \
      --trusted-principal arn:aws:iam::111122223333:role/log-ingest-consumer \
      --out acme-prod-onboarding.yaml

--bucket NAME[:PREFIX] is repeatable and keeps its order. A missing prefix becomes NONE.
In CrossAccountRole mode an ExternalId is generated unless you pass --external-id.
Also prints the stack name, the matching consumer config entry and a CLI command.
"""
import argparse
import re
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "cloudformation" / "onboarding-stack.yaml"
TAG = re.compile(r"^[a-z0-9][a-z0-9-]{1,28}[a-z0-9]$")
BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")


def set_default(text, param, value):
    """Set `Default:` inside one parameter block of the Parameters section."""
    lines = text.splitlines()
    try:
        start = lines.index("Parameters:")
        head = lines.index(f"  {param}:", start)
    except ValueError:
        raise SystemExit(f"parameter {param} not found in template")
    end = head + 1
    while end < len(lines) and (lines[end].startswith("    ") or not lines[end].strip()):
        end += 1
    quoted = '"' + value.replace('"', '\\"') + '"'
    for i in range(head + 1, end):
        if lines[i].startswith("    Default:"):
            lines[i] = f"    Default: {quoted}"
            break
    else:
        lines.insert(head + 2, f"    Default: {quoted}")   # right after `Type:`
    return "\n".join(lines) + "\n"


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source-tag", required=True)
    ap.add_argument("--bucket", action="append", required=True, metavar="NAME[:PREFIX]")
    ap.add_argument("--kms-key", action="append", default=[], metavar="KEY_ARN")
    ap.add_argument("--access-mode", choices=["CrossAccountRole", "AccessKey"], default="CrossAccountRole")
    ap.add_argument("--trusted-principal", default="", help="role/account ARN that will assume the reader role")
    ap.add_argument("--external-id", default="")
    ap.add_argument("--region", default="<REGION>", help="only used in the printed hints")
    ap.add_argument("--out", required=True)
    return ap.parse_args(argv)


def main(argv=None):
    a = parse_args(argv)
    if not TAG.match(a.source_tag):
        sys.exit("--source-tag: 3-30 chars, lowercase letters, digits and dashes")
    names, prefixes = [], []
    for spec in a.bucket:
        name, _, prefix = spec.partition(":")
        if not BUCKET.match(name):
            sys.exit(f"--bucket {name!r} is not a valid S3 bucket name")
        if name in names:
            sys.exit(f"--bucket {name} given twice")
        names.append(name)
        prefixes.append(prefix or "NONE")
    if a.access_mode == "CrossAccountRole":
        if not a.trusted_principal:
            sys.exit("--trusted-principal is required for CrossAccountRole mode")
        a.external_id = a.external_id or secrets.token_urlsafe(24)

    text = TEMPLATE.read_text()
    values = {
        "SourceTag": a.source_tag,
        "BucketNames": ",".join(names),
        "BucketPrefixes": "NONE" if all(p == "NONE" for p in prefixes) else ",".join(prefixes),
        "KmsKeyArns": ",".join(a.kms_key) or "NONE",
        "AccessMode": a.access_mode,
        "TrustedPrincipalArn": a.trusted_principal,
        "ExternalId": a.external_id,
    }
    for param, value in values.items():
        text = set_default(text, param, value)
    Path(a.out).write_text(text)

    stack = f"log-ingest-{a.source_tag}"
    print(f"Wrote {a.out}")
    print(f"\nStack name for the account owner to type:  {stack}")
    print("\nOr deploy from the CLI (inside the account that owns the buckets):")
    print(f"  aws cloudformation deploy --stack-name {stack} --template-file {a.out} \\\n"
          f"    --capabilities CAPABILITY_NAMED_IAM --region {a.region}")
    print("\nConsumer config entry (config/sources.yaml), fill in the account ID once the stack exists:")
    print(f"  - tenant: {a.source_tag}\n    region: {a.region}\n"
          f"    queue_url: https://sqs.{a.region}.amazonaws.com/<ACCOUNT_ID>/{stack}")
    if a.access_mode == "CrossAccountRole":
        env = "EXTERNAL_ID_" + a.source_tag.upper().replace("-", "_")
        print(f"    auth:\n      role_arn: arn:aws:iam::<ACCOUNT_ID>:role/{stack}-reader\n"
              f"      external_id: ${{{env}}}")
        print(f"\nStore the ExternalId in your secret store and export it as {env}:\n  {a.external_id}")
    print("    buckets:")
    for n in names:
        print(f"      {n}: <parser>   # cloudtrail | vpc_flow | alb | waf | jsonl | text")


if __name__ == "__main__":
    main()
