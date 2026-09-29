"""Turn one S3 log object into individual events.

Every parser takes the object's raw bytes (already gunzipped) and its key,
and yields dicts. Add a new log type by writing a function and registering it
in PARSERS.
"""
import json
import shlex

# AWS documents ALB access-log fields in this fixed order.
ALB_FIELDS = [
    "type", "time", "elb", "client_port", "target_port", "request_processing_time",
    "target_processing_time", "response_processing_time", "elb_status_code",
    "target_status_code", "received_bytes", "sent_bytes", "request", "user_agent",
    "ssl_cipher", "ssl_protocol", "target_group_arn", "trace_id", "domain_name",
    "chosen_cert_arn", "matched_rule_priority", "request_creation_time",
    "actions_executed", "redirect_url", "error_reason", "target_port_list",
    "target_status_code_list", "classification", "classification_reason", "conn_trace_id",
]


def _lines(data):
    for line in data.decode("utf-8", errors="replace").splitlines():
        if line.strip():
            yield line


def cloudtrail(data, key):
    # Digest files are integrity proofs, not events.
    if "CloudTrail-Digest" in key:
        return
    doc = json.loads(data)
    yield from doc.get("Records", [])


def jsonl(data, key):
    for line in _lines(data):
        yield json.loads(line)


def vpc_flow(data, key):
    """Space-separated with a header row naming the fields (custom formats work too)."""
    rows = _lines(data)
    header = next(rows, "").split()
    for line in rows:
        yield dict(zip(header, line.split()))


def alb(data, key):
    for line in _lines(data):
        values = shlex.split(line)
        event = dict(zip(ALB_FIELDS, values))
        if len(values) > len(ALB_FIELDS):
            event["extra_fields"] = values[len(ALB_FIELDS):]
        yield event


def text(data, key):
    for line in _lines(data):
        yield {"message": line}


PARSERS = {
    "cloudtrail": cloudtrail,
    "vpc_flow": vpc_flow,
    "alb": alb,
    "waf": jsonl,        # WAF delivers newline-delimited JSON
    "jsonl": jsonl,
    "text": text,
}
