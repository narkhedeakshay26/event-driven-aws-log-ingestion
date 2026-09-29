# Log sources

The stack discovers these on its own when the matching dropdown is **Yes**, and the
consumer detects each file's type from its key. The tables below are what that
automation does, and how to check it by hand.

## Which AWS logs can be ingested with zero extra infrastructure

| Log type | Delivers to S3 natively | Parser (auto-detected by key) |
|---|---|---|
| CloudTrail | Yes (a trail always writes to S3) | `cloudtrail` (`/CloudTrail/`) |
| VPC Flow Logs | Yes, with the S3 destination | `vpc_flow` (`/vpcflowlogs/`) |
| ALB access logs | Yes | `alb` (`/elasticloadbalancing/`) |
| NLB access logs (TLS listeners) | Yes | `text` (fields differ from ALB, add a parser if you need them split) |
| WAF (regional and CloudFront) | Yes, to a bucket named `aws-waf-logs-*` | `waf` (`/WAFLogs/` or bucket name) |
| AWS Config | Yes | `aws_config` (`/Config/`) |
| Route 53 Resolver query logs | Yes | `jsonl` |
| S3 server access logs | Yes | `text` |
| CloudWatch Logs groups (Lambda, ECS, EKS, app logs) | **No**: needs a subscription filter to Firehose to S3 | `jsonl` |

## Finding the bucket and prefix, never guessing

Every service reports its own destination through a read-only call.
The bucket manager runs the first five inside the customer's account. `tools/discover_log_sources.py` runs the same calls from your laptop, as a preview:

| Log type | Call | Read-only permission |
|---|---|---|
| CloudTrail | `cloudtrail.describe_trails()` → `S3BucketName`, `S3KeyPrefix` | `cloudtrail:DescribeTrails` |
| VPC Flow Logs | `ec2.describe_flow_logs()` → `LogDestination` | `ec2:DescribeFlowLogs` |
| ALB / NLB | `elbv2.describe_load_balancer_attributes()` → `access_logs.s3.*` | `elasticloadbalancing:Describe*` |
| WAF | `wafv2.get_logging_configuration()` → `LogDestinationConfigs` | `wafv2:GetLoggingConfiguration` |
| AWS Config | `configservice.describe_delivery_channels()` → `s3BucketName` | `config:DescribeDeliveryChannels` |
| S3 access logs | `s3.get_bucket_logging(Bucket=source)` → `TargetBucket`, `TargetPrefix` | `s3:GetBucketLogging` |

## Which account owns a bucket?

Credentials that can read CloudTrail usually can't call S3, so the bucket name is often
the only clue:

| Name pattern | Owner |
|---|---|
| `aws-controltower-logs-<account-id>-<region>` | The Control Tower log archive account in the name |
| `aws-cloudtrail-logs-<account-id>-<suffix>` | The account in the name (the console's default trail bucket) |
| Anything else | Unknown. Ask the owner |

`IsOrganizationTrail: true` on a trail whose bucket is in *your* account usually means
you're looking at the organization's management account. Control Tower's baseline trail
will then write to a *different* account: the log archive.

## Shared buckets

A Control Tower log archive bucket holds CloudTrail, Config and more under per-account
folders. An S3 prefix filter can't separate those, so the stack watches the whole bucket
and the consumer routes each file by its key. Digest files and Config's writability
checks are skipped.
