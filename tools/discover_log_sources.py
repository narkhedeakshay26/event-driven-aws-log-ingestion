#!/usr/bin/env python3
"""Find where an AWS account's logs land in S3, using read-only calls only.

  python3 tools/discover_log_sources.py --region us-east-1 [--profile NAME]

Checks CloudTrail, VPC Flow Logs, ALB/NLB access logs, WAF (regional) and AWS Config,
prints a table, and suggests --bucket arguments for prefill_template.py. Any check the
credentials aren't allowed to run is reported and skipped, never fatal.
"""
import argparse
import re

import boto3
from botocore.exceptions import BotoCoreError, ClientError

OWNER_PATTERNS = [
    (re.compile(r"^aws-controltower-logs-(\d{12})-"), "Control Tower log-archive bucket"),
    (re.compile(r"^aws-cloudtrail-logs-(\d{12})-"), "default CloudTrail bucket"),
]


def owner_hint(bucket, own_account):
    for pattern, what in OWNER_PATTERNS:
        m = pattern.match(bucket)
        if m:
            acct = m.group(1)
            where = "this account" if acct == own_account else f"account {acct}"
            return f"{where} ({what})"
    return "unknown: custom bucket name, ask the owner"


def split_s3_arn(arn):
    """arn:aws:s3:::bucket/prefix -> (bucket, prefix)"""
    path = arn.split(":::", 1)[-1]
    bucket, _, prefix = path.partition("/")
    return bucket, prefix


def cloudtrail(s):
    out = []
    for t in s.client("cloudtrail").describe_trails(includeShadowTrails=False)["trailList"]:
        if t.get("S3BucketName"):
            prefix = t.get("S3KeyPrefix", "")
            out.append(("cloudtrail", t["Name"], t["S3BucketName"], prefix,
                        "org trail" if t.get("IsOrganizationTrail") else ""))
    return out


def vpc_flow(s):
    out = []
    for f in s.client("ec2").describe_flow_logs()["FlowLogs"]:
        if f.get("LogDestinationType") == "s3" and f.get("LogDestination"):
            b, p = split_s3_arn(f["LogDestination"])
            out.append(("vpc_flow", f["FlowLogId"], b, p, f.get("ResourceId", "")))
    return out


def load_balancers(s):
    out, elb = [], s.client("elbv2")
    for page in elb.get_paginator("describe_load_balancers").paginate():
        for lb in page["LoadBalancers"]:
            attrs = {a["Key"]: a["Value"] for a in
                     elb.describe_load_balancer_attributes(LoadBalancerArn=lb["LoadBalancerArn"])["Attributes"]}
            if attrs.get("access_logs.s3.enabled") == "true":
                out.append(("alb", lb["LoadBalancerName"], attrs.get("access_logs.s3.bucket", ""),
                            attrs.get("access_logs.s3.prefix", ""), lb.get("Type", "")))
    return out


def waf(s):
    out, w = [], s.client("wafv2")
    for acl in w.list_web_acls(Scope="REGIONAL").get("WebACLs", []):
        try:
            cfg = w.get_logging_configuration(ResourceArn=acl["ARN"])["LoggingConfiguration"]
        except ClientError:
            continue
        for dest in cfg.get("LogDestinationConfigs", []):
            if dest.startswith("arn:aws:s3:::"):
                b, p = split_s3_arn(dest)
                out.append(("waf", acl["Name"], b, p, ""))
            else:
                out.append(("waf", acl["Name"], "-", "-", "delivers via Firehose/CloudWatch, not S3"))
    return out


def aws_config(s):
    out = []
    for ch in s.client("config").describe_delivery_channels()["DeliveryChannels"]:
        out.append(("aws_config", ch["name"], ch["s3BucketName"], ch.get("s3KeyPrefix", ""), ""))
    return out


CHECKS = [cloudtrail, vpc_flow, load_balancers, waf, aws_config]


def discover(session):
    rows, problems = [], []
    for check in CHECKS:
        try:
            rows.extend(check(session))
        except (ClientError, BotoCoreError) as e:
            problems.append(f"{check.__name__}: {e}")
    return rows, problems


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--region", required=True)
    ap.add_argument("--profile")
    a = ap.parse_args()
    s = boto3.Session(profile_name=a.profile, region_name=a.region)
    account = s.client("sts").get_caller_identity()["Account"]
    rows, problems = discover(s)

    print(f"Account {account}, region {a.region}\n")
    print(f"{'TYPE':<11} {'SOURCE':<28} {'BUCKET':<44} {'PREFIX':<22} OWNER / NOTES")
    for kind, name, bucket, prefix, note in rows:
        hint = owner_hint(bucket, account) if bucket != "-" else ""
        print(f"{kind:<11} {name[:27]:<28} {bucket[:43]:<44} {prefix[:21] or '-':<22} {hint} {note}".rstrip())
    if not rows:
        print("(no S3 log destinations found)")
    for p in problems:
        print(f"\nskipped {p}")

    wanted = {}
    for kind, _, bucket, prefix, _ in rows:
        if bucket != "-":
            wanted.setdefault(bucket, prefix)
    if wanted:
        print("\nSuggested prefill arguments (buckets in the same region and account go in one stack):")
        print("  " + " ".join(f"--bucket {b}{':' + p if p else ''}" for b, p in wanted.items()))


if __name__ == "__main__":
    main()
