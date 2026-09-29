import shutil
import subprocess
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml

import build_template
import onboard


def load(path):
    class Loader(yaml.SafeLoader):
        pass
    Loader.add_multi_constructor("!", lambda loader, suffix, node: None)
    return yaml.load(path.read_text(), Loader=Loader)


PRINCIPAL = "arn:aws:iam::111122223333:role/log-ingest-consumer"


def test_committed_template_matches_sources():
    assert build_template.OUT.read_text() == build_template.render()[0], "run tools/build_template.py"


def test_build_can_bake_in_the_trusted_principal(tmp_path):
    out = tmp_path / "t.yaml"
    build_template.main(["--trusted-principal", PRINCIPAL, "--out", str(out)])
    assert load(out)["Parameters"]["TrustedPrincipalArn"]["Default"] == PRINCIPAL


def test_onboarding_link_carries_only_per_customer_values(capsys):
    onboard.main(["--tenant", "acme", "--trusted-principal", PRINCIPAL, "--region", "eu-west-1",
                  "--template-url", "https://bkt.s3.amazonaws.com/log-ingest/onboarding-stack.yaml"])
    out = capsys.readouterr().out
    link = next(l.strip() for l in out.splitlines() if "quickcreate" in l).split(": ", 1)[1]
    assert link.startswith("https://eu-west-1.console")
    fragment = urlsplit(link).fragment
    assert fragment.startswith("/stacks/quickcreate?")
    q = parse_qs(fragment.split("?", 1)[1])
    assert q["templateURL"] == ["https://bkt.s3.amazonaws.com/log-ingest/onboarding-stack.yaml"]
    assert q["stackName"] == ["log-ingest"]
    assert q["param_TrustedPrincipalArn"] == [PRINCIPAL]
    assert len(q["param_ExternalId"][0]) >= 24
    assert q["param_IngestCloudTrail"] == ["Yes"] and q["param_IngestConfigLogs"] == ["No"]
    assert q["param_AutoRescan"] == ["No"]
    assert "buckets: auto" in out and "EXTERNAL_ID_ACME" in out


def test_each_customer_gets_a_different_external_id(capsys):
    args = ["--trusted-principal", PRINCIPAL, "--region", "us-east-1", "--template-url", "https://x/t.yaml"]
    onboard.main(["--tenant", "acme"] + args)
    onboard.main(["--tenant", "globex"] + args)
    ids = [l.split("param_ExternalId=")[1].split("&")[0] for l in capsys.readouterr().out.splitlines()
           if "param_ExternalId=" in l]
    assert len(ids) == 2 and ids[0] != ids[1]


def test_prefilled_file_option(tmp_path):
    out = tmp_path / "acme.yaml"
    onboard.main(["--tenant", "acme", "--trusted-principal", PRINCIPAL, "--region", "us-east-1",
                  "--external-id", "fixed-external-id-123456", "--out", str(out)])
    p = load(out)["Parameters"]
    assert p["ExternalId"]["Default"] == "fixed-external-id-123456"
    assert p["IngestCloudTrail"]["AllowedValues"] == ["Yes", "No"]      # dropdowns untouched
    if shutil.which("cfn-lint"):
        subprocess.run(["cfn-lint", str(out)], check=True)


def links(out):
    return [l.strip() for l in out.splitlines() if "quickcreate" in l]


def test_no_region_lets_the_customer_choose(capsys):
    onboard.main(["--tenant", "acme", "--trusted-principal", PRINCIPAL, "--template-url", "https://x/t.yaml"])
    out = capsys.readouterr().out
    [link] = links(out)
    assert link.startswith("https://console.aws.amazon.com/cloudformation/home#/stacks/quickcreate?")
    assert "region=" not in link.split("#")[0]
    assert "region: <Region from their Outputs tab>" in out


def test_multiple_regions_one_link_each_same_external_id(capsys):
    onboard.main(["--tenant", "acme", "--trusted-principal", PRINCIPAL, "--template-url", "https://x/t.yaml",
                  "--region", "us-east-1,eu-west-1,us-east-1"])
    out = capsys.readouterr().out
    ls = links(out)
    assert [l.split(":", 1)[0] for l in ls] == ["us-east-1", "eu-west-1"]          # deduplicated, ordered
    assert "https://eu-west-1.console.aws.amazon.com/cloudformation/home?region=eu-west-1#" in ls[1]
    ids = {l.split("param_ExternalId=")[1].split("&")[0] for l in ls}
    assert len(ids) == 1                                                             # one secret per customer
    assert out.count("  - tenant: acme") == 2


def test_dropdown_preselection(capsys):
    onboard.main(["--tenant", "acme", "--trusted-principal", PRINCIPAL, "--region", "us-east-1",
                  "--template-url", "https://x/t.yaml", "--log-types", "cloudtrail,config", "--auto-rescan", "yes"])
    link = next(l.strip() for l in capsys.readouterr().out.splitlines() if "quickcreate" in l).split(": ", 1)[1]
    q = parse_qs(urlsplit(link).fragment.split("?", 1)[1])
    assert (q["param_IngestCloudTrail"], q["param_IngestConfigLogs"], q["param_IngestWafLogs"],
            q["param_AutoRescan"]) == (["Yes"], ["Yes"], ["No"], ["Yes"])


@pytest.mark.parametrize("args", [
    ["--tenant", "acme", "--trusted-principal", PRINCIPAL, "--region", "us-east-1", "--out", "x", "--log-types", "nope"],
    ["--tenant", "Bad_Tag", "--trusted-principal", PRINCIPAL, "--region", "us-east-1", "--out", "x.yaml"],
    ["--tenant", "acme", "--trusted-principal", "not-an-arn", "--region", "us-east-1", "--out", "x.yaml"],
    ["--tenant", "acme", "--trusted-principal", PRINCIPAL, "--region", "r"],          # no link, no file
    ["--tenant", "acme", "--trusted-principal", PRINCIPAL, "--region", "us-east-1", "--out", "x", "--account-id", "12"],
    ["--tenant", "acme", "--trusted-principal", PRINCIPAL, "--region", "us-east-1,mars", "--out", "x"],
])
def test_onboard_rejects_bad_input(args):
    with pytest.raises(SystemExit):
        onboard.main(args)
