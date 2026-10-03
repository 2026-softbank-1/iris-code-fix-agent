"""Content-addressed, private S3 repair artifacts; no signed URLs in receipts."""

import base64
import re

from botocore.exceptions import BotoCoreError, ClientError

from .canonical import sha256
from .errors import RepairError


class S3Artifacts:
    def __init__(self, client, bucket: str, prefix: str = "repairs"):
        if not bucket or not re.fullmatch(r"[a-z0-9][a-z0-9.-]+", bucket):
            raise ValueError("A valid repair artifact bucket is required")
        if not re.fullmatch(r"[A-Za-z0-9_/-]+", prefix) or ".." in prefix:
            raise ValueError("Invalid artifact prefix")
        self.client, self.bucket, self.prefix = client, bucket, prefix.rstrip("/")

    def put(self, request_id: str, name: str, data: bytes) -> dict:
        if name not in {"patch.diff", "changes.json", "manifest.json", "source.tar.gz"}:
            raise ValueError("Unsupported artifact")
        digest = sha256(data)
        key = f"{self.prefix}/{sha256(request_id.encode())}/{digest}/{name}"
        try:
            response = self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=data,
                ChecksumSHA256=base64.b64encode(bytes.fromhex(digest)).decode(),
                Metadata={"sha256": digest},
                ContentType="application/octet-stream",
            )
        except (BotoCoreError, ClientError):
            raise RepairError(
                "S3_UPLOAD_FAILED", "Repair artifact upload failed.", 502
            ) from None
        return {
            "bucket": self.bucket,
            "key": key,
            "sha256": digest,
            "byteLength": len(data),
            "versionId": response.get("VersionId"),
        }

    def download_url(self, reference: dict) -> str:
        params = {"Bucket": reference["bucket"], "Key": reference["key"]}
        if reference.get("versionId"):
            params["VersionId"] = reference["versionId"]
        return self.client.generate_presigned_url(
            "get_object", Params=params, ExpiresIn=300
        )
