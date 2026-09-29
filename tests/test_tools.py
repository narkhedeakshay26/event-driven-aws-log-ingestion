import shutil
import subprocess

import pytest
import yaml

import build_template
import prefill_template


def load(path):
    class Loader(yaml.SafeLoader):
        pass
    Loader.add_multi_constructor("!", lambda loader, suffix, node: None)
    return yaml.load(path.read_text(), Loader=Loader)


def test_committed_template_matches_lambda_source():
    text, size = build_template.render()
    assert (build_template.OUT.read_text() == text), "run tools/build_template.py"
    assert size <= build_template.LIMIT


def test_prefill_sets_every_default(tmp_path, capsys):
    out = tmp_path / "t.yaml"
    prefill_template.main(["--source-tag", "acme-prod", "--bucket", "trail-logs:AWSLogs/",
                           "--bucket", "flow-logs", "--trusted-principal",
                           "arn:aws:iam::111122223333:role/consumer", "--out", str(out)])
    p = load(out)["Parameters"]
    assert p["SourceTag"]["Default"] == "acme-prod"
    assert p["BucketNames"]["Default"] == "trail-logs,flow-logs"
    assert p["BucketPrefixes"]["Default"] == "AWSLogs/,NONE"
    assert len(p["ExternalId"]["Default"]) >= 24
    printed = capsys.readouterr().out
    assert "log-ingest-acme-prod" in printed and "EXTERNAL_ID_ACME_PROD" in printed
    if shutil.which("cfn-lint"):
        subprocess.run(["cfn-lint", str(out)], check=True)


def test_prefill_all_none_prefixes_collapse_to_single_none(tmp_path):
    out = tmp_path / "t.yaml"
    prefill_template.main(["--source-tag", "acme", "--bucket", "a-logs", "--bucket", "b-logs",
                           "--access-mode", "AccessKey", "--out", str(out)])
    assert load(out)["Parameters"]["BucketPrefixes"]["Default"] == "NONE"


@pytest.mark.parametrize("args", [
    ["--source-tag", "Bad_Tag", "--bucket", "ok-bucket"],
    ["--source-tag", "acme", "--bucket", "ok-bucket", "--bucket", "ok-bucket", "--access-mode", "AccessKey"],
    ["--source-tag", "acme", "--bucket", "ok-bucket"],       # role mode without trusted principal
])
def test_prefill_rejects_bad_input(tmp_path, args):
    with pytest.raises(SystemExit):
        prefill_template.main(args + ["--out", str(tmp_path / "x.yaml")])
