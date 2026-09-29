# Event-Driven AWS Log Ingestion & Self-Service Onboarding

Pull CloudTrail, VPC Flow Logs, ALB, WAF and AWS Config logs out of any number of AWS
accounts, **the moment each file lands in S3**, and onboard a new account with one
CloudFormation upload that its owner can run alone.

![Architecture](docs/img/architecture.svg)

**How it works**

1. The account owner deploys **one CloudFormation stack** that lists their log buckets.
2. A small **bucket-manager Lambda** (a custom resource) wires each bucket's
   `ObjectCreated` events to **one SQS queue**. It also grants a **reader role** read
   access to exactly those buckets, nothing more.
3. The **consumer** (a container you run anywhere) assumes that role with an
   **ExternalId**, long-polls the queue, fetches each new object, parses it, and hands
   the events to a **sink** (Kafka, a file or stdout).
4. To **add or remove a bucket later**, the owner edits one field and runs a stack
   **Update**. The Lambda reconciles the changes. There's no new stack, key or queue.

## Why event-driven instead of polling

| | Polling the CloudWatch Logs API or listing buckets | S3 → SQS events (this project) |
|---|---|---|
| Latency | Minutes (poll interval) | Seconds after the file lands |
| Cost at scale | Grows with bucket size (repeated LIST calls) | Grows with new files only |
| Rate limits | API throttling per log group | Doesn't apply |
| Failure handling | Custom cursors and state files | Visibility timeout, retries, dead-letter queue |
| Adding a source | New poller per log group or bucket | Edit one stack parameter |

## Quick start

**1. Find the logs** (read-only, in the source account):
```bash
python3 tools/discover_log_sources.py --region us-east-1 --profile source-account
```

**2. Pre-fill a template** so the owner has nothing to type:
```bash
python3 tools/prefill_template.py \
  --source-tag acme-prod \
  --bucket acme-cloudtrail-logs:AWSLogs/ \
  --bucket acme-vpc-flow-logs \
  --trusted-principal arn:aws:iam::111122223333:role/log-ingest-consumer \
  --region us-east-1 --out acme-prod-onboarding.yaml
```
It prints the stack name, a generated ExternalId, and the consumer config entry to paste.

**3. The owner deploys it:** upload it in the CloudFormation console
([click-by-click guide](docs/ONBOARDING.md)), or run:
```bash
aws cloudformation deploy --stack-name log-ingest-acme-prod \
  --template-file acme-prod-onboarding.yaml --capabilities CAPABILITY_NAMED_IAM
```

**4. Run the consumer:**
```bash
cp config/sources.example.yaml config/sources.yaml     # add the stack's Outputs
export EXTERNAL_ID_ACME_PROD=...                         # from step 2
docker build -t log-ingest consumer/
docker run --rm -v "$PWD/config:/config" -e EXTERNAL_ID_ACME_PROD log-ingest
```

## Repository layout

| Path | What it is |
|---|---|
| `cloudformation/onboarding-stack.yaml` | **The template the account owner deploys** (generated, don't edit) |
| `cloudformation/onboarding-stack.src.yaml` | Template source, with a placeholder where the Lambda is embedded |
| `lambda/bucket_manager.py` | Custom resource: reconciles bucket notifications and reader permissions |
| `consumer/log_ingest/` | Consumer: supervisor, per-source workers, parsers, sinks, role assumption |
| `consumer/Dockerfile` | Non-root container image for the consumer |
| `config/sources.example.yaml` | Consumer config: one entry per stack, bucket-to-parser map |
| `tools/discover_log_sources.py` | Read-only discovery of where an account's logs land |
| `tools/prefill_template.py` | Pre-fills the template for one source and prints the matching config |
| `tools/build_template.py` | Embeds the Lambda into the template, minified to fit the inline limit |
| `tests/` | 43 tests against simulated AWS (moto), including the minified embedded Lambda |
| `docs/` | [Architecture](docs/ARCHITECTURE.md) · [Onboarding](docs/ONBOARDING.md) · [Log sources](docs/LOG-SOURCES.md) · [Operations](docs/OPERATIONS.md) |

## Design highlights

- **Least privilege, no long-lived secrets.** The default access mode is a cross-account
  role with an ExternalId. The reader can `GetObject` only on the listed buckets and
  prefixes, and only consume its own queue. An access-key mode exists for consumers
  outside AWS; the key goes to Secrets Manager, never to stack outputs.
- **Plays well with existing setups.** The Lambda only touches the notification entry
  it owns, identified by its ID, so other notifications on the same bucket are kept.
- **At-least-once delivery.** A message is deleted only after the sink accepts every
  event. Failures retry, then land in a dead-letter queue that holds them for 14 days.
- **Clear errors.** `BucketPrefixes` has to be `NONE`, or exactly one entry per bucket.
  A mismatch fails the stack with a message saying so, instead of silently pairing
  prefixes with the wrong buckets.
- **Hot reload.** The consumer re-reads its config every 30 s. Adding a source or a
  bucket needs no restart.
- **The code AWS runs is the code that's tested.** Every Lambda test runs twice: against
  the readable source, and against the minified copy embedded in the template.

## Development

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
python tools/build_template.py          # after editing lambda/ or the .src template
cfn-lint cloudformation/onboarding-stack.yaml
pytest -q
```
CI runs all three on every push.

## License

MIT, see [LICENSE](LICENSE).
