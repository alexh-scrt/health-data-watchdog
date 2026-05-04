"""Tests for the health_data_watchdog.store module.

Covers schema creation, snapshot CRUD operations, change record CRUD,
duplicate detection, cascade deletes, prune logic, and aggregate queries.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pytest

from health_data_watchdog.store import (
    ChangeRecord,
    SCHEMA_VERSION,
    SnapshotRecord,
    SnapshotStore,
    open_store,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path: Path) -> SnapshotStore:  # type: ignore[misc]
    """Yield a fresh SnapshotStore backed by a temp SQLite file."""
    s = SnapshotStore(tmp_path / "test.db")
    yield s
    s.close()


def _ts(offset: int = 0) -> str:
    """Return an ISO-8601 UTC timestamp, optionally offset by *offset* seconds."""
    from datetime import timedelta
    return (datetime.now(timezone.utc).replace(microsecond=0)
            + timedelta(seconds=offset)).isoformat()


def _snap(
    key: str = "test_ds",
    url: str = "https://example.com/data.csv",
    content_hash: str = "abc123",
    file_path: str = "/tmp/data.csv",
    fetched_at: Optional[str] = None,
    http_status: int = 200,
    byte_size: int = 1024,
    is_duplicate: bool = False,
) -> SnapshotRecord:
    """Create a minimal SnapshotRecord for testing."""
    return SnapshotRecord(
        dataset_key=key,
        url=url,
        fetched_at=fetched_at or _ts(),
        content_hash=content_hash,
        file_path=file_path,
        http_status=http_status,
        byte_size=byte_size,
        is_duplicate=is_duplicate,
    )


def _change(
    snapshot_id: int,
    key: str = "test_ds",
    change_type: str = "content",
    severity: str = "info",
    row_delta: int = 5,
    col_delta: int = 0,
    summary: str = "Some rows changed",
    diff_json: Optional[str] = None,
    detected_at: Optional[str] = None,
) -> ChangeRecord:
    """Create a minimal ChangeRecord for testing."""
    return ChangeRecord(
        snapshot_id=snapshot_id,
        dataset_key=key,
        detected_at=detected_at or _ts(),
        change_type=change_type,
        severity=severity,
        row_delta=row_delta,
        col_delta=col_delta,
        summary=summary,
        diff_json=diff_json or "{}",
    )


# ---------------------------------------------------------------------------
# Schema / initialisation
# ---------------------------------------------------------------------------


class TestSchemaCreation:
    """Tests that verify the database schema is created correctly."""

    def test_db_file_is_created(self, tmp_path: Path) -> None:
        """Opening a store creates the SQLite file on disk."""
        db_file = tmp_path / "sub" / "watchdog.db"
        assert not db_file.exists()
        s = SnapshotStore(db_file)
        s.close()
        assert db_file.exists()

    def test_schema_version_is_set(self, store: SnapshotStore) -> None:
        """The schema_version table is populated after init."""
        assert store.get_schema_version() == SCHEMA_VERSION

    def test_tables_exist(self, tmp_path: Path) -> None:
        """All expected tables are present after init."""
        db_file = tmp_path / "watchdog.db"
        s = SnapshotStore(db_file)
        s.close()

        conn = sqlite3.connect(str(db_file))
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table';"
            ).fetchall()
        }
        conn.close()

        assert "snapshots" in tables
        assert "change_records" in tables
        assert "schema_version" in tables

    def test_schema_version_not_duplicated_on_reopen(self, tmp_path: Path) -> None:
        """Re-opening the store does not insert a second schema_version row."""
        db_file = tmp_path / "watchdog.db"
        s1 = SnapshotStore(db_file)
        s1.close()
        s2 = SnapshotStore(db_file)
        version = s2.get_schema_version()
        s2.close()

        conn = sqlite3.connect(str(db_file))
        count = conn.execute("SELECT COUNT(*) FROM schema_version;").fetchone()[0]
        conn.close()
        assert count == 1
        assert version == SCHEMA_VERSION


# ---------------------------------------------------------------------------
# open_store factory
# ---------------------------------------------------------------------------


class TestOpenStore:
    """Tests for the open_store convenience function."""

    def test_open_store_returns_snapshot_store(self, tmp_path: Path) -> None:
        """open_store returns a SnapshotStore instance."""
        s = open_store(tmp_path / "factory.db")
        try:
            assert isinstance(s, SnapshotStore)
        finally:
            s.close()

    def test_open_store_creates_file(self, tmp_path: Path) -> None:
        """open_store creates the database file."""
        db_file = tmp_path / "factory.db"
        s = open_store(db_file)
        s.close()
        assert db_file.exists()


# ---------------------------------------------------------------------------
# Context manager
# ---------------------------------------------------------------------------


class TestContextManager:
    """Tests that SnapshotStore works as a context manager."""

    def test_context_manager_closes_connection(self, tmp_path: Path) -> None:
        """Using the store as a context manager closes the connection on exit."""
        db_file = tmp_path / "ctx.db"
        with SnapshotStore(db_file) as s:
            snap_id = s.insert_snapshot(_snap())
            assert snap_id > 0
        # After exit, further operations should raise or indicate closed state.
        with pytest.raises(Exception):
            s.count_snapshots()  # connection is closed


# ---------------------------------------------------------------------------
# Snapshot INSERT
# ---------------------------------------------------------------------------


class TestInsertSnapshot:
    """Tests for insert_snapshot."""

    def test_insert_returns_positive_id(self, store: SnapshotStore) -> None:
        """Inserting a snapshot returns a positive integer ID."""
        snap_id = store.insert_snapshot(_snap())
        assert isinstance(snap_id, int)
        assert snap_id >= 1

    def test_insert_increments_count(self, store: SnapshotStore) -> None:
        """Each inserted snapshot increments the count."""
        store.insert_snapshot(_snap(content_hash="h1"))
        store.insert_snapshot(_snap(content_hash="h2"))
        assert store.count_snapshots() == 2

    def test_insert_persists_all_fields(self, store: SnapshotStore) -> None:
        """All fields of the snapshot are stored and retrieved correctly."""
        ts = _ts()
        record = SnapshotRecord(
            dataset_key="cdc_covid",
            url="https://cdc.gov/data.csv",
            fetched_at=ts,
            content_hash="sha256abc",
            file_path="/data/cdc_covid/2024-01-01.csv",
            http_status=200,
            byte_size=4096,
            is_duplicate=False,
        )
        snap_id = store.insert_snapshot(record)
        retrieved = store.get_snapshot(snap_id)

        assert retrieved is not None
        assert retrieved.id == snap_id
        assert retrieved.dataset_key == "cdc_covid"
        assert retrieved.url == "https://cdc.gov/data.csv"
        assert retrieved.fetched_at == ts
        assert retrieved.content_hash == "sha256abc"
        assert retrieved.file_path == "/data/cdc_covid/2024-01-01.csv"
        assert retrieved.http_status == 200
        assert retrieved.byte_size == 4096
        assert retrieved.is_duplicate is False

    def test_insert_different_keys_tracked_separately(self, store: SnapshotStore) -> None:
        """Snapshots for different dataset keys are tracked independently."""
        store.insert_snapshot(_snap(key="ds_a", content_hash="h1"))
        store.insert_snapshot(_snap(key="ds_b", content_hash="h2"))
        assert store.count_snapshots("ds_a") == 1
        assert store.count_snapshots("ds_b") == 1
        assert store.count_snapshots() == 2


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------


class TestDuplicateDetection:
    """Tests for the automatic duplicate-hash detection on insert."""

    def test_second_insert_with_same_hash_is_marked_duplicate(self, store: SnapshotStore) -> None:
        """When the same hash is inserted twice, the second is a duplicate."""
        store.insert_snapshot(_snap(content_hash="same_hash", fetched_at=_ts(0)))
        dup_id = store.insert_snapshot(_snap(content_hash="same_hash", fetched_at=_ts(1)))
        dup = store.get_snapshot(dup_id)
        assert dup is not None
        assert dup.is_duplicate is True

    def test_first_insert_is_not_duplicate(self, store: SnapshotStore) -> None:
        """The first snapshot for a key is never marked as duplicate."""
        snap_id = store.insert_snapshot(_snap(content_hash="unique_hash"))
        snap = store.get_snapshot(snap_id)
        assert snap is not None
        assert snap.is_duplicate is False

    def test_different_hash_is_not_duplicate(self, store: SnapshotStore) -> None:
        """A different hash than the latest is not marked as duplicate."""
        store.insert_snapshot(_snap(content_hash="hash_v1", fetched_at=_ts(0)))
        snap_id = store.insert_snapshot(_snap(content_hash="hash_v2", fetched_at=_ts(1)))
        snap = store.get_snapshot(snap_id)
        assert snap is not None
        assert snap.is_duplicate is False

    def test_auto_mark_duplicate_false_skips_check(self, store: SnapshotStore) -> None:
        """auto_mark_duplicate=False respects the record's own flag."""
        store.insert_snapshot(_snap(content_hash="same_hash", fetched_at=_ts(0)))
        snap_id = store.insert_snapshot(
            _snap(content_hash="same_hash", is_duplicate=False, fetched_at=_ts(1)),
            auto_mark_duplicate=False,
        )
        snap = store.get_snapshot(snap_id)
        assert snap is not None
        assert snap.is_duplicate is False

    def test_same_hash_different_keys_not_duplicate(self, store: SnapshotStore) -> None:
        """Two snapshots with the same hash but different keys are independent."""
        store.insert_snapshot(_snap(key="ds_a", content_hash="shared_hash", fetched_at=_ts(0)))
        snap_id = store.insert_snapshot(
            _snap(key="ds_b", content_hash="shared_hash", fetched_at=_ts(1))
        )
        snap = store.get_snapshot(snap_id)
        assert snap is not None
        assert snap.is_duplicate is False

    def test_snapshot_exists_for_hash_true(self, store: SnapshotStore) -> None:
        """snapshot_exists_for_hash returns True when hash is present."""
        store.insert_snapshot(_snap(key="ds_a", content_hash="existing_hash"))
        assert store.snapshot_exists_for_hash("ds_a", "existing_hash") is True

    def test_snapshot_exists_for_hash_false_wrong_key(self, store: SnapshotStore) -> None:
        """snapshot_exists_for_hash returns False for a different dataset key."""
        store.insert_snapshot(_snap(key="ds_a", content_hash="existing_hash"))
        assert store.snapshot_exists_for_hash("ds_b", "existing_hash") is False

    def test_snapshot_exists_for_hash_false_wrong_hash(self, store: SnapshotStore) -> None:
        """snapshot_exists_for_hash returns False for an unknown hash."""
        store.insert_snapshot(_snap(key="ds_a", content_hash="existing_hash"))
        assert store.snapshot_exists_for_hash("ds_a", "nonexistent_hash") is False


