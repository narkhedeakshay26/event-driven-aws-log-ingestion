#!/usr/bin/env python3
"""Create everything needed to onboard one customer account with the shared template.

  python3 tools/onboard.py --tenant acme \
      --template-url https://my-bucket.s3.amazonaws.com/log-ingest/onboarding-stack.yaml \
      --trusted-principal arn:aws:iam::111122223333:role/log-ingest-consumer \
      [--region us-east-1,eu-west-1] [--account-id 444455556666] [--out acme-onboarding.yaml]

--region is optional. Leave it out and the link opens in whatever region the customer's
console is using (they switch to where their logs are). Give one or more regions to get
one link per region, since each region needs its own stack.

Prints:
  * a CloudFormation quick-create link (one per region): the customer clicks it, picks the
    dropdowns and clicks Create; the ExternalId and trusted principal are already filled in
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
REGION = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d$")
TENANT = re.compile(r"^[a-z0-9][a-z0-9-]{1,28}[a-z0-9]$")
PRINCIPAL = re.compile(r"^arn:aws[a-z-]*:iam::\d{12}:(root|role/.+|user/.+)$")
STACK_NAME = "log-ingest"
LOG_TYPES = {"cloudtrail": "IngestCloudTrail", "vpcflow": "IngestVpcFlowLogs",
             "loadbalancer": "IngestLoadBalancerLogs", "waf": "IngestWafLogs", "config": "IngestConfigLogs"}
DEFAULT_TYPES = "cloudtrail,vpcflow,loadbalancer,waf"


def quick_create_url(region, template_url, params):
    """region=None: no region in the link, so the console uses the customer's current one."""
    query = urlencode({"templateURL": template_url, "stackName": STACK_NAME,
                       **{f"param_{k}": v for k, v in params.items()}})
    if region is None:
        return f"https://console.aws.amazon.com/cloudformation/home#/stacks/quickcreate?{query}"
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
    ap.add_argument("--region", default="",
                    help="region(s) the customer's logs are in, comma-separated; omit to let the customer choose")
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
    regions = list(dict.fromkeys(r.strip() for r in a.region.split(",") if r.strip()))
    bad = [r for r in regions if not REGION.match(r)]
    if bad:
        sys.exit(f"--region: {', '.join(bad)} doesn't look like an AWS region (e.g. us-east-1)")
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
        if not regions:
            print("Onboarding link (opens in the customer's current region; they switch to where "
                  "their logs are, and open it again in each region that has logs):\n")
            print("  " + quick_create_url(None, a.template_url, params))
        else:
            print(f"Onboarding link{'s, one per region' if len(regions) > 1 else ''}:\n")
            for r in regions:
                print(f"  {r}: " + quick_create_url(r, a.template_url, params))
    if a.out:
        text = TEMPLATE.read_text()
        for k, v in params.items():
            text = set_default(text, k, v)
        Path(a.out).write_text(text)
        print(f"\nPre-filled template written to {a.out} (works in any region)")

    print(f"\nExternalId (the same for all of this customer's regions): store it in your secret "
          f"manager and export it as {env}:\n  {external_id}")
    print("\nConsumer config (config/sources.yaml), one entry per region the customer deploys in:")
    for r in regions or ["<Region from their Outputs tab>"]:
        print(f"  - tenant: {a.tenant}\n    account_id: \"{a.account_id or '<AccountId from their Outputs tab>'}\"")
        print(f"    region: {r}\n    auth:\n      external_id: ${{{env}}}\n    buckets: auto")


if __name__ == "__main__":
    main()
