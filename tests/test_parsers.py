import gzip
import json

from log_ingest.parsers import PARSERS, infer
from log_ingest.worker import _gunzip


def run(kind, data, key="k"):
    return list(PARSERS[kind](_gunzip(data), key))


def test_cloudtrail_records_from_gzip():
    doc = {"Records": [{"eventName": "ConsoleLogin"}, {"eventName": "CreateUser"}]}
    events = run("cloudtrail", gzip.compress(json.dumps(doc).encode()))
    assert [e["eventName"] for e in events] == ["ConsoleLogin", "CreateUser"]


def test_cloudtrail_digest_files_are_skipped():
    assert run("cloudtrail", b'{"Records":[{"x":1}]}', key="AWSLogs/1/CloudTrail-Digest/x.json.gz") == []


def test_vpc_flow_uses_header_row():
    data = (b"version account-id srcaddr dstaddr action\n"
            b"2 111122223333 10.0.0.1 10.0.0.2 ACCEPT\n"
            b"2 111122223333 10.0.0.3 10.0.0.4 REJECT\n")
    events = run("vpc_flow", data)
    assert events[1] == {"version": "2", "account-id": "111122223333", "srcaddr": "10.0.0.3",
                         "dstaddr": "10.0.0.4", "action": "REJECT"}


def test_alb_quoted_fields():
    line = ('https 2026-01-01T00:00:00.000000Z app/my-lb/abc 198.51.100.7:4431 10.0.0.9:80 '
            '0.001 0.002 0.000 200 200 34 366 "GET https://example.com:443/ HTTP/1.1" '
            '"curl/8.0" ECDHE-RSA-AES128-GCM-SHA256 TLSv1.2 arn:tg "Root=1-abc" "example.com" '
            '"arn:cert" 0 2026-01-01T00:00:00.000000Z "forward" "-" "-" "10.0.0.9:80" "200" "-" "-" "-"')
    [e] = run("alb", line.encode())
    assert e["request"] == "GET https://example.com:443/ HTTP/1.1"
    assert e["user_agent"] == "curl/8.0" and e["elb_status_code"] == "200"


def test_waf_and_jsonl_and_text():
    assert run("waf", b'{"action":"BLOCK"}\n\n{"action":"ALLOW"}\n') == [{"action": "BLOCK"}, {"action": "ALLOW"}]
    assert run("text", b"hello\nworld\n") == [{"message": "hello"}, {"message": "world"}]


def test_aws_config_items():
    doc = {"fileVersion": "1.0", "configurationItems": [{"resourceType": "AWS::S3::Bucket"}]}
    assert run("aws_config", json.dumps(doc).encode()) == [{"resourceType": "AWS::S3::Bucket"}]
    assert run("aws_config", b"{}", key="x/ConfigWritabilityCheckFile") == []


def test_infer_from_aws_key_layout():
    a = "AWSLogs/123456789012"
    assert infer("b", f"o-abc/{a}/CloudTrail/us-east-1/2026/01/01/x.json.gz") == "cloudtrail"
    assert infer("b", f"{a}/CloudTrail-Digest/us-east-1/x.json.gz") == "cloudtrail"
    assert infer("b", f"vpc/{a}/vpcflowlogs/us-east-1/2026/01/01/x.log.gz") == "vpc_flow"
    assert infer("b", f"web/{a}/elasticloadbalancing/us-east-1/2026/01/01/x.log.gz") == "alb"
    assert infer("b", f"{a}/WAFLogs/us-east-1/acl/2026/x.log.gz") == "waf"
    assert infer("aws-waf-logs-acme", "2026/01/01/x.gz") == "waf"
    assert infer("b", f"{a}/Config/us-east-1/2026/1/1/ConfigHistory/x.json.gz") == "aws_config"
    assert infer("b", "app/2026/x.log") is None