# ---------------------------------------------------------------------------
# Snapshot retrieval
# ---------------------------------------------------------------------------


class TestGetSnapshot:
    """Tests for get_snapshot and get_latest_snapshot."""

    def test_get_snapshot_returns_none_for_missing_id(self, store: SnapshotStore) -> None:
        """get_snapshot returns None for a non-existent ID."""
        assert store.get_snapshot(9999) is None

    def test_get_latest_snapshot_returns_none_when_empty(self, store: SnapshotStore) -> None:
        """get_latest_snapshot returns None when no snapshots exist."""
        assert store.get_latest_snapshot("nonexistent_key") is None

    def test_get_latest_snapshot_returns_most_recent(self, store: SnapshotStore) -> None:
        """get_latest_snapshot returns the snapshot with the most recent timestamp."""
        store.insert_snapshot(_snap(content_hash="h_old", fetched_at=_ts(-100)))
        store.insert_snapshot(_snap(content_hash="h_new", fetched_at=_ts(0)))
        latest = store.get_latest_snapshot("test_ds")
        assert latest is not None
        assert latest.content_hash == "h_new"

    def test_get_latest_snapshot_scoped_to_key(self, store: SnapshotStore) -> None:
        """get_latest_snapshot only considers the specified dataset key."""
        store.insert_snapshot(_snap(key="ds_a", content_hash="a_hash", fetched_at=_ts(10)))
        store.insert_snapshot(_snap(key="ds_b", content_hash="b_hash", fetched_at=_ts(0)))
        latest_a = store.get_latest_snapshot("ds_a")
        assert latest_a is not None
        assert latest_a.content_hash == "a_hash"


