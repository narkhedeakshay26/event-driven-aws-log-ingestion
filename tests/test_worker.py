"""Consumer behaviour against simulated S3 + SQS, including the full chain:
stack wiring (custom resource) -> real S3 upload -> S3 event -> consumer -> sink."""
import gzip
import importlib
import json
from urllib.parse import quote_plus

import boto3
import pytest
from moto import mock_aws

from conftest import REGION
from log_ingest.aws import SourceSession
from log_ingest.sinks import FileSink
from log_ingest.worker import SourceWorker


def s3_event(bucket, key, size=100):
    return json.dumps({"Records": [{"eventName": "ObjectCreated:Put", "s3": {
        "bucket": {"name": bucket}, "object": {"key": quote_plus(key), "size": size}}}]})


@pytest.fixture
def aws():
    with mock_aws():
        s3 = boto3.client("s3", region_name=REGION)
        sqs = boto3.client("sqs", region_name=REGION)
        s3.create_bucket(Bucket="trail-logs")
        url = sqs.create_queue(QueueName="log-ingest-acme")["QueueUrl"]
        yield s3, sqs, url


def make_worker(url, tmp_path, buckets=None, auth=None):
    source = {"name": "acme", "tenant": "acme", "region": REGION, "queue_url": url,
              "buckets": buckets or {"trail-logs": "cloudtrail"}, "auth": auth or {}}
    if buckets == "auto":
        source["buckets"] = "auto"
    return SourceWorker(source, FileSink({"path": str(tmp_path / "out.jsonl")}))


def read_out(tmp_path):
    p = tmp_path / "out.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


def counts(sqs, url):
    a = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["All"])["Attributes"]
    return int(a["ApproximateNumberOfMessages"]), int(a["ApproximateNumberOfMessagesNotVisible"])


def test_processes_object_and_deletes_message(aws, tmp_path):
    s3, sqs, url = aws
    key = "AWSLogs/111122223333/CloudTrail/us-east-1/2026/01/01/file one.json.gz"
    body = {"Records": [{"eventName": "ConsoleLogin"}, {"eventName": "CreateUser"}]}
    s3.put_object(Bucket="trail-logs", Key=key, Body=gzip.compress(json.dumps(body).encode()))
    sqs.send_message(QueueUrl=url, MessageBody=json.dumps({"Event": "s3:TestEvent"}))
    sqs.send_message(QueueUrl=url, MessageBody=s3_event("trail-logs", key))

    w = make_worker(url, tmp_path)
    w.run(once=True)

    out = read_out(tmp_path)
    assert [e["event"]["eventName"] for e in out] == ["ConsoleLogin", "CreateUser"]
    assert out[0]["tenant"] == "acme" and out[0]["log_type"] == "cloudtrail"
    assert out[0]["s3"] == {"bucket": "trail-logs", "key": key, "region": REGION}   # key URL-decoded
    assert counts(sqs, url) == (0, 0)
    assert w.stats["events"] == 2 and w.stats["errors"] == 0


def test_unknown_bucket_is_left_for_retry_not_dropped(aws, tmp_path):
    s3, sqs, url = aws
    sqs.send_message(QueueUrl=url, MessageBody=s3_event("not-configured", "x.json"))
    w = make_worker(url, tmp_path)
    w.poll_once(wait_seconds=1)
    assert read_out(tmp_path) == []
    assert counts(sqs, url) == (0, 1)        # in flight, will reappear, then DLQ
    assert w.stats["errors"] == 1


def test_sink_failure_leaves_message_on_queue(aws, tmp_path):
    s3, sqs, url = aws
    s3.put_object(Bucket="trail-logs", Key="k.json", Body=b'{"Records":[{"a":1}]}')
    sqs.send_message(QueueUrl=url, MessageBody=s3_event("trail-logs", "k.json"))
    w = make_worker(url, tmp_path)

    def boom(events):
        raise RuntimeError("downstream unavailable")
    w.sink.write = boom
    w.poll_once(wait_seconds=1)
    assert counts(sqs, url) == (0, 1)


