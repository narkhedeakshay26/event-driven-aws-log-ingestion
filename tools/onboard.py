#!/usr/bin/env python3
"""Create everything needed to onboard one customer account with the shared template.

  python3 tools/onboard.py --tenant acme \
      --template-url https://my-bucket.s3.amazonaws.com/log-ingest/onboarding-stack.yaml \
      --trusted-principal arn:aws:iam::111122223333:role/log-ingest-consumer \
      --region us-east-1 [--account-id 444455556666] [--out acme-onboarding.yaml]

Prints:
  * a CloudFormation quick-create link: the customer clicks it, picks the dropdowns and
    clicks Create; the ExternalId and trusted principal are already filled in
  * the ExternalId (generated unless --external-id is given): store it in your secret store
  * the consumer config entry for sources.yaml

--out also writes a copy of the template with those two values as defaults, for
customers who prefer uploading a file to clicking a link.
The template is the same for every customer; only the ExternalId differs.
"""
import argparse
import re
import secrets
import sys
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "cloudformation" / "onboarding-stack.yaml"
TENANT = re.compile(r"^[a-z0-9][a-z0-9-]{1,28}[a-z0-9]$")
PRINCIPAL = re.compile(r"^arn:aws[a-z-]*:iam::\d{12}:(root|role/.+|user/.+)$")
STACK_NAME = "log-ingest"
LOG_TYPES = {"cloudtrail": "IngestCloudTrail", "vpcflow": "IngestVpcFlowLogs",
             "loadbalancer": "IngestLoadBalancerLogs", "waf": "IngestWafLogs", "config": "IngestConfigLogs"}
DEFAULT_TYPES = "cloudtrail,vpcflow,loadbalancer,waf"


def quick_create_url(region, template_url, params):
    query = urlencode({"templateURL": template_url, "stackName": STACK_NAME,
                       **{f"param_{k}": v for k, v in params.items()}})
    return (f"https://{region}.console.aws.amazon.com/cloudformation/home?region={region}"
            f"#/stacks/quickcreate?{query}")


def set_default(text, param, value):
    """Set `Default:` inside one parameter block of the Parameters section."""
    lines = text.splitlines()
    start = lines.index("Parameters:")
    head = lines.index(f"  {param}:", start)
    end = head + 1
    while end < len(lines) and (lines[end].startswith("    ") or not lines[end].strip()):
        end += 1
    quoted = '"' + value.replace('"', '\\"') + '"'
    for i in range(head + 1, end):
        if lines[i].startswith("    Default:"):
            lines[i] = f"    Default: {quoted}"
            break
    else:
        lines.insert(head + 2, f"    Default: {quoted}")
    return "\n".join(lines) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tenant", required=True, help="your name for this customer, e.g. acme")
    ap.add_argument("--trusted-principal", required=True, help="your consumer's role ARN (same for every customer)")
    ap.add_argument("--region", required=True, help="region the customer's log buckets are in")
    ap.add_argument("--template-url", help="S3 URL of your hosted onboarding-stack.yaml (for the link)")
    ap.add_argument("--account-id", default="", help="customer's AWS account ID, if known")
    ap.add_argument("--external-id", default="")
    ap.add_argument("--log-types", default=DEFAULT_TYPES,
                    help=f"dropdowns pre-set to Yes in the link, from {','.join(LOG_TYPES)} (default: {DEFAULT_TYPES})")
    ap.add_argument("--auto-rescan", choices=["yes", "no"], default="no", help="pre-set the AutoRescan dropdown")
    ap.add_argument("--out", help="also write a pre-filled template file")
    a = ap.parse_args(argv)

    if not TENANT.match(a.tenant):
        sys.exit("--tenant: 3-30 chars, lowercase letters, digits and dashes")
    if not PRINCIPAL.match(a.trusted_principal):
        sys.exit("--trusted-principal must be an IAM role, user or account-root ARN")
    if a.account_id and not re.fullmatch(r"\d{12}", a.account_id):
        sys.exit("--account-id must be 12 digits")
    if not (a.template_url or a.out):
        sys.exit("give --template-url (for a link), --out (for a file), or both")
    chosen = {t.strip() for t in a.log_types.split(",") if t.strip()}
    if chosen - set(LOG_TYPES):
        sys.exit(f"--log-types: unknown {sorted(chosen - set(LOG_TYPES))}; choose from {','.join(LOG_TYPES)}")
    external_id = a.external_id or secrets.token_urlsafe(24)
    params = {"TrustedPrincipalArn": a.trusted_principal, "ExternalId": external_id,
              **{param: "Yes" if t in chosen else "No" for t, param in LOG_TYPES.items()},
              "AutoRescan": a.auto_rescan.capitalize()}
    env = "EXTERNAL_ID_" + a.tenant.upper().replace("-", "_")

    if a.template_url:
        print("Onboarding link (send to the customer's AWS admin):\n")
        print("  " + quick_create_url(a.region, a.template_url, params))
    if a.out:
        text = TEMPLATE.read_text()
        for k, v in params.items():
            text = set_default(text, k, v)
        Path(a.out).write_text(text)
        print(f"\nPre-filled template written to {a.out}")

    print(f"\nExternalId: store it in your secret manager and export it as {env}:\n  {external_id}")
    print("\nConsumer config entry (config/sources.yaml):")
    print(f"  - tenant: {a.tenant}\n    account_id: \"{a.account_id or '<ACCOUNT_ID from the stack Outputs>'}\"")
    print(f"    region: {a.region}\n    auth:\n      external_id: ${{{env}}}\n    buckets: auto")


if __name__ == "__main__":
    main()