# ---------------------------------------------------------------------------
# get_snapshots_for_dataset
# ---------------------------------------------------------------------------


class TestGetSnapshotsForDataset:
    """Tests for the list-retrieval method."""

    def test_returns_empty_list_when_none_exist(self, store: SnapshotStore) -> None:
        """Returns an empty list for a dataset with no snapshots."""
        assert store.get_snapshots_for_dataset("missing_key") == []

    def test_returns_all_snapshots_for_key(self, store: SnapshotStore) -> None:
        """Returns all snapshots for the given key."""
        for i in range(3):
            store.insert_snapshot(_snap(content_hash=f"h{i}", fetched_at=_ts(i)))
        result = store.get_snapshots_for_dataset("test_ds")
        assert len(result) == 3

    def test_ordered_newest_first(self, store: SnapshotStore) -> None:
        """Results are ordered newest first."""
        store.insert_snapshot(_snap(content_hash="old", fetched_at=_ts(-50)))
        store.insert_snapshot(_snap(content_hash="new", fetched_at=_ts(0)))
        result = store.get_snapshots_for_dataset("test_ds")
        assert result[0].content_hash == "new"
        assert result[1].content_hash == "old"

    def test_limit_parameter(self, store: SnapshotStore) -> None:
        """The limit parameter caps the number of results."""
        for i in range(5):
            store.insert_snapshot(_snap(content_hash=f"h{i}", fetched_at=_ts(i)))
        result = store.get_snapshots_for_dataset("test_ds", limit=3)
        assert len(result) == 3

    def test_exclude_duplicates(self, store: SnapshotStore) -> None:
        """include_duplicates=False filters out duplicate snapshots."""
        store.insert_snapshot(_snap(content_hash="unique_h", fetched_at=_ts(0)))
        store.insert_snapshot(_snap(content_hash="unique_h", fetched_at=_ts(1)))  # dup
        store.insert_snapshot(_snap(content_hash="other_h", fetched_at=_ts(2)))
        result = store.get_snapshots_for_dataset("test_ds", include_duplicates=False)
        assert all(not s.is_duplicate for s in result)
        assert len(result) == 2

    def test_does_not_return_other_keys(self, store: SnapshotStore) -> None:
        """Snapshots for other dataset keys are not included."""
        store.insert_snapshot(_snap(key="ds_a", content_hash="h1"))
        store.insert_snapshot(_snap(key="ds_b", content_hash="h2"))
        result = store.get_snapshots_for_dataset("ds_a")
        assert all(s.dataset_key == "ds_a" for s in result)


