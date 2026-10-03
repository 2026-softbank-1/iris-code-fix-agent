import io
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError

from iris_code_fix_agent.canonical import sha256
from iris_code_fix_agent.errors import RepairError
from iris_code_fix_agent.storage import S3Artifacts


def setup_store(data=b"candidate"):
    client = Mock()
    body = io.BytesIO(data)
    client.get_object.return_value = {"Body": body}
    store = S3Artifacts(client, "private-fixtures")
    reference = {
        "bucket": "private-fixtures",
        "key": "repairs/run/source.tar.gz",
        "sha256": sha256(b"candidate"),
        "byteLength": 9,
        "versionId": "v1",
    }
    return client, body, store, reference


def test_retrieval_pins_version_and_checks_bytes():
    client, body, store, reference = setup_store()
    assert store.get(reference) == b"candidate"
    assert body.closed
    client.get_object.assert_called_once_with(
        Bucket="private-fixtures", Key=reference["key"], VersionId="v1"
    )


@pytest.mark.parametrize("data", [b"changed!!", b"candidate extra", b"short"])
def test_retrieval_rejects_corrupt_truncated_or_oversized_bytes(data):
    _, body, store, reference = setup_store(data)
    with pytest.raises(RepairError, match="Stored artifact bytes changed"):
        store.get(reference)
    assert body.closed


@pytest.mark.parametrize(
    "field,value",
    [
        ("bucket", "foreign-bucket"),
        ("key", "outside/source.tar.gz"),
        ("sha256", "bad"),
        ("byteLength", True),
        ("byteLength", -1),
        ("byteLength", 11 * 1024 * 1024),
    ],
)
def test_retrieval_rejects_invalid_reference_before_network(field, value):
    client, _, store, reference = setup_store()
    reference[field] = value
    with pytest.raises(RepairError):
        store.get(reference)
    client.get_object.assert_not_called()


def test_s3_missing_object_has_stable_error():
    client, _, store, reference = setup_store()
    client.get_object.side_effect = ClientError(
        {"Error": {"Code": "NoSuchKey"}}, "GetObject"
    )
    with pytest.raises(RepairError) as failure:
        store.get(reference)
    assert failure.value.code == "S3_DOWNLOAD_FAILED"
