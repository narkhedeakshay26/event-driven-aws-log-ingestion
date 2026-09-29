"""Where parsed events go. A sink gets a batch and must raise if it can't deliver
it; the message is then left on the queue and retried (at-least-once delivery)."""
import json
import sys
import threading
from pathlib import Path


class StdoutSink:
    def __init__(self, cfg):
        self._lock = threading.Lock()

    def write(self, events):
        with self._lock:
            for e in events:
                sys.stdout.write(json.dumps(e, default=str) + "\n")
            sys.stdout.flush()

    def close(self):
        pass


class FileSink:
    """Appends JSON lines to a file, one per event."""

    def __init__(self, cfg):
        self.path = Path(cfg.get("path", "events.jsonl"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def write(self, events):
        with self._lock, self.path.open("a") as f:
            for e in events:
                f.write(json.dumps(e, default=str) + "\n")

    def close(self):
        pass


class KafkaSink:
    """Produces to a Kafka topic. Requires: pip install confluent-kafka."""

    def __init__(self, cfg):
        from confluent_kafka import Producer  # optional dependency

        self.topic = cfg["topic"]
        self.key_field = cfg.get("key_field", "tenant")
        self.producer = Producer({"bootstrap.servers": cfg["bootstrap_servers"],
                                  "enable.idempotence": True, **cfg.get("extra", {})})
        self._errors = []
        self._lock = threading.Lock()   # one flush at a time keeps error tracking per batch

    def _delivered(self, err, msg):
        if err:
            self._errors.append(err)

    def write(self, events):
        with self._lock:
            self._write(events)

    def _write(self, events):
        self._errors = []
        for e in events:
            self.producer.produce(self.topic, json.dumps(e, default=str).encode(),
                                  key=str(e.get(self.key_field, "")).encode(),
                                  on_delivery=self._delivered)
            self.producer.poll(0)
        remaining = self.producer.flush(30)
        if remaining or self._errors:
            raise RuntimeError(f"kafka delivery failed: {remaining} pending, errors={self._errors[:3]}")

    def close(self):
        self.producer.flush(10)


SINKS = {"stdout": StdoutSink, "file": FileSink, "kafka": KafkaSink}


def build_sink(cfg):
    kind = cfg.get("type", "stdout")
    if kind not in SINKS:
        raise ValueError(f"unknown sink type {kind!r}; choose from {sorted(SINKS)}")
    return SINKS[kind](cfg)
