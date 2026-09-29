"""AWS sessions per source: assume the source's cross-account role (refreshed before
expiry), use an access key, a named profile, or fall back to the default chain."""
import threading
from datetime import datetime, timedelta, timezone

import boto3


class SourceSession:
    REFRESH_MARGIN = timedelta(minutes=5)

    def __init__(self, source, base_session=None):
        self.source = source
        self.auth = source.get("auth", {})
        self.base = base_session or boto3.Session()
        self._lock = threading.Lock()
        self._session = None
        self._expires = None

    def _new_session(self):
        region = self.source["region"]
        a = self.auth
        if a.get("role_arn"):
            creds = self.base.client("sts", region_name=region).assume_role(
                RoleArn=a["role_arn"], ExternalId=a["external_id"],
                RoleSessionName=f"log-ingest-{self.source['name']}"[:64], DurationSeconds=3600,
            )["Credentials"]
            self._expires = creds["Expiration"]
            return boto3.Session(aws_access_key_id=creds["AccessKeyId"],
                                 aws_secret_access_key=creds["SecretAccessKey"],
                                 aws_session_token=creds["SessionToken"], region_name=region)
        self._expires = None
        if a.get("access_key_id"):
            return boto3.Session(aws_access_key_id=a["access_key_id"],
                                 aws_secret_access_key=a["secret_access_key"], region_name=region)
        if a.get("profile"):
            return boto3.Session(profile_name=a["profile"], region_name=region)
        return boto3.Session(region_name=region)

    def clients(self):
        """Return (sqs, s3) clients, re-assuming the role when it is about to expire."""
        with self._lock:
            now = datetime.now(timezone.utc)
            if self._session is None or (self._expires and self._expires - now < self.REFRESH_MARGIN):
                self._session = self._new_session()
                self._sqs = self._session.client("sqs")
                self._s3 = self._session.client("s3")
            return self._sqs, self._s3
