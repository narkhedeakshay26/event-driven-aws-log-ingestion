"""CloudFormation custom resource: wire listed S3 buckets to the ingest queue.

Runs on every stack Create / Update / Delete and reconciles:
  1. S3 event notifications: adds ours to newly listed buckets, removes it from
     dropped ones. Notifications owned by anything else on the bucket are kept.
  2. The reader's inline S3/KMS policy: rewritten to cover exactly the current list.
"""
import json
import urllib.request

import boto3
from botocore.exceptions import ClientError

s3 = boto3.client("s3")
iam = boto3.client("iam")
POLICY_NAME = "read-listed-buckets"


def _clean(values):
    return [v.strip() for v in (values or []) if v and v.strip()]


def desired(props, strict=True):
    """Return {bucket: prefix-or-None} from the resource properties."""
    names = _clean(props.get("BucketNames"))
    prefixes = _clean(props.get("BucketPrefixes")) or ["NONE"]
    if prefixes == ["NONE"]:
        prefixes = ["NONE"] * len(names)
    if strict:
        if not names:
            raise ValueError("BucketNames must list at least one bucket")
        if len(prefixes) != len(names):
            raise ValueError(f"BucketPrefixes has {len(prefixes)} entries, BucketNames has "
                             f"{len(names)}: use NONE alone or one entry per bucket")
        if len(set(names)) != len(names):
            raise ValueError("BucketNames contains duplicates")
    return {n: (None if p.upper() == "NONE" else p) for n, p in zip(names, prefixes)}


def set_notification(bucket, config_id, queue_arn=None, prefix=None):
    """Replace (or remove, if queue_arn is None) our queue notification on a bucket."""
    cfg = s3.get_bucket_notification_configuration(Bucket=bucket)
    cfg.pop("ResponseMetadata", None)
    queues = [q for q in cfg.get("QueueConfigurations", []) if q.get("Id") != config_id]
    if queue_arn:
        entry = {"Id": config_id, "QueueArn": queue_arn, "Events": ["s3:ObjectCreated:*"]}
        if prefix:
            entry["Filter"] = {"Key": {"FilterRules": [{"Name": "prefix", "Value": prefix}]}}
        queues.append(entry)
    cfg["QueueConfigurations"] = queues
    s3.put_bucket_notification_configuration(Bucket=bucket, NotificationConfiguration=cfg)


def reader_policy(buckets, kms_keys):
    statements = [{
        "Effect": "Allow",
        "Action": "s3:GetObject",
        "Resource": ["arn:aws:s3:::%s/%s*" % (b, p or "") for b, p in sorted(buckets.items())],
    }]
    if kms_keys:
        statements.append({
            "Effect": "Allow",
            "Action": "kms:Decrypt",
            "Resource": kms_keys,
        })
    return json.dumps({"Version": "2012-10-17", "Statement": statements})


def apply_policy(props, buckets):
    kms = [k for k in _clean(props.get("KmsKeyArns")) if k.upper() != "NONE"]
    for kind, name in (("role", props.get("ReaderRoleName")), ("user", props.get("ReaderUserName"))):
        if not name:
            continue
        key = {"role": "RoleName", "user": "UserName"}[kind]
        args = {key: name, "PolicyName": POLICY_NAME}
        if buckets:
            getattr(iam, f"put_{kind}_policy")(PolicyDocument=reader_policy(buckets, kms), **args)
        else:
            try:
                getattr(iam, f"delete_{kind}_policy")(**args)
            except ClientError as e:
                print(f"policy cleanup skipped: {e}")


def reconcile(event):
    kind = event["RequestType"]
    props = event["ResourceProperties"]
    config_id = props["ConfigId"]

    if kind == "Delete":
        want, had = {}, desired(props, strict=False)
    elif kind == "Update":
        want, had = desired(props), desired(event.get("OldResourceProperties", {}), strict=False)
    else:
        want, had = desired(props), {}

    for bucket in sorted(set(had) - set(want)):
        try:
            set_notification(bucket, config_id)
            print("unwired", bucket)
        except ClientError as e:  # bucket deleted or no longer accessible: nothing to undo
            print(f"could not clean {bucket}: {e}")

    for bucket, prefix in sorted(want.items()):
        set_notification(bucket, config_id, props["QueueArn"], prefix)
        print("wired", bucket, prefix or "")

    apply_policy(props, want)
    return {"Buckets": ",".join(sorted(want)), "Count": str(len(want))}


def send(event, context, status, data=None, reason=""):
    log = getattr(context, "log_stream_name", "-")
    body = {k: event[k] for k in ("StackId", "RequestId", "LogicalResourceId")}
    body.update(Status=status, Reason=reason[:900] or "log: " + log, Data=data or {},
                PhysicalResourceId=event.get("PhysicalResourceId") or event["LogicalResourceId"] + "-wiring")
    req = urllib.request.Request(event["ResponseURL"], data=json.dumps(body).encode(),
                                 method="PUT", headers={"Content-Type": ""})
    urllib.request.urlopen(req, timeout=15)


def handler(event, context):
    print(event["RequestType"], json.dumps(event["ResourceProperties"]))
    try:
        send(event, context, "SUCCESS", reconcile(event))
    except Exception as e:  # never leave CloudFormation hanging
        print(f"ERROR: {e}")
        # A failed Delete would block stack removal forever, so report success anyway.
        status = "SUCCESS" if event["RequestType"] == "Delete" else "FAILED"
        send(event, context, status, reason=str(e))
