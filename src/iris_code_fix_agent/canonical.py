import hashlib
import json


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def semantic_digest(request) -> str:
    payload = request.model_dump(mode="json", by_alias=True)
    payload["source"].pop("downloadUrl", None)
    return sha256(canonical_json(payload))
