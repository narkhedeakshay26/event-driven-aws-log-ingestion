"""Discover this account's log buckets and wire them to the ingest queue.

Runs three ways, always doing the same reconcile:
  * as a CloudFormation custom resource, on stack Create / Update / Delete
  * on a daily schedule, when the stack's AutoRescan dropdown is Yes
  * on demand: `aws lambda invoke --function-name <ManagerFunction> out.json`

Reconcile =
  1. Discover where each selected log type is delivered, using read-only describe calls
     (CloudTrail, VPC Flow Logs, ALB, WAF, AWS Config), plus the Control Tower
     log-archive bucket and any AdditionalBuckets.
  2. Keep only buckets owned by this account and in this region; report the rest.
  3. Point S3 event notifications at the queue, keeping notifications that aren't ours.
  4. Rewrite the reader's inline policy to cover exactly those buckets and their KMS keys.

The reader policy doubles as the record of what is currently wired, so a scheduled run
knows which buckets to unwire without keeping any other state.
"""
import json
import os
import urllib.parse
import urllib.request

import boto3
from botocore.exceptions import BotoCoreError, ClientError

POLICY_NAME = "read-discovered-buckets"
SOURCE_TYPES = ("CloudTrail", "VpcFlowLogs", "LoadBalancerLogs", "WafLogs", "ConfigLogs")

_clients = {}


def client(name):
    if name not in _clients:
        _clients[name] = boto3.client(name)
    return _clients[name]


def settings():
    """Everything comes from environment variables set by the template."""
    env = os.environ
    return {
        "config_id": env["CONFIG_ID"],
        "queue_arn": env["QUEUE_ARN"],
        "account": env["ACCOUNT_ID"],
        "region": env["REGION"],
        "reader_role": env.get("READER_ROLE", ""),
        "reader_user": env.get("READER_USER", ""),
        "enabled": {t for t in SOURCE_TYPES if env.get(f"INGEST_{t.upper()}", "No") == "Yes"},
        "additional": [b.strip() for b in env.get("ADDITIONAL_BUCKETS", "").split(",")
                       if b.strip() and b.strip().upper() != "NONE"],
    }


# ------------------------------------------------------------------------- discovery
def split_s3_arn(arn):
    bucket, _, prefix = arn.split(":::", 1)[-1].partition("/")
    return bucket, prefix


def find_cloudtrail(cfg):
    found = []
    for t in client("cloudtrail").describe_trails(includeShadowTrails=True)["trailList"]:
        if t.get("S3BucketName"):
            prefix = t.get("S3KeyPrefix") or ""
            found.append({"bucket": t["S3BucketName"], "prefix": prefix + "/" if prefix else "",
                          "kms": t.get("KmsKeyId"), "via": f"CloudTrail trail {t['Name']}"})
    # Control Tower keeps the organization's trail in the log-archive account, where
    # DescribeTrails shows nothing. Its bucket name is predictable, so look for it.
    ct_bucket = f"aws-controltower-logs-{cfg['account']}-{cfg['region']}"
    found.append({"bucket": ct_bucket, "prefix": "", "via": "Control Tower log archive",
                  "optional": True})
    return found


def find_vpc_flow_logs(cfg):
    found = []
    paginator = client("ec2").get_paginator("describe_flow_logs")
    for page in paginator.paginate(Filters=[{"Name": "log-destination-type", "Values": ["s3"]}]):
        for f in page["FlowLogs"]:
            if f.get("LogDestination"):
                bucket, prefix = split_s3_arn(f["LogDestination"])
                found.append({"bucket": bucket, "prefix": prefix, "via": f"flow log {f['FlowLogId']}"})
    return found


def find_load_balancer_logs(cfg):
    found, elb = [], client("elbv2")
    for page in elb.get_paginator("describe_load_balancers").paginate():
        for lb in page["LoadBalancers"]:
            attrs = {a["Key"]: a["Value"] for a in elb.describe_load_balancer_attributes(
                LoadBalancerArn=lb["LoadBalancerArn"])["Attributes"]}
            if attrs.get("access_logs.s3.enabled") == "true" and attrs.get("access_logs.s3.bucket"):
                prefix = attrs.get("access_logs.s3.prefix") or ""
                found.append({"bucket": attrs["access_logs.s3.bucket"],
                              "prefix": prefix.rstrip("/") + "/" if prefix else "",
                              "via": f"load balancer {lb['LoadBalancerName']}"})
    return found


