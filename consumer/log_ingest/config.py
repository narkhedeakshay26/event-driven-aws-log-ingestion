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
    """Check each source and fill in what can be derived.

    A stack deployed from the shared template names everything after its account and
    region, so `account_id` + `region` is enough to find the queue and the reader role.
    """
    sources = cfg.get("sources") or []
    if not sources:
        raise ValueError("config needs at least one entry under 'sources'")
    names = set()
    for i, s in enumerate(sources):
        where = f"sources[{i}]"
        for field in ("tenant", "region"):
            if not s.get(field):
                raise ValueError(f"{where}: '{field}' is required")
        acct = str(s.get("account_id") or "")
        if acct and not re.fullmatch(r"\d{12}", acct):
            raise ValueError(f"{where}: account_id must be 12 digits")
        base = f"log-ingest-{acct}-{s['region']}" if acct else None
        if not s.get("queue_url"):
            if not base:
                raise ValueError(f"{where}: set 'queue_url', or 'account_id' to derive it")
            s["queue_url"] = f"https://sqs.{s['region']}.amazonaws.com/{acct}/{base}"
        auth = s.setdefault("auth", {})
        if auth.get("external_id") and not auth.get("role_arn") and base:
            auth["role_arn"] = f"arn:aws:iam::{acct}:role/{base}-reader"
        if auth.get("role_arn") and not auth.get("external_id"):
            raise ValueError(f"{where}: role_arn needs an external_id")

        name = s.get("name") or (f"{s['tenant']}-{acct}-{s['region']}" if acct else s["tenant"])
        if name in names:
            raise ValueError(f"{where}: duplicate source name {name!r} (set 'name' to tell them apart)")
        names.add(name)
        s["name"] = name

        buckets = s.get("buckets", "auto")
        if buckets != "auto" and not isinstance(buckets, dict):
            raise ValueError(f"{where}: 'buckets' must be 'auto' or a map of bucket: parser")
        s["buckets"] = buckets
        for bucket, parser in (buckets.items() if isinstance(buckets, dict) else []):
            if parser not in PARSERS:
                raise ValueError(f"{where}: bucket {bucket} uses unknown parser {parser!r}; "
                                 f"choose from {sorted(PARSERS)}")
        default = s.get("default_parser")
        if default and default not in PARSERS:
            raise ValueError(f"{where}: unknown default_parser {default!r}")
    return cfg


def load(path):
    path = Path(path)
    cfg = validate(_expand(yaml.safe_load(path.read_text()) or {}))
    sink = cfg.setdefault("sink", {"type": "stdout"})
    if sink.get("path") and not Path(sink["path"]).is_absolute():
        sink["path"] = str(path.parent / sink["path"])   # relative to the config file, not the cwd
    return cfg
