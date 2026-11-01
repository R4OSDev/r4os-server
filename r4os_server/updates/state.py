from __future__ import annotations
from dataclasses import dataclass
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator
from ..registration.state import utc_text

@dataclass(frozen=True)
class PackageCache:
    state: str
    filename: str
    size: int
    mtime_ns: int
    metadata: dict[str, Any] | None
    error: str | None
    published_at: float | None


class StateStore:
    def __init__(self, database, initial_gateway_rate=None):
        self.database=Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            version=connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2):
                raise ValueError("Unsupported update database version")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("CREATE TABLE IF NOT EXISTS package_cache (state TEXT NOT NULL, filename TEXT NOT NULL, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, metadata_json TEXT, error TEXT, published_at REAL, PRIMARY KEY(state,filename))")
            if version == 0:
                connection.execute("PRAGMA user_version=1")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        try:
            with connection:
                yield connection
        finally:
            connection.close()


    def package_cache_get(self, state: str, filename: str) -> PackageCache | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT state, filename, size, mtime_ns, metadata_json, error, published_at
                FROM package_cache WHERE state = ? AND filename = ?
                """,
                (state, filename),
            ).fetchone()
        if row is None:
            return None
        return PackageCache(
            state=row["state"],
            filename=row["filename"],
            size=int(row["size"]),
            mtime_ns=int(row["mtime_ns"]),
            metadata=json.loads(row["metadata_json"]) if row["metadata_json"] else None,
            error=row["error"],
            published_at=float(row["published_at"]) if row["published_at"] is not None else None,
        )


    def package_cache_put(self, record: PackageCache) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO package_cache(state, filename, size, mtime_ns, metadata_json, error, published_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(state, filename) DO UPDATE SET
                    size = excluded.size,
                    mtime_ns = excluded.mtime_ns,
                    metadata_json = excluded.metadata_json,
                    error = excluded.error,
                    published_at = excluded.published_at
                """,
                (
                    record.state,
                    record.filename,
                    record.size,
                    record.mtime_ns,
                    json.dumps(record.metadata, sort_keys=True) if record.metadata is not None else None,
                    record.error,
                    record.published_at,
                ),
            )


    def package_cache_delete(self, state: str, filename: str) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM package_cache WHERE state = ? AND filename = ?", (state, filename))


    def package_cache_delete_missing(self, state: str, filenames: set[str]) -> None:
        with self._connect() as connection:
            rows = connection.execute("SELECT filename FROM package_cache WHERE state = ?", (state,)).fetchall()
            for row in rows:
                if row["filename"] not in filenames:
                    connection.execute(
                        "DELETE FROM package_cache WHERE state = ? AND filename = ?",
                        (state, row["filename"]),
                    )


