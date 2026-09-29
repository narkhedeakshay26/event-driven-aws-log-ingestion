"""Discovery + wiring, against a simulated account, run against both the source module
and the copy embedded in the CloudFormation template (the code AWS actually runs)."""
import importlib
import json
import types

import boto3
import pytest
import yaml
from moto import mock_aws

from conftest import REGION, ROOT

ACCOUNT = "123456789012"                       # moto's default account
CONFIG_ID = f"log-ingest-{ACCOUNT}-{REGION}"
CT_BUCKET = f"aws-controltower-logs-{ACCOUNT}-{REGION}"


def load_embedded():
    class Loader(yaml.SafeLoader):
        pass
    Loader.add_multi_constructor("!", lambda loader, suffix, node: None)
    tpl = yaml.load((ROOT / "cloudformation" / "onboarding-stack.yaml").read_text(), Loader=Loader)
    mod = types.ModuleType("index")
    exec(compile(tpl["Resources"]["ManagerFunction"]["Properties"]["Code"]["ZipFile"], "index.py", "exec"),
         mod.__dict__)
    return mod


def build_account():
    """A small but realistic account: every supported log source, plus edge cases."""
    s3 = boto3.client("s3", region_name=REGION)
    for b in ("trail-logs", "flow-logs", "alb-logs", "config-logs", CT_BUCKET, "extra-logs", "aws-waf-logs-acme"):
        s3.create_bucket(Bucket=b)
    waf = boto3.client("wafv2", region_name=REGION)
    acl = waf.create_web_acl(Name="acl", Scope="REGIONAL", DefaultAction={"Allow": {}}, VisibilityConfig={
        "SampledRequestsEnabled": False, "CloudWatchMetricsEnabled": False, "MetricName": "m"})["Summary"]["ARN"]
    waf.put_logging_configuration(LoggingConfiguration={
        "ResourceArn": acl, "LogDestinationConfigs": ["arn:aws:s3:::aws-waf-logs-acme"]})
    boto3.client("s3", region_name="eu-west-1").create_bucket(
        Bucket="eu-logs", CreateBucketConfiguration={"LocationConstraint": "eu-west-1"})

    boto3.client("cloudtrail", region_name=REGION).create_trail(
        Name="main", S3BucketName="trail-logs", S3KeyPrefix="org")

    ec2 = boto3.client("ec2", region_name=REGION)
    vpc = ec2.create_vpc(CidrBlock="10.0.0.0/16")["Vpc"]["VpcId"]
    # moto rejects a prefix in a flow log's S3 destination (real AWS allows it), so the
    # prefix case is covered by test_split_s3_arn instead.
    made = ec2.create_flow_logs(ResourceIds=[vpc], ResourceType="VPC", TrafficType="ALL",
                                LogDestinationType="s3", LogDestination="arn:aws:s3:::flow-logs")
    assert made["FlowLogIds"], made["Unsuccessful"]
    subnets = [ec2.create_subnet(VpcId=vpc, CidrBlock=f"10.0.{i}.0/24", AvailabilityZone=f"{REGION}{z}")
               ["Subnet"]["SubnetId"] for i, z in ((1, "a"), (2, "b"))]
    elb = boto3.client("elbv2", region_name=REGION)
    lb = elb.create_load_balancer(Name="web", Subnets=subnets)["LoadBalancers"][0]["LoadBalancerArn"]
    elb.modify_load_balancer_attributes(LoadBalancerArn=lb, Attributes=[
        {"Key": "access_logs.s3.enabled", "Value": "true"},
        {"Key": "access_logs.s3.bucket", "Value": "alb-logs"},
        {"Key": "access_logs.s3.prefix", "Value": "web"}])

    cfg = boto3.client("config", region_name=REGION)
    cfg.put_configuration_recorder(ConfigurationRecorder={"name": "default", "roleARN": f"arn:aws:iam::{ACCOUNT}:role/config"})
    cfg.put_delivery_channel(DeliveryChannel={"name": "default", "s3BucketName": "config-logs"})

    key = boto3.client("kms", region_name=REGION).create_key()["KeyMetadata"]["Arn"]
    s3.put_bucket_encryption(Bucket="flow-logs", ServerSideEncryptionConfiguration={"Rules": [
        {"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "aws:kms", "KMSMasterKeyID": key}}]})

    sqs = boto3.client("sqs", region_name=REGION)
    arn = lambda url: sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
    queue = arn(sqs.create_queue(QueueName=CONFIG_ID)["QueueUrl"])
    other = arn(sqs.create_queue(QueueName="someone-else")["QueueUrl"])
    s3.put_bucket_notification_configuration(Bucket="trail-logs", NotificationConfiguration={
        "QueueConfigurations": [{"Id": "other", "QueueArn": other, "Events": ["s3:ObjectRemoved:*"]}]})
    boto3.client("iam", region_name=REGION).create_role(RoleName="reader", AssumeRolePolicyDocument="{}")
    return queue, key, vpc


@pytest.fixture(params=["source", "embedded"])
def env(request, monkeypatch):
    with mock_aws():
        queue, key, vpc = build_account()
        monkeypatch.setenv("CONFIG_ID", CONFIG_ID)
        monkeypatch.setenv("QUEUE_ARN", queue)
        monkeypatch.setenv("ACCOUNT_ID", ACCOUNT)
        monkeypatch.setenv("REGION", REGION)
        monkeypatch.setenv("READER_ROLE", "reader")
        monkeypatch.setenv("READER_USER", "")
        for t in ("CLOUDTRAIL", "VPCFLOWLOGS", "LOADBALANCERLOGS", "WAFLOGS", "CONFIGLOGS"):
            monkeypatch.setenv(f"INGEST_{t}", "Yes")
        monkeypatch.setenv("ADDITIONAL_BUCKETS", "extra-logs:app/,eu-logs")
        if request.param == "source":
            import bucket_manager
            mod = importlib.reload(bucket_manager)
        else:
            mod = load_embedded()
        mod._clients.clear()
        sent = []
        mod.send = lambda event, context, status, data=None, reason="": sent.append(
            {"status": status, "data": data, "reason": reason})
        yield types.SimpleNamespace(mod=mod, s3=boto3.client("s3", region_name=REGION),
                                    iam=boto3.client("iam", region_name=REGION), sent=sent, key=key,
                                    vpc=vpc, monkeypatch=monkeypatch)


def cfn(kind):
    e = {"RequestType": kind, "ResourceProperties": {}, "StackId": "stack", "RequestId": "req",
         "LogicalResourceId": "BucketWiring", "ResponseURL": "https://example.invalid"}
    if kind != "Create":
        e["PhysicalResourceId"] = "BucketWiring-wiring"
    return e


def ours(env, bucket):
    conf = env.s3.get_bucket_notification_configuration(Bucket=bucket)
    return sorted((q.get("Filter", {}).get("Key", {}).get("FilterRules", [{}])[0].get("Value", ""))
                  for q in conf.get("QueueConfigurations", []) if q["Id"].startswith(CONFIG_ID))


def policy(env):
    return env.iam.get_role_policy(RoleName="reader", PolicyName="read-discovered-buckets")["PolicyDocument"]


def test_create_discovers_and_wires_every_source(env):
    env.mod.handler(cfn("Create"), None)
    result = env.sent[-1]
    assert result["status"] == "SUCCESS", result
    assert result["data"]["WiredBuckets"] == ",".join(sorted(
        ["alb-logs", "aws-waf-logs-acme", "config-logs", "extra-logs", "flow-logs", "trail-logs", CT_BUCKET]))

    assert ours(env, "trail-logs") == ["org/"]           # CloudTrail S3KeyPrefix
    assert ours(env, "flow-logs") == [""]                # from the flow log's S3 ARN
    assert ours(env, "alb-logs") == ["web/"]             # load balancer attribute
    assert ours(env, "config-logs") == [""]              # whole bucket
    assert ours(env, "aws-waf-logs-acme") == [""]        # web ACL logging configuration
    assert ours(env, CT_BUCKET) == [""]                  # found by name
    assert ours(env, "extra-logs") == ["app/"]           # AdditionalBuckets bucket:prefix

    conf = env.s3.get_bucket_notification_configuration(Bucket="trail-logs")
    assert "other" in [q["Id"] for q in conf["QueueConfigurations"]]      # not ours, kept

    assert "eu-logs" in result["data"]["Skipped"] and "is in eu-west-1" in result["data"]["Skipped"]

    stmts = {s["Sid"]: s for s in policy(env)["Statement"]}
    assert "arn:aws:s3:::trail-logs/org/*" in stmts["ReadDiscoveredLogs"]["Resource"]
    assert "arn:aws:s3:::eu-logs/*" not in stmts["ReadDiscoveredLogs"]["Resource"]
    assert stmts["DecryptLogObjects"]["Resource"] == [env.key]            # from bucket encryption


def test_turning_a_dropdown_off_unwires_on_next_run(env):
    env.mod.handler(cfn("Create"), None)
    env.monkeypatch.setenv("INGEST_CONFIGLOGS", "No")
    result = env.mod.handler({}, None)                   # scheduled / manual invoke
    assert "config-logs" not in result["WiredBuckets"]
    assert ours(env, "config-logs") == []
    assert not any("config-logs" in r for r in policy(env)["Statement"][0]["Resource"])


def test_rescan_picks_up_a_new_source(env):
    env.mod.handler(cfn("Create"), None)
    env.s3.create_bucket(Bucket="new-flow-logs")
    made = boto3.client("ec2", region_name=REGION).create_flow_logs(
        ResourceIds=[env.vpc], ResourceType="VPC", TrafficType="REJECT",
        LogDestinationType="s3", LogDestination="arn:aws:s3:::new-flow-logs")
    assert made["FlowLogIds"], made["Unsuccessful"]
    result = env.mod.handler({"source": "aws.events"}, None)
    assert "new-flow-logs" in result["WiredBuckets"].split(",")
    assert ours(env, "new-flow-logs") == [""]


def test_delete_unwires_everything_and_removes_policy(env):
    env.mod.handler(cfn("Create"), None)
    env.mod.handler(cfn("Delete"), None)
    assert env.sent[-1]["status"] == "SUCCESS"
    for b in ("trail-logs", "flow-logs", "alb-logs", "config-logs", CT_BUCKET, "extra-logs", "aws-waf-logs-acme"):
        assert ours(env, b) == []
    with pytest.raises(env.iam.exceptions.NoSuchEntityException):
        policy(env)


def test_failures_fail_the_stack_but_never_block_delete(env):
    def boom(*a, **k):
        raise RuntimeError("S3 said no")
    env.mod.set_notifications = boom
    env.mod.handler(cfn("Create"), None)
    assert env.sent[-1]["status"] == "FAILED" and "S3 said no" in env.sent[-1]["reason"]
    env.mod.handler(cfn("Delete"), None)
    assert env.sent[-1]["status"] == "SUCCESS"


def test_merge_prefixes(env):
    m = env.mod.merge_prefixes
    assert m(["a/", ""]) == [""]
    assert m(["logs/", "logs/alb/", "other/"]) == ["logs/", "other/"]
    assert m(["x/", "x/"]) == ["x/"]


def test_split_s3_arn(env):
    assert env.mod.split_s3_arn("arn:aws:s3:::flow-logs/vpc/") == ("flow-logs", "vpc/")
    assert env.mod.split_s3_arn("arn:aws:s3:::aws-waf-logs-x") == ("aws-waf-logs-x", "")
