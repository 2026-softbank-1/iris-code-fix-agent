import gzip
import io
import tarfile
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from .canonical import canonical_json, sha256
from .contracts import SourceSpec, safe_path
from .errors import RepairError
from .paths import validate_file_tree

MAX_ARCHIVE = 10 * 1024 * 1024
MAX_EXPANDED = 20 * 1024 * 1024
MAX_FILE = 5 * 1024 * 1024
MAX_FILES = 1000


class _LimitExceeded(ValueError):
    """A size or count limit, reported separately from an unsafe archive."""


@dataclass(frozen=True)
class SourceFile:
    data: bytes
    mode: str = "100644"


@dataclass(frozen=True)
class SourceSnapshot:
    files: dict[str, SourceFile]
    manifest_sha256: str


def manifest_digest(files: dict[str, SourceFile]) -> str:
    return sha256(
        canonical_json(
            [
                {
                    "path": path,
                    "sha256": sha256(file.data),
                    "mode": file.mode,
                    "size": len(file.data),
                }
                for path, file in sorted(files.items())
            ]
        )
    )


def from_archive(data: bytes, spec: SourceSpec) -> SourceSnapshot:
    return _parse_archive(data, spec)


def inspect_trusted_snapshot(data: bytes, spec: SourceSpec) -> SourceSnapshot:
    """Discover the manifest of a WAS-issued GitHub snapshot (one wrapper)."""
    return _parse_archive(data, spec, discover=True)


def _parse_archive(data: bytes, spec: SourceSpec, *, discover=False) -> SourceSnapshot:
    if len(data) > MAX_ARCHIVE or sha256(data) != spec.archive_sha256:
        raise RepairError(
            "SOURCE_INTEGRITY", "Source archive digest or size is invalid"
        )
    entries: dict[str, SourceFile] = {}
    seen: set[str] = set()
    directories: set[str] = set()
    expanded = 0
    try:
        # Bound decompression before tarfile interprets extended/PAX metadata.
        if data.startswith(b"\x1f\x8b"):
            with gzip.GzipFile(fileobj=io.BytesIO(data)) as compressed:
                data = compressed.read(MAX_EXPANDED + 4 * 1024 * 1024 + 1)
            if len(data) > MAX_EXPANDED + 4 * 1024 * 1024:
                raise _LimitExceeded("decompressed archive size")
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
            for member in archive:
                name = member.name.rstrip("/") if member.isdir() else member.name
                safe_path(name)
                if name in seen:
                    raise ValueError("duplicate")
                seen.add(name)
                if len(seen) > MAX_FILES * 2:
                    raise _LimitExceeded("too many entries")
                if member.isdir():
                    directories.add(name)
                    continue
                if member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE):
                    raise ValueError("unsupported entry type")
                if member.size > MAX_FILE or len(entries) >= MAX_FILES:
                    raise _LimitExceeded("file size or count")
                expanded += member.size
                if expanded > MAX_EXPANDED:
                    raise _LimitExceeded("expanded size")
                file = archive.extractfile(member)
                if file is None:
                    raise ValueError("missing data")
                payload = file.read(MAX_FILE + 1)
                if len(payload) != member.size:
                    raise ValueError("invalid size")
                entries[name] = SourceFile(
                    payload, "100755" if member.mode & 0o111 else "100644"
                )
            validate_file_tree(entries)
            for directory in directories:
                parts = directory.split("/")
                if any(
                    "/".join(parts[:index]) in entries
                    for index in range(1, len(parts) + 1)
                ):
                    raise ValueError("File and directory path collision")
    except _LimitExceeded:
        raise RepairError(
            "SOURCE_TOO_LARGE", "Source archive exceeds a size or file-count limit"
        ) from None
    except (ValueError, tarfile.TarError, OSError, EOFError):
        raise RepairError(
            "SOURCE_UNSAFE", "Source archive contains unsafe or invalid entries"
        ) from None
    if not entries:
        raise RepairError("SOURCE_UNAVAILABLE", "Source archive contains no files")
    # Try exactly the raw layout and a single common wrapper. The pinned manifest
    # disambiguates a legitimate repository whose files all occupy one directory.
    candidates = [entries]
    prefixes = {name.split("/")[0] for name in entries}
    if len(prefixes) == 1 and all("/" in name for name in entries):
        candidates.append(
            {name.split("/", 1)[1]: file for name, file in entries.items()}
        )
    if discover:
        files = candidates[-1]
        return SourceSnapshot(files, manifest_digest(files))
    for files in candidates:
        digest = manifest_digest(files)
        if digest == spec.manifest_sha256:
            return SourceSnapshot(files, digest)
    raise RepairError("SOURCE_INTEGRITY", "Source manifest digest does not match")


def _allowed_host(host: str, allowed_hosts: tuple[str, ...]) -> bool:
    if allowed_hosts:
        return host.lower() in {v.lower() for v in allowed_hosts}
    # S3 regional and virtual-host endpoints; no arbitrary amazonaws service.
    import re

    return bool(
        re.fullmatch(
            r"(?:[a-z0-9][a-z0-9.-]*\.)?s3(?:[.-][a-z0-9-]+)?\.amazonaws\.com",
            host.lower(),
        )
    )


async def load_source(
    spec: SourceSpec, client: httpx.AsyncClient, allowed_hosts: tuple[str, ...] = ()
) -> SourceSnapshot:
    data = await download_source(spec, client, allowed_hosts)
    return from_archive(data, spec)


async def download_source(spec: SourceSpec, client, allowed_hosts=()) -> bytes:
    url = str(spec.download_url)
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.port not in (None, 443)
        or not _allowed_host(parsed.hostname or "", allowed_hosts)
    ):
        raise RepairError("SOURCE_HOST_FORBIDDEN", "Source host is not permitted")
    try:
        async with client.stream(
            "GET", url, follow_redirects=False, timeout=30
        ) as response:
            if response.status_code != 200:
                raise RepairError("SOURCE_UNAVAILABLE", "Source download failed")
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > MAX_ARCHIVE:
                    raise RepairError(
                        "SOURCE_TOO_LARGE", "Source archive exceeds size limit"
                    )
    except httpx.HTTPError:
        raise RepairError("SOURCE_UNAVAILABLE", "Source download failed") from None
    return bytes(data)
