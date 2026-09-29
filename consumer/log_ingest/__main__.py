"""Run the consumer.

  python -m log_ingest --config config/sources.yaml          # run forever
  python -m log_ingest --config config/sources.yaml --once   # drain queues, then exit

Each source runs in its own thread. The config file is re-read every 30 s, so
sources and buckets can be added, changed or removed without a restart.
"""
import argparse
import json
import logging
import signal
import threading
import time
from pathlib import Path

from . import config as config_mod
from .sinks import build_sink
from .worker import SourceWorker

log = logging.getLogger("log_ingest")


class Supervisor:
    def __init__(self, path, once=False):
        self.path = Path(path)
        self.once = once
        self.workers = {}          # name -> (fingerprint, worker, thread)
        self.sink = None
        self.stopping = threading.Event()

    def _start(self, source):
        worker = SourceWorker(source, self.sink)
        thread = threading.Thread(target=worker.run, kwargs={"once": self.once},
                                  name=f"src-{source['name']}", daemon=True)
        thread.start()
        self.workers[source["name"]] = (json.dumps(source, sort_keys=True), worker, thread)

    def _stop(self, name):
        _, worker, thread = self.workers.pop(name)
        worker.stop()
        thread.join(timeout=30)

    def apply(self, cfg):
        if self.sink is None:
            self.sink = build_sink(cfg.get("sink", {"type": "stdout"}))
        wanted = {s["name"]: s for s in cfg["sources"]}
        for name in list(self.workers):
            if name not in wanted or self.workers[name][0] != json.dumps(wanted[name], sort_keys=True):
                log.info("source %s removed or changed, stopping its worker", name)
                self._stop(name)
        for name, source in wanted.items():
            if name not in self.workers:
                self._start(source)

    def run(self):
        mtime = self.path.stat().st_mtime
        self.apply(config_mod.load(self.path))
        while not self.stopping.is_set():
            if self.once:
                for _, _, t in list(self.workers.values()):
                    t.join()
                break
            self.stopping.wait(30)
            try:
                new_mtime = self.path.stat().st_mtime
                if new_mtime != mtime:
                    mtime = new_mtime
                    self.apply(config_mod.load(self.path))
                    log.info("config reloaded")
            except Exception:
                log.exception("config reload failed; keeping the previous config")
        for name in list(self.workers):
            self._stop(name)
        if self.sink:
            self.sink.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True)
    ap.add_argument("--once", action="store_true", help="drain every queue, then exit")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args()
    logging.basicConfig(level=args.log_level.upper(),
                        format="%(asctime)s %(levelname)s %(threadName)s %(message)s")

    sup = Supervisor(args.config, once=args.once)
    signal.signal(signal.SIGTERM, lambda *_: sup.stopping.set())
    signal.signal(signal.SIGINT, lambda *_: sup.stopping.set())
    sup.run()


if __name__ == "__main__":
    main()
