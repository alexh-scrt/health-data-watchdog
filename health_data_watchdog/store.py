"""SQLite-backed snapshot store for Health Data Watchdog.

This module provides a persistent store for tracking dataset fetch history,
content hashes, and change records.  All data is kept in a single SQLite
database file whose path is configured via :class:`~health_data_watchdog.config.Config`.

Schema overview
---------------
* ``snapshots``    — one row per successful fetch; records URL, hash, file path,
  fetch timestamp, HTTP status, and byte size.
* ``change_records`` — one row per detected change event; references the
  snapshot that introduced the change and stores a JSON-serialised diff summary.
* ``schema_version`` — single-row metadata table used for future migrations.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Generator, Iterator, List, Optional


# ---------------------------------------------------------------------------
# Schema constants
# ---------------------------------------------------------------------------

SCHEMA_VERSION: int = 1

_DDL_SCHEMA_VERSION = """
CREATE TABLE IF NOT EXISTS schema_version (
    version   INTEGER NOT NULL,
    applied_at TEXT NOT NULL
);
"""

_DDL_SNAPSHOTS = """
CREATE TABLE IF NOT EXISTS snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset_key   TEXT    NOT NULL,
    url           TEXT    NOT NULL,
    fetched_at    TEXT    NOT NULL,
    content_hash  TEXT    NOT NULL,
    file_path     TEXT    NOT NULL,
    http_status   INTEGER NOT NULL DEFAULT 200,
    byte_size     INTEGER NOT NULL DEFAULT 0,
    is_duplicate  INTEGER NOT NULL DEFAULT 0  -- 1 if hash matches previous fetch
);
"""

_DDL_SNAPSHOTS_IDX_KEY = """
CREATE INDEX IF NOT EXISTS idx_snapshots_dataset_key
    ON snapshots (dataset_key, fetched_at);
"""

_DDL_SNAPSHOTS_IDX_HASH = """
CREATE INDEX IF NOT EXISTS idx_snapshots_hash
    ON snapshots (content_hash);
"""

_DDL_CHANGE_RECORDS = """
CREATE TABLE IF NOT EXISTS change_records (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id    INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    dataset_key    TEXT    NOT NULL,
    detected_at    TEXT    NOT NULL,
    change_type    TEXT    NOT NULL,  -- 'content', 'schema', 'deletion', 'new'
    severity       TEXT    NOT NULL DEFAULT 'info',  -- 'info', 'warning', 'critical'
    row_delta      INTEGER NOT NULL DEFAULT 0,
    col_delta      INTEGER NOT NULL DEFAULT 0,
    summary        TEXT    NOT NULL DEFAULT '',
    diff_json      TEXT    NOT NULL DEFAULT '{}'
);
"""

_DDL_CHANGE_RECORDS_IDX = """
CREATE INDEX IF NOT EXISTS idx_change_records_dataset_key
    ON change_records (dataset_key, detected_at);
