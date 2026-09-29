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
| `[acme-…] bucket/key -> 42 events` | Normal |
| `can't tell the log type of bucket/key` | Object outside AWS's standard layout: add the bucket to `buckets`, or set `default_parser` |
| `failed; message will be retried` | Parser or sink error, stack trace follows |
| `polling failed, retrying in Ns` | Can't reach SQS, or can't assume the role (check the ExternalId and account ID) |

Bucket manager log lines, in the customer's CloudWatch Logs under
`/aws/lambda/<ManagerFunction>`: `wired`, `unwired` and `skipped`, each with the reason
and where the bucket was found.

Suggested alarms in the customer account: `ApproximateAgeOfOldestMessage > 900` on the
queue (consumer behind or down), and `ApproximateNumberOfMessagesVisible > 0` on the DLQ.

## A customer adds a log source

| Their AutoRescan | What happens |
|---|---|
| Yes | Wired within 24 h automatically |
| No | They run the `RescanNow` output command, or update the stack |

Nothing changes on your side: `buckets: auto` detects the new files' log type.

## Redrive the DLQ after a fix

```bash
aws sqs start-message-move-task \
  --source-arn arn:aws:sqs:REGION:ACCOUNT:log-ingest-ACCOUNT-REGION-dlq
```
With no destination given, SQS moves the messages back to the queue they came from.

## Rotate a customer's ExternalId

Generate a new one (`python3 -c "import secrets; print(secrets.token_urlsafe(24))"`). Ask
the customer to update the stack with it as the `ExternalId` parameter, or send a new link
and have them update. Then update the environment variable the consumer reads and restart
it. In between, messages simply wait in the queue.

## Change your consumer principal

Rebuild and re-upload the template with the new `--trusted-principal`. Existing stacks
keep the old value until each customer runs an Update with the new template, so keep
the old role valid until they have.

## Offboard

The customer deletes the stack. That removes the queue, the role and the notifications it
added. Delete their entry from `sources.yaml`.

## Scaling

- **One consumer, many customers:** each source gets its own thread. Hundreds of
  low-volume sources fit in one small container.
- **One busy source:** run more replicas with the same config. SQS hands each message to
  one receiver at a time, so they share the work without coordinating.
- **Large objects:** the visibility timeout (300 s) must exceed the time to fetch and
  parse the largest file. Raise it in the template if needed.
