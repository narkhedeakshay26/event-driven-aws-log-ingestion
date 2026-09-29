"""The custom resource's reconcile logic, run against both the readable source and the
minified copy embedded in the CloudFormation template (the code AWS actually runs)."""
import importlib
import json
import types

import boto3
import pytest
import yaml
from moto import mock_aws

from conftest import REGION, ROOT

CONFIG_ID = "log-ingest-acme-prod"


def load_embedded():
    class Loader(yaml.SafeLoader):
        pass
    Loader.add_multi_constructor("!", lambda loader, suffix, node: None)
    tpl = yaml.load((ROOT / "cloudformation" / "onboarding-stack.yaml").read_text(), Loader=Loader)
    code = tpl["Resources"]["ManagerFunction"]["Properties"]["Code"]["ZipFile"]
    mod = types.ModuleType("index")
    exec(compile(code, "index.py", "exec"), mod.__dict__)
    return mod


@pytest.fixture(params=["source", "embedded"])
def env(request):
    with mock_aws():
        s3 = boto3.client("s3", region_name=REGION)
        sqs = boto3.client("sqs", region_name=REGION)
        iam = boto3.client("iam", region_name=REGION)
        for b in ("logs-1", "logs-2", "logs-3"):
            s3.create_bucket(Bucket=b)
        ours = sqs.create_queue(QueueName="ingest")["QueueUrl"]
        other = sqs.create_queue(QueueName="someone-else")["QueueUrl"]
        arn = lambda url: sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
        iam.create_role(RoleName="reader", AssumeRolePolicyDocument="{}")
        # A notification owned by someone else must survive everything we do.
        s3.put_bucket_notification_configuration(Bucket="logs-1", NotificationConfiguration={
            "QueueConfigurations": [{"Id": "other", "QueueArn": arn(other), "Events": ["s3:ObjectRemoved:*"]}]})

        if request.param == "source":
            import bucket_manager
            mod = importlib.reload(bucket_manager)
        else:
            mod = load_embedded()
        sent = []
        mod.send = lambda event, context, status, data=None, reason="": sent.append(
            {"status": status, "data": data, "reason": reason})
        yield types.SimpleNamespace(mod=mod, s3=s3, iam=iam, queue_arn=arn(ours), sent=sent)


def event(env, kind, names, prefixes=("NONE",), old=None, kms=("NONE",)):
    props = {"ServiceToken": "x", "ConfigId": CONFIG_ID, "QueueArn": env.queue_arn,
             "BucketNames": list(names), "BucketPrefixes": list(prefixes), "KmsKeyArns": list(kms),
             "ReaderRoleName": "reader", "ReaderUserName": ""}
    e = {"RequestType": kind, "ResourceProperties": props, "StackId": "stack", "RequestId": "req",
         "LogicalResourceId": "BucketWiring", "ResponseURL": "https://example.invalid"}
    if kind != "Create":
        e["PhysicalResourceId"] = "BucketWiring-wiring"
    if old is not None:
        e["OldResourceProperties"] = {**props, "BucketNames": list(old[0]), "BucketPrefixes": list(old[1])}
    return e


def ours(env, bucket):
    cfg = env.s3.get_bucket_notification_configuration(Bucket=bucket)
    return [q for q in cfg.get("QueueConfigurations", []) if q["Id"] == CONFIG_ID]


def ids(env, bucket):
    cfg = env.s3.get_bucket_notification_configuration(Bucket=bucket)
    return sorted(q["Id"] for q in cfg.get("QueueConfigurations", []))


def policy(env):
    return json.loads(json.dumps(env.iam.get_role_policy(RoleName="reader", PolicyName="read-listed-buckets")
                                 ["PolicyDocument"]))


def test_create_wires_buckets_with_per_bucket_prefix(env):
    env.mod.handler(event(env, "Create", ["logs-1", "logs-2"], ["AWSLogs/", "NONE"]), None)
    assert env.sent[-1]["status"] == "SUCCESS"
    assert env.sent[-1]["data"] == {"Buckets": "logs-1,logs-2", "Count": "2"}

    [b1] = ours(env, "logs-1")
    assert b1["Filter"]["Key"]["FilterRules"] == [{"Name": "prefix", "Value": "AWSLogs/"}]
    assert ids(env, "logs-1") == [CONFIG_ID, "other"]          # foreign notification kept
    [b2] = ours(env, "logs-2")
    assert "Filter" not in b2
    assert ours(env, "logs-3") == []

    resources = policy(env)["Statement"][0]["Resource"]
    assert sorted(resources) == ["arn:aws:s3:::logs-1/AWSLogs/*", "arn:aws:s3:::logs-2/*"]


def test_update_adds_and_removes_buckets(env):
    env.mod.handler(event(env, "Create", ["logs-1", "logs-2"]), None)
    env.mod.handler(event(env, "Update", ["logs-2", "logs-3"], old=(["logs-1", "logs-2"], ["NONE"])), None)
    assert env.sent[-1]["status"] == "SUCCESS"
    assert ours(env, "logs-1") == [] and ids(env, "logs-1") == ["other"]
    assert len(ours(env, "logs-2")) == 1 and len(ours(env, "logs-3")) == 1
    assert sorted(policy(env)["Statement"][0]["Resource"]) == ["arn:aws:s3:::logs-2/*", "arn:aws:s3:::logs-3/*"]


def test_update_is_idempotent(env):
    env.mod.handler(event(env, "Create", ["logs-1"]), None)
    env.mod.handler(event(env, "Update", ["logs-1"], old=(["logs-1"], ["NONE"])), None)
    assert len(ours(env, "logs-1")) == 1


def test_kms_keys_are_added_to_reader_policy(env):
    key = "arn:aws:kms:us-east-1:111122223333:key/abc"
    env.mod.handler(event(env, "Create", ["logs-1"], kms=[key]), None)
    stmts = policy(env)["Statement"]
    assert stmts[1]["Action"] == "kms:Decrypt" and stmts[1]["Resource"] == [key]


@pytest.mark.parametrize("names,prefixes,msg", [
    (["logs-1", "logs-2"], ["AWSLogs/"], "BucketPrefixes has 1 entries"),
    ([], ["NONE"], "at least one bucket"),
    (["logs-1", "logs-1"], ["NONE"], "duplicates"),
])
def test_bad_input_fails_the_stack_with_a_clear_reason(env, names, prefixes, msg):
    env.mod.handler(event(env, "Create", names, prefixes), None)
    assert env.sent[-1]["status"] == "FAILED"
    assert msg in env.sent[-1]["reason"]
    assert ours(env, "logs-1") == []


def test_delete_cleans_up_even_if_a_bucket_is_gone(env):
    env.mod.handler(event(env, "Create", ["logs-2", "logs-3"]), None)
    env.s3.delete_bucket(Bucket="logs-3")
    env.mod.handler(event(env, "Delete", ["logs-2", "logs-3"]), None)
    assert env.sent[-1]["status"] == "SUCCESS"
    assert ours(env, "logs-2") == []
    with pytest.raises(env.iam.exceptions.NoSuchEntityException):
        env.iam.get_role_policy(RoleName="reader", PolicyName="read-listed-buckets")
