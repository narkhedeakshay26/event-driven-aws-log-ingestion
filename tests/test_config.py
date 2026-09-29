import pytest

from log_ingest import config


def write(tmp_path, text):
    p = tmp_path / "c.yaml"
    p.write_text(text)
    return p


BASE = """
sources:
  - tenant: acme
    region: us-east-1
    queue_url: https://q
    auth: {role_arn: arn:aws:iam::1:role/r, external_id: "${EXT}"}
    buckets: {b1: cloudtrail}
"""


def test_env_vars_are_expanded(tmp_path, monkeypatch):
    monkeypatch.setenv("EXT", "s3cr3t")
    cfg = config.load(write(tmp_path, BASE))
    assert cfg["sources"][0]["auth"]["external_id"] == "s3cr3t"
    assert cfg["sources"][0]["name"] == "acme"


def test_missing_env_var_is_an_error(tmp_path, monkeypatch):
    monkeypatch.delenv("EXT", raising=False)
    with pytest.raises(ValueError, match="EXT is not set"):
        config.load(write(tmp_path, BASE))


@pytest.mark.parametrize("change,msg", [
    (("cloudtrail}", "nope}"), "unknown parser"),
    (('external_id: "${EXT}"', "x: 1"), "needs an external_id"),
    (("region: us-east-1", "region: ''"), "'region' is required"),
])
def test_validation_errors(tmp_path, monkeypatch, change, msg):
    monkeypatch.setenv("EXT", "x")
    with pytest.raises(ValueError, match=msg):
        config.load(write(tmp_path, BASE.replace(*change)))


def test_duplicate_source_names_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("EXT", "x")
    doubled = BASE + BASE.split("sources:\n", 1)[1]
    with pytest.raises(ValueError, match="duplicate source name"):
        config.load(write(tmp_path, doubled))


def test_relative_sink_path_is_next_to_config(tmp_path, monkeypatch):
    monkeypatch.setenv("EXT", "x")
    cfg = config.load(write(tmp_path, "sink: {type: file, path: out/e.jsonl}\n" + BASE))
    assert cfg["sink"]["path"] == str(tmp_path / "out" / "e.jsonl")


def test_account_and_region_are_enough(tmp_path, monkeypatch):
    monkeypatch.setenv("EXT", "x")
    cfg = config.load(write(tmp_path, """
sources:
  - tenant: acme
    account_id: "444455556666"
    region: eu-west-1
    auth: {external_id: "${EXT}"}
"""))
    s = cfg["sources"][0]
    assert s["queue_url"] == "https://sqs.eu-west-1.amazonaws.com/444455556666/log-ingest-444455556666-eu-west-1"
    assert s["auth"]["role_arn"] == "arn:aws:iam::444455556666:role/log-ingest-444455556666-eu-west-1-reader"
    assert s["buckets"] == "auto" and s["name"] == "acme-444455556666-eu-west-1"


def test_bad_account_id(tmp_path):
    with pytest.raises(ValueError, match="12 digits"):
        config.load(write(tmp_path, "sources: [{tenant: a1c, region: r, account_id: '123'}]\n"))


def test_needs_queue_or_account(tmp_path):
    with pytest.raises(ValueError, match="queue_url"):
        config.load(write(tmp_path, "sources: [{tenant: a1c, region: r}]\n"))