# ---------------------------------------------------------------------------
# get_all_snapshots
# ---------------------------------------------------------------------------


class TestGetAllSnapshots:
    """Tests for get_all_snapshots."""

    def test_returns_empty_when_no_data(self, store: SnapshotStore) -> None:
        """Returns an empty list when the table is empty."""
        assert store.get_all_snapshots() == []

    def test_returns_all_records(self, store: SnapshotStore) -> None:
        """Returns snapshots across multiple datasets."""
        store.insert_snapshot(_snap(key="ds_a", content_hash="h1"))
        store.insert_snapshot(_snap(key="ds_b", content_hash="h2"))
        assert len(store.get_all_snapshots()) == 2

    def test_limit_parameter(self, store: SnapshotStore) -> None:
        """The limit parameter works for get_all_snapshots."""
        for i in range(6):
            store.insert_snapshot(_snap(key=f"ds_{i}", content_hash=f"h{i}"))
        result = store.get_all_snapshots(limit=4)
        assert len(result) == 4


# ---------------------------------------------------------------------------
# Snapshot DELETE
# ---------------------------------------------------------------------------


class TestDeleteSnapshot:
    """Tests for delete_snapshot."""

    def test_delete_existing_returns_true(self, store: SnapshotStore) -> None:
        """Deleting an existing snapshot returns True."""
        snap_id = store.insert_snapshot(_snap())
        assert store.delete_snapshot(snap_id) is True

    def test_delete_removes_from_db(self, store: SnapshotStore) -> None:
        """After deletion the snapshot can no longer be retrieved."""
        snap_id = store.insert_snapshot(_snap())
        store.delete_snapshot(snap_id)
        assert store.get_snapshot(snap_id) is None

    def test_delete_nonexistent_returns_false(self, store: SnapshotStore) -> None:
        """Deleting a non-existent ID returns False."""
        assert store.delete_snapshot(99999) is False

    def test_delete_snapshot_cascades_to_change_records(self, store: SnapshotStore) -> None:
        """Deleting a snapshot cascades to its change records."""
        snap_id = store.insert_snapshot(_snap())
        store.insert_change_record(_change(snapshot_id=snap_id))
        assert store.count_change_records() == 1
        store.delete_snapshot(snap_id)
        assert store.count_change_records() == 0


