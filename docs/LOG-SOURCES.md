# Log sources

## Which AWS logs can be ingested with zero extra infrastructure

| Log type | Delivers to S3 natively | Parser |
|---|---|---|
| CloudTrail | Yes (a trail always writes to S3) | `cloudtrail` |
| VPC Flow Logs | Yes, with the S3 destination | `vpc_flow` |
| ALB access logs | Yes | `alb` |
| NLB access logs (TLS listeners) | Yes | `text` (fields differ from ALB, add a parser if you need them split) |
| WAF (regional and CloudFront) | Yes, to a bucket named `aws-waf-logs-*` | `waf` |
| AWS Config | Yes | `jsonl` (snapshots and history files) |
| Route 53 Resolver query logs | Yes | `jsonl` |
| S3 server access logs | Yes | `text` |
| CloudWatch Logs groups (Lambda, ECS, EKS, app logs) | **No**: needs a subscription filter to Firehose to S3 | `jsonl` |

## Finding the bucket and prefix, never guessing

Every service reports its own destination through a read-only call.
`tools/discover_log_sources.py` runs the first five of these for you:

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

## Shared buckets need a prefix

A Control Tower log archive bucket holds CloudTrail, Config and more under different
folders. Without a prefix filter, the `cloudtrail` parser would receive Config files.
Use the `S3KeyPrefix` that `describe_trails` reports as that bucket's `BucketPrefixes`
entry.