def test_assumes_cross_account_role(aws, tmp_path):
    s3, sqs, url = aws
    iam = boto3.client("iam", region_name=REGION)
    role = iam.create_role(RoleName="log-ingest-acme-reader", AssumeRolePolicyDocument="{}")["Role"]["Arn"]
    w = make_worker(url, tmp_path, auth={"role_arn": role, "external_id": "ext-123"})
    assert isinstance(w.session, SourceSession)
    sqs_client, _ = w.session.clients()
    assert w.session._expires is not None           # temporary credentials, will be refreshed
    assert sqs_client.list_queues()["QueueUrls"]


def test_auto_mode_detects_log_type_from_key(aws, tmp_path):
    s3, sqs, url = aws
    key = "org/AWSLogs/123456789012/CloudTrail/us-east-1/2026/01/01/a.json.gz"
    s3.put_object(Bucket="trail-logs", Key=key, Body=gzip.compress(b'{"Records":[{"eventName":"X"}]}'))
    s3.put_object(Bucket="trail-logs", Key="random/notes.bin", Body=b"??")
    sqs.send_message(QueueUrl=url, MessageBody=s3_event("trail-logs", key))
    sqs.send_message(QueueUrl=url, MessageBody=s3_event("trail-logs", "random/notes.bin"))
    w = make_worker(url, tmp_path, buckets="auto")
    w.run(once=True)
    out = read_out(tmp_path)
    assert [(e["log_type"], e["event"]["eventName"]) for e in out] == [("cloudtrail", "X")]
    assert counts(sqs, url) == (0, 1)        # unknown layout: left for retry, not guessed


def test_default_parser_catches_unrecognised_keys(aws, tmp_path):
    s3, sqs, url = aws
    s3.put_object(Bucket="trail-logs", Key="app/1.log", Body=b"hello\n")
    sqs.send_message(QueueUrl=url, MessageBody=s3_event("trail-logs", "app/1.log"))
    w = make_worker(url, tmp_path, buckets="auto")
    w.source["default_parser"] = "text"
    w.run(once=True)
    assert [e["event"] for e in read_out(tmp_path)] == [{"message": "hello"}]


def test_full_chain_from_discovery_to_sink(aws, tmp_path, monkeypatch):
    """The stack's manager discovers the trail and wires it; a real upload then flows
    through S3's own event into the consumer, which detects the log type itself."""
    s3, sqs, url = aws
    account = "123456789012"
    boto3.client("cloudtrail", region_name=REGION).create_trail(
        Name="main", S3BucketName="trail-logs", S3KeyPrefix="org")
    boto3.client("iam", region_name=REGION).create_role(RoleName="reader", AssumeRolePolicyDocument="{}")
    arn = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
    for k, v in {"CONFIG_ID": "log-ingest-x", "QUEUE_ARN": arn, "ACCOUNT_ID": account, "REGION": REGION,
                 "READER_ROLE": "reader", "INGEST_CLOUDTRAIL": "Yes", "ADDITIONAL_BUCKETS": "NONE"}.items():
        monkeypatch.setenv(k, v)
    import bucket_manager
    mod = importlib.reload(bucket_manager)
    mod._clients.clear()
    assert mod.handler({}, None)["WiredBuckets"] == "trail-logs"

    s3.put_object(Bucket="trail-logs", Key="elsewhere/ignored.json", Body=b'{"Records":[{"x":0}]}')
    key = f"org/AWSLogs/{account}/CloudTrail/{REGION}/2026/01/01/a.json.gz"
    s3.put_object(Bucket="trail-logs", Key=key, Body=gzip.compress(b'{"Records":[{"eventName":"PutObject"}]}'))

    make_worker(url, tmp_path, buckets="auto").run(once=True)
    out = read_out(tmp_path)
    assert [(e["s3"]["key"], e["log_type"]) for e in out] == [(key, "cloudtrail")]   # prefix filter honoured