# ---------------------------------------------------------------------------
# Change record INSERT
# ---------------------------------------------------------------------------


class TestInsertChangeRecord:
    """Tests for insert_change_record."""

    def test_insert_returns_positive_id(self, store: SnapshotStore) -> None:
        """Inserting a change record returns a positive integer ID."""
        snap_id = store.insert_snapshot(_snap())
        change_id = store.insert_change_record(_change(snapshot_id=snap_id))
        assert isinstance(change_id, int)
        assert change_id >= 1

    def test_insert_persists_all_fields(self, store: SnapshotStore) -> None:
        """All fields of the change record are stored correctly."""
        snap_id = store.insert_snapshot(_snap())
        ts = _ts()
        diff_data = {"added_rows": 5, "removed_cols": ["status"]}
        record = ChangeRecord(
            snapshot_id=snap_id,
            dataset_key="test_ds",
            detected_at=ts,
            change_type="schema",
            severity="warning",
            row_delta=5,
            col_delta=-1,
            summary="Column 'status' removed",
            diff_json=json.dumps(diff_data),
        )
        change_id = store.insert_change_record(record)
        retrieved = store.get_change_record(change_id)

        assert retrieved is not None
        assert retrieved.id == change_id
        assert retrieved.snapshot_id == snap_id
        assert retrieved.dataset_key == "test_ds"
        assert retrieved.detected_at == ts
        assert retrieved.change_type == "schema"
        assert retrieved.severity == "warning"
        assert retrieved.row_delta == 5
        assert retrieved.col_delta == -1
        assert retrieved.summary == "Column 'status' removed"
        assert retrieved.diff_data() == diff_data

    def test_insert_with_invalid_snapshot_id_raises(self, store: SnapshotStore) -> None:
        """Inserting a change record with a non-existent snapshot_id raises."""
        record = _change(snapshot_id=9999)
        with pytest.raises((sqlite3.IntegrityError, sqlite3.DatabaseError)):
            store.insert_change_record(record)


# ---------------------------------------------------------------------------
# Change record retrieval
# ---------------------------------------------------------------------------


