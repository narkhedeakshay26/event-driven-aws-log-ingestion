import boto3
from moto import mock_aws

from conftest import REGION
import discover_log_sources as d


def test_owner_hints():
    assert d.owner_hint("aws-controltower-logs-123456789012-us-east-1", "123456789012").startswith("this account")
    assert "account 999999999999" in d.owner_hint("aws-cloudtrail-logs-999999999999-abc", "123456789012")
    assert d.owner_hint("my-custom-bucket", "123456789012").startswith("unknown")


def test_split_s3_arn():
    assert d.split_s3_arn("arn:aws:s3:::flow-bucket/vpc/") == ("flow-bucket", "vpc/")
    assert d.split_s3_arn("arn:aws:s3:::flow-bucket") == ("flow-bucket", "")


@mock_aws
def test_finds_cloudtrail_and_skips_what_it_cannot_read():
    s = boto3.Session(region_name=REGION)
    s.client("s3").create_bucket(Bucket="trail-bucket")
    s.client("cloudtrail").create_trail(Name="main", S3BucketName="trail-bucket", S3KeyPrefix="ct")
    rows, problems = d.discover(s)
    assert ("cloudtrail", "main", "trail-bucket", "ct", "") in rows
    assert all(isinstance(p, str) for p in problems)
