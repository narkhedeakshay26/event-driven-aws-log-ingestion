"""Hot reload: changed or removed sources restart or stop; untouched ones keep running."""
import threading

from log_ingest import __main__ as main_mod


class FakeWorker:
    started = []

    def __init__(self, source, sink):
        self.source, self.stopped = source, threading.Event()
        FakeWorker.started.append(source["name"])

    def run(self, once=False):
        self.stopped.wait(5)

    def stop(self):
        self.stopped.set()


def cfg(*sources):
    return {"sink": {"type": "stdout"}, "sources": [dict(s) for s in sources]}


def test_apply_diffs_sources(monkeypatch):
    monkeypatch.setattr(main_mod, "SourceWorker", FakeWorker)
    FakeWorker.started = []
    a = {"name": "a", "tenant": "a", "region": "r", "queue_url": "q", "buckets": {"x": "text"}}
    b = {"name": "b", "tenant": "b", "region": "r", "queue_url": "q", "buckets": {"y": "text"}}
    sup = main_mod.Supervisor("unused.yaml")

    sup.apply(cfg(a, b))
    assert sorted(FakeWorker.started) == ["a", "b"]

    b_changed = {**b, "buckets": {"y": "text", "z": "jsonl"}}
    sup.apply(cfg(a, b_changed))                 # a untouched, b restarted
    assert FakeWorker.started.count("a") == 1 and FakeWorker.started.count("b") == 2

    sup.apply(cfg(b_changed))                    # a removed
    assert sorted(sup.workers) == ["b"]
    for name in list(sup.workers):
        sup._stop(name)