class TestGetChangeRecord:
    """Tests for change record retrieval methods."""

    def test_get_change_record_returns_none_for_missing_id(self, store: SnapshotStore) -> None:
        """get_change_record returns None for a non-existent ID."""
        assert store.get_change_record(9999) is None

    def test_get_change_records_for_dataset_empty(self, store: SnapshotStore) -> None:
        """Returns empty list when no change records exist for a key."""
        assert store.get_change_records_for_dataset("no_such_key") == []

    def test_get_change_records_for_dataset_returns_correct_records(self, store: SnapshotStore) -> None:
        """Returns change records only for the specified dataset key."""
        snap_a = store.insert_snapshot(_snap(key="ds_a", content_hash="h1"))
        snap_b = store.insert_snapshot(_snap(key="ds_b", content_hash="h2"))
        store.insert_change_record(_change(snapshot_id=snap_a, key="ds_a"))
        store.insert_change_record(_change(snapshot_id=snap_b, key="ds_b"))
        result = store.get_change_records_for_dataset("ds_a")
        assert len(result) == 1
        assert result[0].dataset_key == "ds_a"

    def test_get_change_records_ordered_newest_first(self, store: SnapshotStore) -> None:
        """Change records are returned newest first."""
        snap_id = store.insert_snapshot(_snap())
        store.insert_change_record(_change(snap_id, detected_at=_ts(-100), summary="old"))
        store.insert_change_record(_change(snap_id, detected_at=_ts(0), summary="new"))
        result = store.get_change_records_for_dataset("test_ds")
        assert result[0].summary == "new"
        assert result[1].summary == "old"

    def test_get_change_records_limit(self, store: SnapshotStore) -> None:
        """The limit parameter caps the number of change records returned."""
        snap_id = store.insert_snapshot(_snap())
        for i in range(5):
            store.insert_change_record(_change(snap_id, detected_at=_ts(i)))
        result = store.get_change_records_for_dataset("test_ds", limit=3)
        assert len(result) == 3

    def test_get_change_records_filter_by_severity(self, store: SnapshotStore) -> None:
        """severity filter returns only records with the specified severity."""
        snap_id = store.insert_snapshot(_snap())
        store.insert_change_record(_change(snap_id, severity="info"))
        store.insert_change_record(_change(snap_id, severity="warning"))
        store.insert_change_record(_change(snap_id, severity="critical"))
        result = store.get_change_records_for_dataset("test_ds", severity="warning")
        assert len(result) == 1
        assert result[0].severity == "warning"

    def test_get_all_change_records_returns_all(self, store: SnapshotStore) -> None:
        """get_all_change_records returns records across all dataset keys."""
        snap_a = store.insert_snapshot(_snap(key="ds_a", content_hash="h1"))
        snap_b = store.insert_snapshot(_snap(key="ds_b", content_hash="h2"))
        store.insert_change_record(_change(snap_a, key="ds_a"))
        store.insert_change_record(_change(snap_b, key="ds_b"))
        result = store.get_all_change_records()
        assert len(result) == 2

    def test_get_all_change_records_severity_filter(self, store: SnapshotStore) -> None:
        """get_all_change_records supports severity filtering."""
        snap_id = store.insert_snapshot(_snap())
        store.insert_change_record(_change(snap_id, severity="info"))
        store.insert_change_record(_change(snap_id, severity="critical"))
        result = store.get_all_change_records(severity="critical")
        assert len(result) == 1
        assert result[0].severity == "critical"

    def test_get_change_records_for_snapshot(self, store: SnapshotStore) -> None:
        """Returns all change records belonging to a specific snapshot."""
        snap_a = store.insert_snapshot(_snap(key="ds_a", content_hash="ha"))
        snap_b = store.insert_snapshot(_snap(key="ds_b", content_hash="hb"))
        store.insert_change_record(_change(snap_a, key="ds_a"))
        store.insert_change_record(_change(snap_a, key="ds_a"))
        store.insert_change_record(_change(snap_b, key="ds_b"))
        result = store.get_change_records_for_snapshot(snap_a)
        assert len(result) == 2
        assert all(r.snapshot_id == snap_a for r in result)


# ---------------------------------------------------------------------------
# Change record DELETE
# ---------------------------------------------------------------------------


class TestDeleteChangeRecord:
    """Tests for delete_change_record."""

    def test_delete_existing_returns_true(self, store: SnapshotStore) -> None:
        """Deleting an existing change record returns True."""
        snap_id = store.insert_snapshot(_snap())
        change_id = store.insert_change_record(_change(snap_id))
        assert store.delete_change_record(change_id) is True

    def test_delete_removes_from_db(self, store: SnapshotStore) -> None:
        """After deletion the change record can no longer be retrieved."""
        snap_id = store.insert_snapshot(_snap())
        change_id = store.insert_change_record(_change(snap_id))
        store.delete_change_record(change_id)
        assert store.get_change_record(change_id) is None

    def test_delete_nonexistent_returns_false(self, store: SnapshotStore) -> None:
        """Deleting a non-existent change record ID returns False."""
        assert store.delete_change_record(99999) is False


# ---------------------------------------------------------------------------
# Aggregate / utility
# ---------------------------------------------------------------------------