def find_waf_logs(cfg):
    found, waf = [], client("wafv2")
    scopes = ["REGIONAL"] + (["CLOUDFRONT"] if cfg["region"] == "us-east-1" else [])
    for scope in scopes:
        for acl in waf.list_web_acls(Scope=scope).get("WebACLs", []):
            try:
                conf = waf.get_logging_configuration(ResourceArn=acl["ARN"])["LoggingConfiguration"]
            except ClientError:
                continue          # logging not enabled for this web ACL
            for dest in conf.get("LogDestinationConfigs", []):
                if dest.startswith("arn:aws:s3:::"):
                    bucket, prefix = split_s3_arn(dest)
                    found.append({"bucket": bucket, "prefix": prefix, "via": f"web ACL {acl['Name']}"})
    return found


def find_config_logs(cfg):
    found = []
    for ch in client("config").describe_delivery_channels()["DeliveryChannels"]:
        prefix = ch.get("s3KeyPrefix") or ""
        found.append({"bucket": ch["s3BucketName"], "prefix": prefix + "/" if prefix else "",
                      "kms": ch.get("s3KmsKeyArn"), "via": f"Config channel {ch['name']}"})
    return found


FINDERS = {
    "CloudTrail": find_cloudtrail,
    "VpcFlowLogs": find_vpc_flow_logs,
    "LoadBalancerLogs": find_load_balancer_logs,
    "WafLogs": find_waf_logs,
    "ConfigLogs": find_config_logs,
}


def bucket_home(bucket, cfg):
    """None if the bucket is ours and in this region, else a reason to skip it."""
    try:
        loc = client("s3").get_bucket_location(Bucket=bucket, ExpectedBucketOwner=cfg["account"])
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("NoSuchBucket", "404"):
            return "does not exist"
        return "is owned by another account (deploy this stack in that account)"
    region = loc.get("LocationConstraint") or "us-east-1"
    region = "eu-west-1" if region == "EU" else region
    if region != cfg["region"]:
        return f"is in {region} (deploy this stack there too)"
    return None


def bucket_kms_keys(bucket):
    try:
        rules = client("s3").get_bucket_encryption(Bucket=bucket)["ServerSideEncryptionConfiguration"]["Rules"]
    except ClientError:
        return []
    keys = []
    for r in rules:
        d = r.get("ApplyServerSideEncryptionByDefault", {})
        if d.get("SSEAlgorithm", "").startswith("aws:kms") and d.get("KMSMasterKeyID"):
            keys.append(d["KMSMasterKeyID"])
    return keys


def key_arn(key_id):
    """IAM policies need key ARNs; aliases and bare IDs are resolved."""
    if key_id.startswith("arn:") and ":key/" in key_id:
        return key_id
    try:
        return client("kms").describe_key(KeyId=key_id)["KeyMetadata"]["Arn"]
    except ClientError:
        return None


def merge_prefixes(prefixes):
    """One bucket may be named by several sources. S3 rejects overlapping prefixes for
    the same event, so watch the whole bucket if any source needs it, and drop any
    prefix already covered by a shorter one."""
    if "" in prefixes:
        return [""]
    kept = []
    for p in sorted(set(prefixes), key=len):
        if not any(p.startswith(k) for k in kept):
            kept.append(p)
    return kept


def discover(cfg):
    """Returns (wanted {bucket: {"prefixes": [...], "kms": set, "via": [...]}}, skipped [...])."""
    candidates, skipped = [], []
    for source in sorted(cfg["enabled"]):
        try:
            candidates.extend(FINDERS[source](cfg))
        except (ClientError, BotoCoreError) as e:
            skipped.append(f"{source}: discovery failed ({e})")
    for spec in cfg["additional"]:
        bucket, _, prefix = spec.partition(":")
        candidates.append({"bucket": bucket, "prefix": prefix, "via": "AdditionalBuckets"})

    wanted, homes = {}, {}
    for c in candidates:
        b = c["bucket"]
        if b not in homes:
            homes[b] = bucket_home(b, cfg)
        if homes[b]:
            if not c.get("optional"):
                skipped.append(f"{b} ({c['via']}) {homes[b]}")
            continue
        entry = wanted.setdefault(b, {"prefixes": [], "kms": set(), "via": []})
        entry["prefixes"].append(c.get("prefix", ""))
        entry["via"].append(c["via"])
        if c.get("kms"):
            entry["kms"].add(c["kms"])
    for b, entry in wanted.items():
        entry["prefixes"] = merge_prefixes(entry["prefixes"])
        entry["kms"] |= set(bucket_kms_keys(b))
    return wanted, sorted(set(skipped))


