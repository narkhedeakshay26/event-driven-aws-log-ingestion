import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in ("consumer", "lambda", "tools"):
    sys.path.insert(0, str(ROOT / p))

# Never touch a real AWS account from tests.
os.environ.update(AWS_ACCESS_KEY_ID="testing", AWS_SECRET_ACCESS_KEY="testing",
                  AWS_SESSION_TOKEN="testing", AWS_DEFAULT_REGION="us-east-1")
for var in ("AWS_PROFILE", "AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE", "AWS_ENDPOINT_URL"):
    os.environ.pop(var, None)

REGION = "us-east-1"


@pytest.fixture
def root():
    return ROOT