class TestAggregateQueries:
    """Tests for count and dataset key listing helpers."""

    def test_count_snapshots_empty(self, store: SnapshotStore) -> None:
        """count_snapshots returns 0 when table is empty."""
        assert store.count_snapshots() == 0

    def test_count_snapshots_all(self, store: SnapshotStore) -> None:
        """count_snapshots returns correct total across all datasets."""
        store.insert_snapshot(_snap(key="a", content_hash="h1"))
        store.insert_snapshot(_snap(key="b", content_hash="h2"))
        assert store.count_snapshots() == 2

    def test_count_snapshots_per_key(self, store: SnapshotStore) -> None:
        """count_snapshots scoped to a key returns the correct count."""
        store.insert_snapshot(_snap(key="a", content_hash="h1"))
        store.insert_snapshot(_snap(key="a", content_hash="h2"))
        store.insert_snapshot(_snap(key="b", content_hash="h3"))
        assert store.count_snapshots("a") == 2
        assert store.count_snapshots("b") == 1

    def test_count_change_records_empty(self, store: SnapshotStore) -> None:
        """count_change_records returns 0 when table is empty."""
        assert store.count_change_records() == 0

    def test_count_change_records_total(self, store: SnapshotStore) -> None:
        """count_change_records returns correct total."""
        snap_id = store.insert_snapshot(_snap())
        store.insert_change_record(_change(snap_id))
        store.insert_change_record(_change(snap_id))
        assert store.count_change_records() == 2

    def test_count_change_records_per_key(self, store: SnapshotStore) -> None:
        """count_change_records scoped to a key returns the correct count."""
        snap_a = store.insert_snapshot(_snap(key="ds_a", content_hash="ha"))
        snap_b = store.insert_snapshot(_snap(key="ds_b", content_hash="hb"))
        store.insert_change_record(_change(snap_a, key="ds_a"))
        store.insert_change_record(_change(snap_b, key="ds_b"))
        assert store.count_change_records("ds_a") == 1

    def test_get_dataset_keys_empty(self, store: SnapshotStore) -> None:
        """get_dataset_keys returns an empty list when no snapshots exist."""
        assert store.get_dataset_keys() == []

    def test_get_dataset_keys_sorted(self, store: SnapshotStore) -> None:
        """get_dataset_keys returns sorted unique dataset keys."""
        store.insert_snapshot(_snap(key="zzz", content_hash="h1"))
        store.insert_snapshot(_snap(key="aaa", content_hash="h2"))
        store.insert_snapshot(_snap(key="zzz", content_hash="h3"))  # duplicate key
        keys = store.get_dataset_keys()
        assert keys == ["aaa", "zzz"]


# ---------------------------------------------------------------------------
# Prune old snapshots
# ---------------------------------------------------------------------------


class TestPruneOldSnapshots:
    """Tests for the prune_old_snapshots method."""

    def test_prune_keeps_n_most_recent(self, store: SnapshotStore) -> None:
        """prune_old_snapshots keeps only the N most recent snapshots."""
        for i in range(5):
            store.insert_snapshot(_snap(content_hash=f"h{i}", fetched_at=_ts(i)))
        deleted = store.prune_old_snapshots("test_ds", keep=3)
        assert deleted == 2
        assert store.count_snapshots("test_ds") == 3

    def test_prune_when_fewer_than_keep(self, store: SnapshotStore) -> None:
        """No rows are deleted when the count is <= keep."""
        for i in range(3):
            store.insert_snapshot(_snap(content_hash=f"h{i}", fetched_at=_ts(i)))
        deleted = store.prune_old_snapshots("test_ds", keep=10)
        assert deleted == 0
        assert store.count_snapshots("test_ds") == 3

    def test_prune_removes_oldest(self, store: SnapshotStore) -> None:
        """The oldest snapshots (by fetched_at) are the ones removed."""
        for i in range(4):
            store.insert_snapshot(_snap(content_hash=f"h{i}", fetched_at=_ts(i)))
        store.prune_old_snapshots("test_ds", keep=2)
        remaining = store.get_snapshots_for_dataset("test_ds")
        hashes = {s.content_hash for s in remaining}
        assert "h3" in hashes  # newest
        assert "h2" in hashes
        assert "h0" not in hashes  # oldest removed
        assert "h1" not in hashes

    def test_prune_does_not_affect_other_keys(self, store: SnapshotStore) -> None:
        """Pruning one dataset key does not delete snapshots for other keys."""
        for i in range(5):
            store.insert_snapshot(_snap(key="ds_a", content_hash=f"a{i}", fetched_at=_ts(i)))
        store.insert_snapshot(_snap(key="ds_b", content_hash="b0"))
        store.prune_old_snapshots("ds_a", keep=2)
        assert store.count_snapshots("ds_b") == 1

    def test_prune_keep_zero_raises(self, store: SnapshotStore) -> None:
        """prune_old_snapshots raises ValueError if keep < 1."""
        store.insert_snapshot(_snap())
        with pytest.raises(ValueError, match="keep"):
            store.prune_old_snapshots("test_ds", keep=0)

    def test_prune_cascades_change_records(self, store: SnapshotStore) -> None:
        """Pruning snapshots also removes their associated change records."""
        ids = []
        for i in range(3):
            sid = store.insert_snapshot(
                _snap(content_hash=f"h{i}", fetched_at=_ts(i))
            )
            ids.append(sid)
            store.insert_change_record(_change(sid))
        assert store.count_change_records() == 3
        store.prune_old_snapshots("test_ds", keep=1)
        # Only the newest snapshot and its change record should remain.
        assert store.count_snapshots("test_ds") == 1
        assert store.count_change_records() == 1