# ------------------------------------------------------------------------- wiring
def set_notifications(bucket, cfg, prefixes):
    """Replace our queue notifications on a bucket (all IDs starting with config_id)."""
    s3 = client("s3")
    conf = s3.get_bucket_notification_configuration(Bucket=bucket, ExpectedBucketOwner=cfg["account"])
    conf.pop("ResponseMetadata", None)
    mine = cfg["config_id"]
    queues = [q for q in conf.get("QueueConfigurations", []) if not q.get("Id", "").startswith(mine)]
    for i, prefix in enumerate(prefixes):
        entry = {"Id": f"{mine}-{i}", "QueueArn": cfg["queue_arn"], "Events": ["s3:ObjectCreated:*"]}
        if prefix:
            entry["Filter"] = {"Key": {"FilterRules": [{"Name": "prefix", "Value": prefix}]}}
        queues.append(entry)
    conf["QueueConfigurations"] = queues
    s3.put_bucket_notification_configuration(Bucket=bucket, NotificationConfiguration=conf,
                                             ExpectedBucketOwner=cfg["account"])


def policy_document(wanted):
    resources = sorted(f"arn:aws:s3:::{b}/{p}*" for b, e in wanted.items() for p in e["prefixes"])
    keys = sorted({k for e in wanted.values() for k in (key_arn(x) for x in e["kms"]) if k})
    statements = [{"Sid": "ReadDiscoveredLogs", "Effect": "Allow", "Action": "s3:GetObject",
                   "Resource": resources}]
    if keys:
        statements.append({"Sid": "DecryptLogObjects", "Effect": "Allow", "Action": "kms:Decrypt",
                           "Resource": keys})
    return {"Version": "2012-10-17", "Statement": statements}


def _policy_api(cfg, verb):
    iam = client("iam")
    if cfg["reader_role"]:
        return getattr(iam, f"{verb}_role_policy"), {"RoleName": cfg["reader_role"]}
    return getattr(iam, f"{verb}_user_policy"), {"UserName": cfg["reader_user"]}


def currently_wired(cfg):
    """Read the buckets we wired last time back out of the reader policy."""
    get, who = _policy_api(cfg, "get")
    try:
        doc = get(PolicyName=POLICY_NAME, **who)["PolicyDocument"]
    except ClientError:
        return set()
    if isinstance(doc, str):
        doc = json.loads(urllib.parse.unquote(doc))
    buckets = set()
    for st in doc.get("Statement", []):
        if st.get("Sid") == "ReadDiscoveredLogs":
            res = st["Resource"] if isinstance(st["Resource"], list) else [st["Resource"]]
            buckets |= {r.split(":::", 1)[1].split("/", 1)[0] for r in res}
    return buckets


def reconcile(cfg, deleting=False):
    before = currently_wired(cfg)
    wanted, skipped = ({}, []) if deleting else discover(cfg)

    for bucket in sorted(before - set(wanted)):
        try:
            set_notifications(bucket, cfg, [])
            print("unwired", bucket)
        except ClientError as e:  # bucket deleted or no longer ours: nothing to undo
            print("could not unwire", bucket, e)

    for bucket, entry in sorted(wanted.items()):
        set_notifications(bucket, cfg, entry["prefixes"])
        print("wired", bucket, entry["prefixes"], "via", ", ".join(entry["via"]))

    put, who = _policy_api(cfg, "put")
    delete, _ = _policy_api(cfg, "delete")
    if wanted:
        put(PolicyName=POLICY_NAME, PolicyDocument=json.dumps(policy_document(wanted)), **who)
    else:
        try:
            delete(PolicyName=POLICY_NAME, **who)
        except ClientError:
            pass

    for s in skipped:
        print("skipped", s)
    return {
        "WiredBuckets": ",".join(sorted(wanted)) or "none",
        "WiredCount": str(len(wanted)),
        "Skipped": ("; ".join(skipped) or "none")[:1000],
    }


# ------------------------------------------------------------------------- entry point
def send(event, context, status, data=None, reason=""):
    log = getattr(context, "log_stream_name", "-")
    body = {k: event[k] for k in ("StackId", "RequestId", "LogicalResourceId")}
    body.update(Status=status, Reason=reason[:900] or "log: " + log, Data=data or {},
                PhysicalResourceId=event.get("PhysicalResourceId") or event["LogicalResourceId"] + "-wiring")
    req = urllib.request.Request(event["ResponseURL"], data=json.dumps(body).encode(),
                                 method="PUT", headers={"Content-Type": ""})
    urllib.request.urlopen(req, timeout=15)


def handler(event, context):
    cfg = settings()
    if "RequestType" not in event:                       # schedule or manual invoke
        result = reconcile(cfg)
        print(json.dumps(result))
        return result

    print(event["RequestType"], json.dumps(event.get("ResourceProperties", {})))
    try:
        send(event, context, "SUCCESS", reconcile(cfg, deleting=event["RequestType"] == "Delete"))
    except Exception as e:  # never leave CloudFormation hanging
        print(f"ERROR: {e}")
        # A failed Delete would block stack removal forever, so report success anyway.
        status = "SUCCESS" if event["RequestType"] == "Delete" else "FAILED"
        send(event, context, status, reason=str(e))