"""


# ---------------------------------------------------------------------------
# Helper types
# ---------------------------------------------------------------------------


class SnapshotRecord:
    """A row from the ``snapshots`` table.

    Attributes:
        id: Auto-assigned row ID (``None`` before insertion).
        dataset_key: Machine-readable dataset identifier.
        url: The URL from which the snapshot was fetched.
        fetched_at: UTC timestamp (ISO-8601) of the fetch.
        content_hash: SHA-256 hex digest of the raw downloaded content.
        file_path: Absolute path where the raw snapshot file is stored.
        http_status: HTTP response status code.
        byte_size: Size of the downloaded content in bytes.
        is_duplicate: ``True`` if the hash matches the immediately preceding
            snapshot for the same dataset key.
    """

    __slots__ = (
        "id",
        "dataset_key",
        "url",
        "fetched_at",
        "content_hash",
        "file_path",
        "http_status",
        "byte_size",
        "is_duplicate",
    )

    def __init__(
        self,
        dataset_key: str,
        url: str,
        fetched_at: str,
        content_hash: str,
        file_path: str,
        http_status: int = 200,
        byte_size: int = 0,
        is_duplicate: bool = False,
        id: Optional[int] = None,
    ) -> None:
        self.id = id
        self.dataset_key = dataset_key
        self.url = url
        self.fetched_at = fetched_at
        self.content_hash = content_hash
        self.file_path = file_path
        self.http_status = http_status
        self.byte_size = byte_size
        self.is_duplicate = is_duplicate

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"SnapshotRecord(id={self.id!r}, dataset_key={self.dataset_key!r}, "
            f"fetched_at={self.fetched_at!r}, content_hash={self.content_hash[:12]!r}...)"
        )

    def to_dict(self) -> Dict[str, Any]:
        """Serialise the record to a plain dictionary."""
        return {
            "id": self.id,
            "dataset_key": self.dataset_key,
            "url": self.url,
            "fetched_at": self.fetched_at,
            "content_hash": self.content_hash,
            "file_path": self.file_path,
            "http_status": self.http_status,
            "byte_size": self.byte_size,
            "is_duplicate": self.is_duplicate,
        }


class ChangeRecord:
    """A row from the ``change_records`` table.

    Attributes:
        id: Auto-assigned row ID (``None`` before insertion).
        snapshot_id: Foreign key to the ``snapshots`` row that introduced
            this change.
        dataset_key: Machine-readable dataset identifier.
        detected_at: UTC timestamp (ISO-8601) when the change was detected.
        change_type: Category of change: ``'content'``, ``'schema'``,
            ``'deletion'``, or ``'new'``.
        severity: Alert severity: ``'info'``, ``'warning'``, or ``'critical'``.
        row_delta: Net change in row count (positive = rows added, negative
            = rows removed).
        col_delta: Net change in column count.
        summary: Short human-readable description of the change.
        diff_json: JSON-serialised detailed diff data (arbitrary structure).
    """

    __slots__ = (
        "id",
        "snapshot_id",
        "dataset_key",
        "detected_at",
        "change_type",
        "severity",
        "row_delta",
        "col_delta",
        "summary",
        "diff_json",
    )

    def __init__(
        self,
        snapshot_id: int,
        dataset_key: str,
        detected_at: str,
        change_type: str,
        severity: str = "info",
        row_delta: int = 0,
        col_delta: int = 0,
        summary: str = "",
        diff_json: str = "{}",
        id: Optional[int] = None,
    ) -> None:
        self.id = id
        self.snapshot_id = snapshot_id
        self.dataset_key = dataset_key
        self.detected_at = detected_at
        self.change_type = change_type
        self.severity = severity
        self.row_delta = row_delta
        self.col_delta = col_delta
        self.summary = summary
        self.diff_json = diff_json

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"ChangeRecord(id={self.id!r}, dataset_key={self.dataset_key!r}, "
            f"change_type={self.change_type!r}, severity={self.severity!r}, "
            f"detected_at={self.detected_at!r})"
        )

    def diff_data(self) -> Dict[str, Any]:
        """Deserialise :attr:`diff_json` and return as a dict."""
        try:
            return json.loads(self.diff_json)  # type: ignore[no-any-return]
        except (json.JSONDecodeError, TypeError):
            return {}

    def to_dict(self) -> Dict[str, Any]:
        """Serialise the record to a plain dictionary."""
        return {
            "id": self.id,
            "snapshot_id": self.snapshot_id,
            "dataset_key": self.dataset_key,
            "detected_at": self.detected_at,
            "change_type": self.change_type,
            "severity": self.severity,
            "row_delta": self.row_delta,
            "col_delta": self.col_delta,
            "summary": self.summary,
            "diff_json": self.diff_json,
        }


# ---------------------------------------------------------------------------
# Store class
# ---------------------------------------------------------------------------


class SnapshotStore:
    """SQLite-backed persistent store for dataset snapshots and change records.

    The store creates the database file and schema on first use.  All
    operations are committed immediately (autocommit style) to keep the
    interface simple and avoid the caller needing to manage transactions.

    Usage::

        store = SnapshotStore(Path("watchdog.db"))
        snap_id = store.insert_snapshot(record)
        store.close()

    It also works as a context manager::

        with SnapshotStore(Path("watchdog.db")) as store:
            snap_id = store.insert_snapshot(record)

    Args:
        db_path: Filesystem path to the SQLite database file.  The parent
            directory is created automatically if it does not exist.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection = sqlite3.connect(
            str(self._db_path), check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        self._apply_schema()

    # ------------------------------------------------------------------
    # Context manager support
    # ------------------------------------------------------------------

    def __enter__(self) -> "SnapshotStore":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _apply_schema(self) -> None:
        """Create tables and indexes if they do not already exist."""
        with self._conn:
            self._conn.execute(_DDL_SCHEMA_VERSION)
            self._conn.execute(_DDL_SNAPSHOTS)
            self._conn.execute(_DDL_SNAPSHOTS_IDX_KEY)
            self._conn.execute(_DDL_SNAPSHOTS_IDX_HASH)
            self._conn.execute(_DDL_CHANGE_RECORDS)
            self._conn.execute(_DDL_CHANGE_RECORDS_IDX)

            # Insert the schema version row only if the table is empty.
            cursor = self._conn.execute("SELECT COUNT(*) FROM schema_version;")
            if cursor.fetchone()[0] == 0:
                now = datetime.now(timezone.utc).isoformat()
                self._conn.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (?, ?);",
                    (SCHEMA_VERSION, now),
                )

    @contextmanager
    def _transaction(self) -> Generator[sqlite3.Connection, None, None]:
        """Yield the connection within an explicit transaction."""
        with self._conn:
            yield self._conn

    @staticmethod
    def _row_to_snapshot(row: sqlite3.Row) -> SnapshotRecord:
        """Convert a :class:`sqlite3.Row` to a :class:`SnapshotRecord`."""
        return SnapshotRecord(
            id=row["id"],
            dataset_key=row["dataset_key"],
            url=row["url"],
            fetched_at=row["fetched_at"],
            content_hash=row["content_hash"],
            file_path=row["file_path"],
            http_status=row["http_status"],
            byte_size=row["byte_size"],
            is_duplicate=bool(row["is_duplicate"]),
        )

    @staticmethod
    def _row_to_change(row: sqlite3.Row) -> ChangeRecord:
        """Convert a :class:`sqlite3.Row` to a :class:`ChangeRecord`."""
        return ChangeRecord(
            id=row["id"],
            snapshot_id=row["snapshot_id"],
            dataset_key=row["dataset_key"],
            detected_at=row["detected_at"],
            change_type=row["change_type"],
            severity=row["severity"],
            row_delta=row["row_delta"],
            col_delta=row["col_delta"],
            summary=row["summary"],
            diff_json=row["diff_json"],
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the underlying database connection."""
        try:
            self._conn.close()
        except Exception:  # pragma: no cover
            pass

    @property
    def db_path(self) -> Path:
        """Return the filesystem path to the SQLite database file."""
        return self._db_path

    # ------------------------------------------------------------------
    # Snapshot CRUD
    # ------------------------------------------------------------------

    def insert_snapshot(
        self,
        record: SnapshotRecord,
        *,
        auto_mark_duplicate: bool = True,
    ) -> int:
        """Persist a new snapshot record and return its assigned row ID.

        If *auto_mark_duplicate* is ``True`` (the default), the
        ``is_duplicate`` flag is automatically set to ``True`` when the
        provided content hash matches the most recent snapshot for the
        same ``dataset_key``.

        Args:
            record: The :class:`SnapshotRecord` to insert.  The ``id``
                attribute is ignored; the database assigns the ID.
            auto_mark_duplicate: Whether to detect and mark duplicate
                hashes automatically.

        Returns:
            The ``rowid`` assigned by SQLite for the new row.

        Raises:
            sqlite3.DatabaseError: On any database error.
        """
        is_dup = record.is_duplicate
        if auto_mark_duplicate:
            prev = self.get_latest_snapshot(record.dataset_key)
            if prev is not None and prev.content_hash == record.content_hash:
                is_dup = True

        with self._transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO snapshots
                    (dataset_key, url, fetched_at, content_hash, file_path,
                     http_status, byte_size, is_duplicate)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    record.dataset_key,
                    record.url,
                    record.fetched_at,
                    record.content_hash,
                    record.file_path,
                    record.http_status,
                    record.byte_size,
                    1 if is_dup else 0,
                ),
            )
        return cursor.lastrowid  # type: ignore[return-value]

    def get_snapshot(self, snapshot_id: int) -> Optional[SnapshotRecord]:
        """Retrieve a snapshot by its primary key.

        Args:
            snapshot_id: The ``id`` of the snapshot to fetch.

        Returns:
            The matching :class:`SnapshotRecord`, or ``None`` if not found.
        """
        row = self._conn.execute(
            "SELECT * FROM snapshots WHERE id = ?;", (snapshot_id,)
        ).fetchone()
        return self._row_to_snapshot(row) if row else None

    def get_latest_snapshot(self, dataset_key: str) -> Optional[SnapshotRecord]:
        """Return the most-recently fetched snapshot for a dataset.

        Args:
            dataset_key: The dataset whose latest snapshot to retrieve.

        Returns:
            The most recent :class:`SnapshotRecord`, or ``None`` if no
            snapshots exist for the given key.
        """
        row = self._conn.execute(
            """
            SELECT * FROM snapshots
            WHERE dataset_key = ?
            ORDER BY fetched_at DESC, id DESC
            LIMIT 1;
            """,
            (dataset_key,),
        ).fetchone()
        return self._row_to_snapshot(row) if row else None

    def get_snapshots_for_dataset(
        self,
        dataset_key: str,
        *,
        limit: Optional[int] = None,
        include_duplicates: bool = True,
    ) -> List[SnapshotRecord]:
        """Return all snapshots for a dataset, newest first.

        Args:
            dataset_key: The dataset key to query.
            limit: If set, return at most this many records.
            include_duplicates: If ``False``, skip snapshots whose
                ``is_duplicate`` flag is set.

        Returns:
            List of :class:`SnapshotRecord` objects ordered by
            ``fetched_at`` descending.
        """
        where_clause = "WHERE dataset_key = ?"
        params: List[Any] = [dataset_key]

        if not include_duplicates:
            where_clause += " AND is_duplicate = 0"

        sql = f"SELECT * FROM snapshots {where_clause} ORDER BY fetched_at DESC, id DESC"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        sql += ";"

        rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_snapshot(r) for r in rows]

    def get_all_snapshots(
        self,
        *,
        limit: Optional[int] = None,
    ) -> List[SnapshotRecord]:
        """Return all snapshots across all datasets, newest first.

        Args:
            limit: If set, return at most this many records.

        Returns:
            List of :class:`SnapshotRecord` objects.
        """
        sql = "SELECT * FROM snapshots ORDER BY fetched_at DESC, id DESC"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        sql += ";"
        rows = self._conn.execute(sql).fetchall()
        return [self._row_to_snapshot(r) for r in rows]

    def delete_snapshot(self, snapshot_id: int) -> bool:
        """Delete a snapshot record and its associated change records.

        Because ``change_records`` has ``ON DELETE CASCADE`` on the
        ``snapshot_id`` foreign key, child change records are removed
        automatically.

        Args:
            snapshot_id: The ``id`` of the snapshot to delete.

        Returns:
            ``True`` if a row was deleted, ``False`` if no row with that
            ID existed.
        """
        with self._transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM snapshots WHERE id = ?;", (snapshot_id,)
            )
        return cursor.rowcount > 0

    def snapshot_exists_for_hash(
        self, dataset_key: str, content_hash: str
    ) -> bool:
        """Check whether a snapshot with the given hash already exists.

        Useful for deduplication checks before storing a new snapshot file.

        Args:
            dataset_key: The dataset key to scope the search.
            content_hash: SHA-256 hex digest to look up.

        Returns:
            ``True`` if at least one snapshot with this hash exists for
            the given dataset key.
        """
        row = self._conn.execute(
            "SELECT 1 FROM snapshots WHERE dataset_key = ? AND content_hash = ? LIMIT 1;",
            (dataset_key, content_hash),
        ).fetchone()
        return row is not None

    # ------------------------------------------------------------------
    # Change record CRUD
    # ------------------------------------------------------------------

    def insert_change_record(self, record: ChangeRecord) -> int:
        """Persist a new change record and return its assigned row ID.

        Args:
            record: The :class:`ChangeRecord` to insert.  The ``id``
                attribute is ignored.

        Returns:
            The ``rowid`` assigned by SQLite.

        Raises:
            sqlite3.IntegrityError: If ``snapshot_id`` does not reference
                an existing snapshot row.
        """
        with self._transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO change_records
                    (snapshot_id, dataset_key, detected_at, change_type,
                     severity, row_delta, col_delta, summary, diff_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    record.snapshot_id,
                    record.dataset_key,
                    record.detected_at,
                    record.change_type,
                    record.severity,
                    record.row_delta,
                    record.col_delta,
                    record.summary,
                    record.diff_json,
                ),
            )
        return cursor.lastrowid  # type: ignore[return-value]

    def get_change_record(self, change_id: int) -> Optional[ChangeRecord]:
        """Retrieve a change record by its primary key.

        Args:
            change_id: The ``id`` of the change record to fetch.

        Returns:
            The matching :class:`ChangeRecord`, or ``None`` if not found.
        """
        row = self._conn.execute(
            "SELECT * FROM change_records WHERE id = ?;", (change_id,)
        ).fetchone()
        return self._row_to_change(row) if row else None

    def get_change_records_for_dataset(
        self,
        dataset_key: str,
        *,
        limit: Optional[int] = None,
        severity: Optional[str] = None,
    ) -> List[ChangeRecord]:
        """Return change records for a specific dataset, newest first.

        Args:
            dataset_key: The dataset key to query.
            limit: Maximum number of records to return.
            severity: If provided, filter to this severity level only
                (``'info'``, ``'warning'``, or ``'critical'``).

        Returns:
            List of :class:`ChangeRecord` objects.
        """
        where_clause = "WHERE dataset_key = ?"
        params: List[Any] = [dataset_key]

        if severity is not None:
            where_clause += " AND severity = ?"
            params.append(severity)

        sql = (
            f"SELECT * FROM change_records {where_clause} "
            "ORDER BY detected_at DESC, id DESC"
        )
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        sql += ";"

        rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_change(r) for r in rows]

    def get_all_change_records(
        self,
        *,
        limit: Optional[int] = None,
        severity: Optional[str] = None,
    ) -> List[ChangeRecord]:
        """Return change records across all datasets, newest first.

        Args:
            limit: Maximum number of records to return.
            severity: If provided, filter to this severity level only.

        Returns:
            List of :class:`ChangeRecord` objects.
        """
        where_clause = ""
        params: List[Any] = []

        if severity is not None:
            where_clause = "WHERE severity = ?"
            params.append(severity)

        sql = (
            f"SELECT * FROM change_records {where_clause} "
            "ORDER BY detected_at DESC, id DESC"
        )
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        sql += ";"

        rows = self._conn.execute(sql, params).fetchall()
        return [self._row_to_change(r) for r in rows]

    def get_change_records_for_snapshot(self, snapshot_id: int) -> List[ChangeRecord]:
        """Return all change records associated with a specific snapshot.

        Args:
            snapshot_id: The snapshot whose change records to retrieve.

        Returns:
            List of :class:`ChangeRecord` objects.
        """
        rows = self._conn.execute(
            "SELECT * FROM change_records WHERE snapshot_id = ? ORDER BY id ASC;",
            (snapshot_id,),
        ).fetchall()
        return [self._row_to_change(r) for r in rows]

    def delete_change_record(self, change_id: int) -> bool:
        """Delete a specific change record.

        Args:
            change_id: The ``id`` of the record to delete.

        Returns:
            ``True`` if a row was deleted, ``False`` otherwise.
        """
        with self._transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM change_records WHERE id = ?;", (change_id,)
            )
        return cursor.rowcount > 0

    # ------------------------------------------------------------------
    # Aggregate / utility queries
    # ------------------------------------------------------------------

    def count_snapshots(self, dataset_key: Optional[str] = None) -> int:
        """Return the total number of snapshots, optionally scoped to one dataset.

        Args:
            dataset_key: If provided, count only snapshots for this key.

        Returns:
            Integer row count.
        """
        if dataset_key is not None:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM snapshots WHERE dataset_key = ?;",
                (dataset_key,),
            ).fetchone()
        else:
            row = self._conn.execute("SELECT COUNT(*) FROM snapshots;").fetchone()
        return row[0]

    def count_change_records(self, dataset_key: Optional[str] = None) -> int:
        """Return the total number of change records, optionally scoped.

        Args:
            dataset_key: If provided, count only change records for this key.

        Returns:
            Integer row count.
        """
        if dataset_key is not None:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM change_records WHERE dataset_key = ?;",
                (dataset_key,),
            ).fetchone()
        else:
            row = self._conn.execute("SELECT COUNT(*) FROM change_records;").fetchone()
        return row[0]

    def get_dataset_keys(self) -> List[str]:
        """Return a sorted list of all distinct dataset keys in the snapshot table.

        Returns:
            Alphabetically sorted list of dataset key strings.
        """
        rows = self._conn.execute(
            "SELECT DISTINCT dataset_key FROM snapshots ORDER BY dataset_key;"
        ).fetchall()
        return [r[0] for r in rows]

    def prune_old_snapshots(
        self, dataset_key: str, keep: int = 10
    ) -> int:
        """Delete older snapshots for a dataset, keeping only the most recent *keep* rows.

        This is useful for keeping the database and raw snapshot directory
        from growing unboundedly.  Associated change records are removed
        automatically via ``ON DELETE CASCADE``.

        Args:
            dataset_key: The dataset whose old snapshots to prune.
            keep: Number of most-recent snapshots to retain (default 10).

        Returns:
            The number of rows deleted.

        Raises:
            ValueError: If *keep* is less than 1.
        """
        if keep < 1:
            raise ValueError(f"'keep' must be >= 1, got {keep!r}.")

        # Find the IDs to delete: all except the N most recent.
        rows = self._conn.execute(
            """
            SELECT id FROM snapshots
            WHERE dataset_key = ?
            ORDER BY fetched_at DESC, id DESC;
            """,
            (dataset_key,),
        ).fetchall()

        ids_to_delete = [r[0] for r in rows[keep:]]
        if not ids_to_delete:
            return 0

        placeholders = ",".join("?" for _ in ids_to_delete)
        with self._transaction() as conn:
            cursor = conn.execute(
                f"DELETE FROM snapshots WHERE id IN ({placeholders});",
                ids_to_delete,
            )
        return cursor.rowcount

    def get_schema_version(self) -> int:
        """Return the schema version stored in the database.

        Returns:
            The integer schema version, or 0 if the table is empty.
        """
        row = self._conn.execute(
            "SELECT version FROM schema_version ORDER BY rowid DESC LIMIT 1;"
        ).fetchone()
        return row[0] if row else 0


# ---------------------------------------------------------------------------
# Convenience factory
# ---------------------------------------------------------------------------


def open_store(db_path: Path) -> SnapshotStore:
    """Open (or create) a :class:`SnapshotStore` at the given path.

    Convenience wrapper so callers do not need to import the class directly.

    Args:
        db_path: Filesystem path to the SQLite database file.

    Returns:
        An initialised :class:`SnapshotStore` instance.
    """
    return SnapshotStore(db_path)
