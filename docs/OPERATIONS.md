# Operations

## Health at a glance

```bash
# Messages waiting, in flight, and in the DLQ
aws sqs get-queue-attributes --queue-url "$QUEUE" \
  --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible
aws sqs get-queue-attributes --queue-url "$DLQ" --attribute-names ApproximateNumberOfMessages
```

Consumer log lines to know:

| Line | Meaning |
|---|---|
| `[acme-prod] bucket/key -> 42 events` | Normal |
| `left on queue ... is not in source acme-prod's 'buckets' map` | Bucket wired on the AWS side, missing from `sources.yaml` |
| `failed; message will be retried` | Parser or sink error, stack trace follows |
| `polling failed, retrying in Ns` | Can't reach SQS, or can't assume the role (check the ExternalId) |

Suggested CloudWatch alarms in the source account: `ApproximateAgeOfOldestMessage > 900`
on the queue (consumer behind or down), and `ApproximateNumberOfMessagesVisible > 0` on
the DLQ.

## Add or remove a bucket

1. The owner runs a stack **Update** with the new `BucketNames` (and `BucketPrefixes`).
2. You edit the source's `buckets` map in `sources.yaml`. It's picked up within 30 s.

## Redrive the DLQ after a fix

```bash
aws sqs start-message-move-task \
  --source-arn arn:aws:sqs:REGION:ACCOUNT:log-ingest-acme-prod-dlq
```
With no destination given, SQS moves the messages back to the queue they came from.

## Rotate the ExternalId

Generate a new one, run a stack Update with it, then update the environment variable the
consumer reads and restart it. Between the stack update and the consumer restart,
AssumeRole fails and messages simply wait in the queue.

## Offboard a source

The owner deletes the stack. That removes the queue, the role and the bucket notifications.
Then delete the source's entry from `sources.yaml`.

## Scaling

- **One consumer, many sources:** each source gets its own thread. Hundreds of low-volume
  sources fit in one small container.
- **One busy source:** run more consumer replicas with the same config. SQS hands each
  message to one receiver at a time, so they share the work without coordinating.
- **Large objects:** the visibility timeout (300 s) must exceed the time to fetch and
  parse the biggest file. Raise it in the template if needed.
