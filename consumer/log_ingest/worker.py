"""One worker per source: long-poll its queue, fetch each new object, parse it,
hand events to the sink, then delete the message.

Delivery is at-least-once: a message is deleted only after every event from every
object it references has been accepted by the sink. Anything that fails is left on
the queue, becomes visible again after the visibility timeout, and goes to the
dead-letter queue after the stack's maxReceiveCount.
"""
import gzip
import json
import logging
import threading
import time
from datetime import datetime, timezone
from urllib.parse import unquote_plus

from .aws import SourceSession
from .parsers import PARSERS, infer

log = logging.getLogger("log_ingest")
MAX_OBJECT_BYTES = 512 * 1024 * 1024


class SkipMessage(Exception):
    """The message can't be processed yet (e.g. unknown bucket); leave it for retry."""


def _gunzip(data):
    return gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data


class SourceWorker:
    def __init__(self, source, sink, session=None, batch_size=500):
        self.source = source
        self.name = source["name"]
        self.sink = sink
        self.session = session or SourceSession(source)
        self.batch_size = batch_size
        self.stop_event = threading.Event()
        self.stats = {"messages": 0, "objects": 0, "events": 0, "errors": 0}

    # ------------------------------------------------------------------ helpers
    def _envelope(self, bucket, key, log_type, event):
        return {
            "tenant": self.source["tenant"],
            "source": self.name,
            "log_type": log_type,
            "s3": {"bucket": bucket, "key": key, "region": self.source["region"]},
            "ingested_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "event": event,
        }

    def log_type(self, bucket, key):
        """Explicit bucket map first, then AWS's key layout, then the source's default."""
        buckets = self.source["buckets"]
        if isinstance(buckets, dict):
            if bucket in buckets:
                return buckets[bucket]
            if not self.source.get("default_parser"):
                raise SkipMessage(f"bucket {bucket} is not in source {self.name}'s 'buckets' map "
                                  "(added on the AWS side but not here yet?)")
        kind = infer(bucket, key) or self.source.get("default_parser")
        if not kind:
            raise SkipMessage(f"can't tell the log type of {bucket}/{key}; add the bucket to "
                              f"'buckets' or set 'default_parser' on source {self.name}")
        return kind

    def process_object(self, s3, bucket, key, size=None):
        log_type = self.log_type(bucket, key)
        if size and size > MAX_OBJECT_BYTES:
            raise SkipMessage(f"{bucket}/{key} is {size} bytes, over the {MAX_OBJECT_BYTES} limit")
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        batch, count = [], 0
        for event in PARSERS[log_type](_gunzip(body), key):
            batch.append(self._envelope(bucket, key, log_type, event))
            if len(batch) >= self.batch_size:
                self.sink.write(batch)
                count += len(batch)
                batch = []
        if batch:
            self.sink.write(batch)
            count += len(batch)
        return count

    def handle_message(self, s3, message):
        body = json.loads(message["Body"])
        if body.get("Event") == "s3:TestEvent":   # sent once when a notification is created
            return 0
        total = 0
        for record in body.get("Records", []):
            if not record.get("eventName", "").startswith("ObjectCreated"):
                continue
            obj = record["s3"]["object"]
            bucket = record["s3"]["bucket"]["name"]
            key = unquote_plus(obj["key"])        # S3 URL-encodes keys in events
            n = self.process_object(s3, bucket, key, obj.get("size"))
            self.stats["objects"] += 1
            total += n
            log.info("[%s] %s/%s -> %d events", self.name, bucket, key, n)
        return total

    # ------------------------------------------------------------------ main loop
    def poll_once(self, wait_seconds=20):
        """Receive one batch of messages. Returns how many were received."""
        sqs, s3 = self.session.clients()
        resp = sqs.receive_message(QueueUrl=self.source["queue_url"], MaxNumberOfMessages=10,
                                   WaitTimeSeconds=wait_seconds, AttributeNames=["ApproximateReceiveCount"])
        messages = resp.get("Messages", [])
        for m in messages:
            try:
                self.stats["events"] += self.handle_message(s3, m)
                sqs.delete_message(QueueUrl=self.source["queue_url"], ReceiptHandle=m["ReceiptHandle"])
                self.stats["messages"] += 1
            except SkipMessage as e:
                self.stats["errors"] += 1
                log.warning("[%s] left on queue (attempt %s): %s", self.name,
                            m.get("Attributes", {}).get("ApproximateReceiveCount", "?"), e)
            except Exception:
                self.stats["errors"] += 1
                log.exception("[%s] failed; message will be retried", self.name)
        return len(messages)

    def run(self, once=False):
        b = self.source["buckets"]
        log.info("[%s] consuming %s (%s)", self.name, self.source["queue_url"],
                 "log types detected from object keys" if b == "auto" else f"{len(b)} buckets")
        backoff = 1
        while not self.stop_event.is_set():
            try:
                received = self.poll_once(wait_seconds=1 if once else 20)
                backoff = 1
                if once and received == 0:
                    break
            except Exception:
                log.exception("[%s] polling failed, retrying in %ss", self.name, backoff)
                self.stop_event.wait(backoff)
                backoff = min(backoff * 2, 60)
        log.info("[%s] stopped: %s", self.name, self.stats)

    def stop(self):
        self.stop_event.set()
