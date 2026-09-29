"""Load and validate the consumer config (YAML). ${VAR} is replaced from the environment,
so secrets such as external IDs can stay out of the file."""
import os
import re
from pathlib import Path

import yaml

from .parsers import PARSERS

_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand(value):
    if isinstance(value, str):
        def sub(m):
            if m.group(1) not in os.environ:
                raise ValueError(f"environment variable {m.group(1)} is not set")
            return os.environ[m.group(1)]
        return _ENV.sub(sub, value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


def validate(cfg):
    sources = cfg.get("sources") or []
    if not sources:
        raise ValueError("config needs at least one entry under 'sources'")
    names = set()
    for i, s in enumerate(sources):
        where = f"sources[{i}]"
        for field in ("tenant", "region", "queue_url", "buckets"):
            if not s.get(field):
                raise ValueError(f"{where}: '{field}' is required")
        name = s.get("name") or s["tenant"]
        if name in names:
            raise ValueError(f"{where}: duplicate source name {name!r} (set 'name' to tell them apart)")
        names.add(name)
        s["name"] = name
        for bucket, parser in s["buckets"].items():
            if parser not in PARSERS:
                raise ValueError(f"{where}: bucket {bucket} uses unknown parser {parser!r}; "
                                 f"choose from {sorted(PARSERS)}")
        auth = s.setdefault("auth", {})
        if auth.get("role_arn") and not auth.get("external_id"):
            raise ValueError(f"{where}: role_arn needs an external_id")
    return cfg


def load(path):
    path = Path(path)
    cfg = validate(_expand(yaml.safe_load(path.read_text()) or {}))
    sink = cfg.setdefault("sink", {"type": "stdout"})
    if sink.get("path") and not Path(sink["path"]).is_absolute():
        sink["path"] = str(path.parent / sink["path"])   # relative to the config file, not the cwd
    return cfg