# ---------------------------------------------------------------------------
# ChangeRecord.diff_data helper
# ---------------------------------------------------------------------------


class TestChangeRecordDiffData:
    """Tests for the ChangeRecord.diff_data helper."""

    def test_diff_data_parses_valid_json(self, store: SnapshotStore) -> None:
        """diff_data deserialises the stored JSON string."""
        snap_id = store.insert_snapshot(_snap())
        payload = {"removed": ["col_a"], "added_rows": 10}
        cr = _change(snap_id, diff_json=json.dumps(payload))
        change_id = store.insert_change_record(cr)
        retrieved = store.get_change_record(change_id)
        assert retrieved is not None
        assert retrieved.diff_data() == payload

    def test_diff_data_returns_empty_dict_on_invalid_json(self) -> None:
        """diff_data returns {} when diff_json is malformed."""
        cr = ChangeRecord(
            snapshot_id=1,
            dataset_key="ds",
            detected_at=_ts(),
            change_type="content",
            diff_json="not valid json {{{",
        )
        assert cr.diff_data() == {}


# ---------------------------------------------------------------------------
# to_dict helpers
# ---------------------------------------------------------------------------


class TestToDictHelpers:
    """Tests for SnapshotRecord.to_dict and ChangeRecord.to_dict."""

    def test_snapshot_to_dict_has_all_keys(self, store: SnapshotStore) -> None:
        """SnapshotRecord.to_dict contains all expected keys."""
        snap_id = store.insert_snapshot(_snap())
        retrieved = store.get_snapshot(snap_id)
        assert retrieved is not None
        d = retrieved.to_dict()
        for key in (
            "id", "dataset_key", "url", "fetched_at", "content_hash",
            "file_path", "http_status", "byte_size", "is_duplicate",
        ):
            assert key in d, f"Missing key: {key}"

    def test_change_record_to_dict_has_all_keys(self, store: SnapshotStore) -> None:
        """ChangeRecord.to_dict contains all expected keys."""
        snap_id = store.insert_snapshot(_snap())
        change_id = store.insert_change_record(_change(snap_id))
        retrieved = store.get_change_record(change_id)
        assert retrieved is not None
        d = retrieved.to_dict()
        for key in (
            "id", "snapshot_id", "dataset_key", "detected_at", "change_type",
            "severity", "row_delta", "col_delta", "summary", "diff_json",
        ):
            assert key in d, f"Missing key: {key}"

    def test_snapshot_to_dict_id_is_set(self, store: SnapshotStore) -> None:
        """to_dict on a retrieved snapshot has a non-None id."""
        snap_id = store.insert_snapshot(_snap())
        retrieved = store.get_snapshot(snap_id)
        assert retrieved is not None
        assert retrieved.to_dict()["id"] == snap_id
