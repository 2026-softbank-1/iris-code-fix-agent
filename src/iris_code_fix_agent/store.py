"""Durable single-host result receipts; model calls are never replayed implicitly."""

import fcntl
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .canonical import sha256
from .errors import RepairError


class ResultStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.locks = self.root / "locks"
        self.locks.mkdir(exist_ok=True, mode=0o700)
        self.db = self.root / "receipts.sqlite3"
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS repairs ("
                "request_id TEXT PRIMARY KEY, input_digest TEXT NOT NULL, "
                "status TEXT NOT NULL, result TEXT, error_code TEXT, "
                "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )
        self.db.chmod(0o600)

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.db, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @contextmanager
    def lock(self, request_id: str):
        """A process crash releases this lock but preserves the RUNNING receipt."""
        path = self.locks / f"{sha256(request_id.encode('utf-8'))}.lock"
        with path.open("a+b") as handle:
            os.chmod(path, 0o600)
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RepairError(
                    "REPAIR_IN_PROGRESS", "Request is running.", 409
                ) from None
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def get(self, request_id: str) -> dict | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM repairs WHERE request_id = ?", (request_id,)
            ).fetchone()
        if row is None:
            return None
        return {
            "requestId": row["request_id"],
            "inputDigest": row["input_digest"],
            "status": row["status"],
            "result": json.loads(row["result"]) if row["result"] else None,
            "errorCode": row["error_code"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }

    def begin(self, request_id: str, input_digest: str) -> dict | None:
        """Called while holding the process lock. Existing receipt means no new call."""
        existing = self.get(request_id)
        if existing is not None:
            if existing["inputDigest"] != input_digest:
                raise RepairError("IDEMPOTENCY_CONFLICT", "Request input changed.", 409)
            if existing["status"] == "RUNNING":
                self.finish(
                    request_id, "UNKNOWN_OUTCOME", error_code="MODEL_CALL_UNKNOWN"
                )
                return self.get(request_id)
            return existing
        now = datetime.now(UTC).isoformat()
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO repairs VALUES (?, ?, 'RUNNING', NULL, NULL, ?, ?)",
                (request_id, input_digest, now, now),
            )
        return None

    def finish(self, request_id: str, status: str, result=None, error_code=None):
        with self.connect() as connection:
            connection.execute(
                "UPDATE repairs SET status=?, result=?, error_code=?, updated_at=? "
                "WHERE request_id=? AND status='RUNNING'",
                (
                    status,
                    json.dumps(result, ensure_ascii=False)
                    if result is not None
                    else None,
                    error_code,
                    datetime.now(UTC).isoformat(),
                    request_id,
                ),
            )

    def recover(self, request_id: str) -> dict | None:
        record = self.get(request_id)
        if record is None or record["status"] != "RUNNING":
            return record
        try:
            with self.lock(request_id):
                self.finish(
                    request_id, "UNKNOWN_OUTCOME", error_code="MODEL_CALL_UNKNOWN"
                )
        except RepairError as exc:
            if exc.code != "REPAIR_IN_PROGRESS":
                raise
        return self.get(request_id)

    def write_artifacts(self, request_id: str, artifacts: dict[str, bytes]):
        directory = self.root / "results" / sha256(request_id.encode("utf-8"))
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        for name, data in artifacts.items():
            destination = directory / name
            temporary = directory / f".{name}.tmp"
            with temporary.open("wb") as handle:
                os.chmod(temporary, 0o600)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(destination)
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def artifact(self, request_id: str, name: str) -> Path:
        if name not in {"patch.diff", "changes.json", "manifest.json"}:
            raise RepairError("ARTIFACT_NOT_FOUND", "Artifact not found.", 404)
        record = self.get(request_id)
        if record is None or record["status"] != "SUCCEEDED":
            raise RepairError("ARTIFACT_NOT_FOUND", "Artifact not available.", 404)
        path = self.root / "results" / sha256(request_id.encode("utf-8")) / name
        if not path.is_file():
            raise RepairError("ARTIFACT_NOT_FOUND", "Artifact not found.", 404)
        return path
