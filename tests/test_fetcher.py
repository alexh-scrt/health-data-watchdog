"""Tests for the health_data_watchdog.fetcher module.

Uses mocked HTTP responses (via the ``responses`` library) to verify
hashing, snapshot storage, duplicate detection, error handling, and
integration with the SQLite store.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest
import responses as responses_lib
from responses import RequestsMock

from health_data_watchdog.datasets import DatasetEntry
from health_data_watchdog.fetcher import (
    DEFAULT_CHUNK_SIZE,
    DEFAULT_TIMEOUT,
    DatasetFetcher,
    FetchResult,
    compute_sha256,
    compute_sha256_file,
    fetch_dataset,
)
from health_data_watchdog.store import SnapshotStore


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


TEST_URL = "https://example.com/data.csv"
TEST_CONTENT = b"col_a,col_b\n1,2\n3,4\n"
TEST_HASH = hashlib.sha256(TEST_CONTENT).hexdigest()


def _entry(
    key: str = "test_ds",
    url: str = TEST_URL,
    fmt: str = "csv",
    source: str = "Test",
    description: str = "A test dataset",
    enabled: bool = True,
    extra_headers: Optional[dict] = None,
) -> DatasetEntry:
    """Build a DatasetEntry for testing."""
    return DatasetEntry(
        key=key,
        url=url,
        source=source,
        format=fmt,
        description=description,
        enabled=enabled,
        extra_headers=extra_headers or {},
    )


@pytest.fixture()
def store(tmp_path: Path) -> SnapshotStore:  # type: ignore[misc]
    """Yield a fresh SnapshotStore backed by a temp SQLite file."""
    s = SnapshotStore(tmp_path / "test.db")
    yield s
    s.close()


@pytest.fixture()
def fetcher(tmp_path: Path, store: SnapshotStore) -> DatasetFetcher:  # type: ignore[misc]
    """Yield a DatasetFetcher with a temp data dir and in-memory-ish store."""
    f = DatasetFetcher(data_dir=tmp_path / "snapshots", store=store)
    yield f
    f.close()


@pytest.fixture()
def fetcher_no_store(tmp_path: Path) -> DatasetFetcher:  # type: ignore[misc]
    """Yield a DatasetFetcher without a store."""
    f = DatasetFetcher(data_dir=tmp_path / "snapshots", store=None)
    yield f
    f.close()


# ---------------------------------------------------------------------------
# compute_sha256
# ---------------------------------------------------------------------------


class TestComputeSha256:
    """Tests for the compute_sha256 helper."""

    def test_known_hash(self) -> None:
        """SHA-256 of empty bytes matches the known digest."""
        expected = hashlib.sha256(b"").hexdigest()
        assert compute_sha256(b"") == expected

    def test_non_empty_bytes(self) -> None:
        """SHA-256 of known content matches independently computed value."""
        assert compute_sha256(TEST_CONTENT) == TEST_HASH

    def test_returns_lowercase_hex(self) -> None:
        """The digest is returned as a lowercase hex string."""
        digest = compute_sha256(b"hello")
        assert digest == digest.lower()
        assert len(digest) == 64


# ---------------------------------------------------------------------------
# compute_sha256_file
# ---------------------------------------------------------------------------


class TestComputeSha256File:
    """Tests for the compute_sha256_file helper."""

    def test_matches_in_memory_hash(self, tmp_path: Path) -> None:
        """File hash matches the in-memory hash of the same content."""
        f = tmp_path / "test.bin"
        f.write_bytes(TEST_CONTENT)
        assert compute_sha256_file(f) == compute_sha256(TEST_CONTENT)

    def test_empty_file(self, tmp_path: Path) -> None:
        """Hash of an empty file matches the known SHA-256 of empty bytes."""
        f = tmp_path / "empty.bin"
        f.write_bytes(b"")
        assert compute_sha256_file(f) == hashlib.sha256(b"").hexdigest()

    def test_large_file_chunked(self, tmp_path: Path) -> None:
        """Files larger than the chunk size are hashed correctly."""
        data = b"x" * (DEFAULT_CHUNK_SIZE * 3 + 100)
        f = tmp_path / "large.bin"
        f.write_bytes(data)
        assert compute_sha256_file(f, chunk_size=DEFAULT_CHUNK_SIZE) == compute_sha256(data)

    def test_raises_on_missing_file(self, tmp_path: Path) -> None:
        """OSError is raised if the file does not exist."""
        with pytest.raises(OSError):
            compute_sha256_file(tmp_path / "nonexistent.bin")


# ---------------------------------------------------------------------------
# DatasetFetcher instantiation
# ---------------------------------------------------------------------------


class TestDatasetFetcherInit:
    """Tests for DatasetFetcher initialisation."""

    def test_creates_data_dir(self, tmp_path: Path) -> None:
        """DatasetFetcher creates the data directory if it does not exist."""
        target = tmp_path / "a" / "b" / "snapshots"
        assert not target.exists()
        f = DatasetFetcher(data_dir=target)
        f.close()
        assert target.exists()

    def test_data_dir_property(self, tmp_path: Path) -> None:
        """data_dir property returns the configured directory."""
        f = DatasetFetcher(data_dir=tmp_path)
        assert f.data_dir == tmp_path
        f.close()

    def test_context_manager(self, tmp_path: Path) -> None:
        """DatasetFetcher can be used as a context manager."""
        with DatasetFetcher(data_dir=tmp_path) as f:
            assert isinstance(f, DatasetFetcher)

    def test_custom_session_is_accepted(self, tmp_path: Path) -> None:
        """A pre-built session can be injected."""
        import requests as req
        session = req.Session()
        f = DatasetFetcher(data_dir=tmp_path, session=session)
        f.close()
        # Should not close the session since it was provided externally
        # (just test it doesn't raise)


# ---------------------------------------------------------------------------
# Successful fetch
# ---------------------------------------------------------------------------


class TestFetchDatasetSuccess:
    """Tests for a successful HTTP fetch."""

    @responses_lib.activate
    def test_returns_success_result(self, fetcher: DatasetFetcher) -> None:
        """A 200 response produces a FetchResult with success=True."""
        responses_lib.add(
            responses_lib.GET, TEST_URL, body=TEST_CONTENT, status=200
        )
        result = fetcher.fetch_dataset(_entry())
        assert result.success is True

    @responses_lib.activate
    def test_content_hash_is_correct(self, fetcher: DatasetFetcher) -> None:
        """The content hash in the result matches SHA-256 of the body."""
        responses_lib.add(
            responses_lib.GET, TEST_URL, body=TEST_CONTENT, status=200
        )
        result = fetcher.fetch_dataset(_entry())
        assert result.content_hash == TEST_HASH

    @responses_lib.activate
    def test_byte_size_is_correct(self, fetcher: DatasetFetcher) -> None:
        """byte_size in the result equals the length of the response body."""
        responses_lib.add(
            responses_lib.GET, TEST_URL, body=TEST_CONTENT, status=200
        )
        result = fetcher.fetch_dataset(_entry())
        assert result.byte_size == len(TEST_CONTENT)

    @responses_lib.activate
    def test_http_status_is_200(self, fetcher: DatasetFetcher) -> None:
        """http_status is 200 on a successful response."""
        responses_lib.add(
            responses_lib.GET, TEST_URL, body=TEST_CONTENT, status=200
        )
        result = fetcher.fetch_dataset(_entry())
        assert result.http_status == 200

    @responses_lib.activate
    def test_dataset_key_in_result(self, fetcher: DatasetFetcher) -> None:
        """The dataset_key attribute in the result matches the entry."""
        responses_lib.add(
            responses_lib.GET, TEST_URL, body=TEST_CONTENT, status=200
        )
        result = fetcher.fetch_dataset(_entry(key="my_dataset"))
        assert result.dataset_key == "my_dataset"

    @responses_lib.activate
    def test_error_is_none_on_success(self, fetcher: DatasetFetcher) -> None:
        """error attribute is None when the fetch succeeds."""
        responses_lib.add(
            responses_lib.GET, TEST_URL, body=TEST_CONTENT, status=200
        )
        result = fetcher.fetch_dataset(_entry())
        assert result.error is None

    @responses_lib.activate
    def test_fetched_at_is_iso_string(self, fetcher: DatasetFetcher) -> None:
        """fetched_at is a non-empty ISO-8601 string."""
        responses_lib.add(
            responses_lib.GET, TEST_URL, body=TEST_CONTENT, status=200
        )
        result = fetcher.fetch_dataset(_entry())
        assert isinstance(result.fetched_at, str)
        assert len(result.fetched_at) > 0
        # Should parse as an ISO datetime
        from datetime import datetime
        datetime.fromisoformat(result.fetched_at)


# ---------------------------------------------------------------------------
# File persistence
# ---------------------------------------------------------------------------


class TestSnapshotFilePersistence:
    """Tests that snapshot files are written to the correct location."""

    @responses_lib.activate
    def test_snapshot_file_is_created(self, fetcher: DatasetFetcher) -> None:
        """A snapshot file is created on disk after a successful fetch."""
        responses_lib.add(
            responses_lib.GET, TEST_URL, body=TEST_CONTENT, status=200
        )
        result = fetcher.fetch_dataset(_entry())
        assert result.file_path is not None
        assert result.file_path.exists()

    @responses_lib.activate
    def test_snapshot_file_content_matches_response(self, fetcher: DatasetFetcher) -> None:
        """The snapshot file contains exactly the bytes from the HTTP response."""
        responses_lib.add(
            responses_lib.GET, TEST_URL, body=TEST_CONTENT, status=200
        )
        result = fetcher.fetch_dataset(_entry())
        assert result.file_path is not None
        assert result.file_path.read_bytes() == TEST_CONTENT

    @responses_lib.activate
    def test_snapshot_file_in_dataset_subdirectory(self, fetcher: DatasetFetcher) -> None:
        """Snapshot files are placed in a per-dataset subdirectory."""
        responses_lib.add(
            responses_lib.GET, TEST_URL, body=TEST_CONTENT, status=200
        )
        result = fetcher.fetch_dataset(_entry(key="my_ds"))
        assert result.file_path is not None
        assert result.file_path.parent.name == "my_ds"

    @responses_lib.activate
    def test_snapshot_filename_uses_correct_extension(self, fetcher: DatasetFetcher) -> None:
        """The snapshot filename ends with the correct format extension."""
        responses_lib.add(
            responses_lib.GET, TEST_URL, body=TEST_CONTENT, status=200
        )
        result = fetcher.fetch_dataset(_entry(fmt="csv"))
        assert result.file_path is not None
        assert result.file_path.suffix == ".csv"

    @responses_lib.activate
    def test_json_format_extension(self, fetcher: DatasetFetcher) -> None:
        """JSON datasets have a .json file extension."""
        json_url = "https://example.com/data.json"
        responses_lib.add(
            responses_lib.GET,
            json_url,
            body=b'{"key": "value"}',
            status=200,
        )
        result = fetcher.fetch_dataset(_entry(url=json_url, fmt="json"))
        assert result.file_path is not None
        assert result.file_path.suffix == ".json"

    @responses_lib.activate
    def test_no_temp_files_left_behind(self, fetcher: DatasetFetcher) -> None:
        """No .tmp files remain in the data directory after a successful fetch."""
        responses_lib.add(
            responses_lib.GET, TEST_URL, body=TEST_CONTENT, status=200
        )
        fetcher.fetch_dataset(_entry())
        tmp_files = list(fetcher.data_dir.rglob("*.tmp"))
        assert tmp_files == []

    @responses_lib.activate
    def test_file_hash_matches_result_hash(self, fetcher: DatasetFetcher) -> None:
        """The SHA-256 hash of the written file equals result.content_hash."""
        responses_lib.add(
            responses_lib.GET, TEST_URL, body=TEST_CONTENT, status=200
        )
        result = fetcher.fetch_dataset(_entry())
        assert result.file_path is not None
        file_hash = compute_sha256_file(result.file_path)
        assert file_hash == result.content_hash


# ---------------------------------------------------------------------------
# Store integration
# ---------------------------------------------------------------------------


class TestStoreIntegration:
    """Tests that snapshot records are correctly persisted in the store."""

    @responses_lib.activate
    def test_snapshot_id_is_set_in_result(self, fetcher: DatasetFetcher) -> None:
        """snapshot_id is a positive integer when a store is attached."""
        responses_lib.add(
            responses_lib.GET, TEST_URL, body=TEST_CONTENT, status=200
        )
        result = fetcher.fetch_dataset(_entry())
        assert result.snapshot_id is not None
        assert result.snapshot_id >= 1

    @responses_lib.activate
    def test_snapshot_persisted_in_store(
        self, fetcher: DatasetFetcher, store: SnapshotStore
    ) -> None:
        """A SnapshotRecord is written to the store after a successful fetch."""
        responses_lib.add(
            responses_lib.GET, TEST_URL, body=TEST_CONTENT, status=200
        )
        result = fetcher.fetch_dataset(_entry())
        assert store.count_snapshots() == 1
        snap = store.get_snapshot(result.snapshot_id)
        assert snap is not None
        assert snap.content_hash == TEST_HASH
        assert snap.dataset_key == "test_ds"

    @responses_lib.activate
    def test_snapshot_id_none_without_store(
        self, fetcher_no_store: DatasetFetcher
    ) -> None:
        """snapshot_id is None when no store is attached."""
        responses_lib.add(
            responses_lib.GET, TEST_URL, body=TEST_CONTENT, status=200
        )
        result = fetcher_no_store.fetch_dataset(_entry())
        assert result.snapshot_id is None

    @responses_lib.activate
    def test_store_records_url(
        self, fetcher: DatasetFetcher, store: SnapshotStore
    ) -> None:
        """The URL is stored correctly in the snapshot record."""
        responses_lib.add(
            responses_lib.GET, TEST_URL, body=TEST_CONTENT, status=200
        )
        result = fetcher.fetch_dataset(_entry(url=TEST_URL))
        snap = store.get_snapshot(result.snapshot_id)
        assert snap is not None
        assert snap.url == TEST_URL

    @responses_lib.activate
    def test_store_records_byte_size(
        self, fetcher: DatasetFetcher, store: SnapshotStore
    ) -> None:
        """The byte size is stored correctly in the snapshot record."""
        responses_lib.add(
            responses_lib.GET, TEST_URL, body=TEST_CONTENT, status=200
        )
        result = fetcher.fetch_dataset(_entry())
        snap = store.get_snapshot(result.snapshot_id)
        assert snap is not None
        assert snap.byte_size == len(TEST_CONTENT)

    @responses_lib.activate
    def test_store_records_file_path(
        self, fetcher: DatasetFetcher, store: SnapshotStore
    ) -> None:
        """The file path is stored correctly in the snapshot record."""
        responses_lib.add(
            responses_lib.GET, TEST_URL, body=TEST_CONTENT, status=200
        )
        result = fetcher.fetch_dataset(_entry())
        snap = store.get_snapshot(result.snapshot_id)
        assert snap is not None
        assert snap.file_path == str(result.file_path.resolve())


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------


class TestDuplicateDetection:
    """Tests for is_duplicate detection between consecutive fetches."""

    @responses_lib.activate
    def test_first_fetch_not_duplicate(
        self, fetcher: DatasetFetcher
    ) -> None:
        """The very first fetch for a dataset is never a duplicate."""
        responses_lib.add(
            responses_lib.GET, TEST_URL, body=TEST_CONTENT, status=200
        )
        result = fetcher.fetch_dataset(_entry())
        assert result.is_duplicate is False

    @responses_lib.activate
    def test_second_fetch_same_content_is_duplicate(
        self, fetcher: DatasetFetcher
    ) -> None:
        """A second fetch with identical content is marked as duplicate."""
        responses_lib.add(
            responses_lib.GET, TEST_URL, body=TEST_CONTENT, status=200
        )
        responses_lib.add(
            responses_lib.GET, TEST_URL, body=TEST_CONTENT, status=200
        )
        fetcher.fetch_dataset(_entry())
        result2 = fetcher.fetch_dataset(_entry())
        assert result2.is_duplicate is True

    @responses_lib.activate
    def test_second_fetch_different_content_not_duplicate(
        self, fetcher: DatasetFetcher
    ) -> None:
        """A second fetch with different content is not a duplicate."""
        new_content = b"col_a,col_b\n5,6\n7,8\n"
        responses_lib.add(
            responses_lib.GET, TEST_URL, body=TEST_CONTENT, status=200
        )
        responses_lib.add(
            responses_lib.GET, TEST_URL, body=new_content, status=200
        )
        fetcher.fetch_dataset(_entry())
        result2 = fetcher.fetch_dataset(_entry())
        assert result2.is_duplicate is False

    @responses_lib.activate
    def test_duplicate_still_written_to_disk(
        self, fetcher: DatasetFetcher
    ) -> None:
        """Even a duplicate fetch results in a file on disk."""
        responses_lib.add(
            responses_lib.GET, TEST_URL, body=TEST_CONTENT, status=200
        )
        responses_lib.add(
            responses_lib.GET, TEST_URL, body=TEST_CONTENT, status=200
        )
        fetcher.fetch_dataset(_entry())
        result2 = fetcher.fetch_dataset(_entry())
        assert result2.file_path is not None
        assert result2.file_path.exists()

    @responses_lib.activate
    def test_duplicate_still_stored_in_db(
        self, fetcher: DatasetFetcher, store: SnapshotStore
    ) -> None:
        """A duplicate fetch is still recorded in the store."""
        responses_lib.add(
            responses_lib.GET, TEST_URL, body=TEST_CONTENT, status=200
        )
        responses_lib.add(
            responses_lib.GET, TEST_URL, body=TEST_CONTENT, status=200
        )
        fetcher.fetch_dataset(_entry())
        result2 = fetcher.fetch_dataset(_entry())
        assert store.count_snapshots() == 2
        snap2 = store.get_snapshot(result2.snapshot_id)
        assert snap2 is not None
        assert snap2.is_duplicate is True

    @responses_lib.activate
    def test_no_store_no_duplicate_check(
        self, fetcher_no_store: DatasetFetcher
    ) -> None:
        """Without a store, is_duplicate is always False."""
        responses_lib.add(
            responses_lib.GET, TEST_URL, body=TEST_CONTENT, status=200
        )
        responses_lib.add(
            responses_lib.GET, TEST_URL, body=TEST_CONTENT, status=200
        )
        fetcher_no_store.fetch_dataset(_entry())
        result2 = fetcher_no_store.fetch_dataset(_entry())
        # Without a store there's no way to detect duplicates
        assert result2.is_duplicate is False


# ---------------------------------------------------------------------------
# Extra headers
# ---------------------------------------------------------------------------


class TestExtraHeaders:
    """Tests that extra_headers from the DatasetEntry are forwarded."""

    @responses_lib.activate
    def test_extra_headers_sent(self, fetcher: DatasetFetcher) -> None:
        """Extra headers defined in the DatasetEntry are sent with the request."""
        responses_lib.add(
            responses_lib.GET, TEST_URL, body=TEST_CONTENT, status=200
        )
        entry = _entry(extra_headers={"Accept": "application/json"})
        fetcher.fetch_dataset(entry)

        assert len(responses_lib.calls) == 1
        sent_headers = responses_lib.calls[0].request.headers
        assert sent_headers.get("Accept") == "application/json"


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestFetchErrors:
    """Tests for various HTTP and network error scenarios."""

    @responses_lib.activate
    def test_404_returns_failure_result(self, fetcher: DatasetFetcher) -> None:
        """A 404 response produces a FetchResult with success=False."""
        responses_lib.add(
            responses_lib.GET, TEST_URL, status=404, body=b"Not Found"
        )
        result = fetcher.fetch_dataset(_entry())
        assert result.success is False
        assert result.http_status == 404
        assert result.error is not None

    @responses_lib.activate
    def test_500_returns_failure_result(self, fetcher: DatasetFetcher) -> None:
        """A 500 response produces a FetchResult with success=False."""
        responses_lib.add(
            responses_lib.GET, TEST_URL, status=500, body=b"Internal Server Error"
        )
        result = fetcher.fetch_dataset(_entry())
        assert result.success is False
        assert result.error is not None

    @responses_lib.activate
    def test_connection_error_returns_failure(self, fetcher: DatasetFetcher) -> None:
        """A ConnectionError produces a FetchResult with success=False."""
        from requests.exceptions import ConnectionError as ReqConnError
        responses_lib.add(
            responses_lib.GET, TEST_URL, body=ReqConnError("connection refused")
        )
        result = fetcher.fetch_dataset(_entry())
        assert result.success is False
        assert result.http_status == 0
        assert result.error is not None

    @responses_lib.activate
    def test_timeout_returns_failure(self, fetcher: DatasetFetcher) -> None:
        """A Timeout exception produces a FetchResult with success=False."""
        from requests.exceptions import Timeout as ReqTimeout
        responses_lib.add(
            responses_lib.GET, TEST_URL, body=ReqTimeout("timed out")
        )
        result = fetcher.fetch_dataset(_entry())
        assert result.success is False
        assert result.error is not None

    @responses_lib.activate
    def test_failure_does_not_write_to_store(
        self, fetcher: DatasetFetcher, store: SnapshotStore
    ) -> None:
        """A failed fetch does not create a store record."""
        responses_lib.add(
            responses_lib.GET, TEST_URL, status=503, body=b"Service Unavailable"
        )
        fetcher.fetch_dataset(_entry())
        assert store.count_snapshots() == 0

    @responses_lib.activate
    def test_failure_does_not_leave_tmp_files(
        self, fetcher: DatasetFetcher
    ) -> None:
        """Temp files are cleaned up even on a streaming I/O error."""
        responses_lib.add(
            responses_lib.GET, TEST_URL, status=404, body=b"Not Found"
        )
        fetcher.fetch_dataset(_entry())
        tmp_files = list(fetcher.data_dir.rglob("*.tmp"))
        assert tmp_files == []

    @responses_lib.activate
    def test_failure_result_has_dataset_key(
        self, fetcher: DatasetFetcher
    ) -> None:
        """The dataset_key is always set in the result, even on failure."""
        responses_lib.add(
            responses_lib.GET, TEST_URL, status=500, body=b"err"
        )
        result = fetcher.fetch_dataset(_entry(key="failing_ds"))
        assert result.dataset_key == "failing_ds"


# ---------------------------------------------------------------------------
# fetch_all
# ---------------------------------------------------------------------------


class TestFetchAll:
    """Tests for the fetch_all method."""

    @responses_lib.activate
    def test_fetches_all_enabled_datasets(
        self, fetcher: DatasetFetcher
    ) -> None:
        """fetch_all returns a result for each enabled dataset."""
        url_a = "https://example.com/a.csv"
        url_b = "https://example.com/b.csv"
        responses_lib.add(responses_lib.GET, url_a, body=b"a,b\n1,2", status=200)
        responses_lib.add(responses_lib.GET, url_b, body=b"c,d\n3,4", status=200)

        datasets = [
            _entry(key="ds_a", url=url_a),
            _entry(key="ds_b", url=url_b),
        ]
        results = fetcher.fetch_all(datasets)
        assert len(results) == 2
        assert all(r.success for r in results)

    @responses_lib.activate
    def test_skip_disabled_true_skips_disabled(
        self, fetcher: DatasetFetcher
    ) -> None:
        """Disabled datasets are skipped when skip_disabled=True (default)."""
        url_a = "https://example.com/a.csv"
        responses_lib.add(responses_lib.GET, url_a, body=b"data", status=200)

        datasets = [
            _entry(key="ds_a", url=url_a, enabled=True),
            _entry(key="ds_b", url="https://example.com/b.csv", enabled=False),
        ]
        results = fetcher.fetch_all(datasets, skip_disabled=True)
        assert len(results) == 1
        assert results[0].dataset_key == "ds_a"

    @responses_lib.activate
    def test_skip_disabled_false_includes_disabled(
        self, fetcher: DatasetFetcher
    ) -> None:
        """Disabled datasets are included when skip_disabled=False."""
        url_a = "https://example.com/a.csv"
        url_b = "https://example.com/b.csv"
        responses_lib.add(responses_lib.GET, url_a, body=b"data_a", status=200)
        responses_lib.add(responses_lib.GET, url_b, body=b"data_b", status=200)

        datasets = [
            _entry(key="ds_a", url=url_a, enabled=True),
            _entry(key="ds_b", url=url_b, enabled=False),
        ]
        results = fetcher.fetch_all(datasets, skip_disabled=False)
        assert len(results) == 2

    def test_empty_list_returns_empty(self, fetcher: DatasetFetcher) -> None:
        """fetch_all with an empty list returns an empty list."""
        assert fetcher.fetch_all([]) == []

    @responses_lib.activate
    def test_partial_failure_continues(
        self, fetcher: DatasetFetcher
    ) -> None:
        """fetch_all continues fetching even if one dataset fails."""
        url_a = "https://example.com/a.csv"
        url_b = "https://example.com/b.csv"
        responses_lib.add(responses_lib.GET, url_a, status=500, body=b"err")
        responses_lib.add(responses_lib.GET, url_b, body=b"c,d\n3,4", status=200)

        datasets = [
            _entry(key="ds_a", url=url_a),
            _entry(key="ds_b", url=url_b),
        ]
        results = fetcher.fetch_all(datasets)
        assert len(results) == 2
        success_map = {r.dataset_key: r.success for r in results}
        assert success_map["ds_a"] is False
        assert success_map["ds_b"] is True


# ---------------------------------------------------------------------------
# fetch_by_key
# ---------------------------------------------------------------------------


class TestFetchByKey:
    """Tests for the fetch_by_key method."""

    @responses_lib.activate
    def test_fetches_correct_dataset(
        self, fetcher: DatasetFetcher
    ) -> None:
        """fetch_by_key fetches the dataset matching the given key."""
        url_a = "https://example.com/a.csv"
        responses_lib.add(responses_lib.GET, url_a, body=b"data", status=200)

        datasets = [
            _entry(key="ds_a", url=url_a),
            _entry(key="ds_b", url="https://example.com/b.csv"),
        ]
        result = fetcher.fetch_by_key("ds_a", datasets)
        assert result is not None
        assert result.dataset_key == "ds_a"
        assert result.success is True

    def test_returns_none_for_unknown_key(
        self, fetcher: DatasetFetcher
    ) -> None:
        """fetch_by_key returns None for a key not in the provided list."""
        result = fetcher.fetch_by_key("nonexistent_key", [])
        assert result is None

    @responses_lib.activate
    def test_uses_builtin_registry_when_no_list(
        self, fetcher: DatasetFetcher
    ) -> None:
        """When no datasets list is passed, the built-in registry is used."""
        from health_data_watchdog.datasets import get_builtin_datasets
        builtin = get_builtin_datasets()
        if not builtin:
            pytest.skip("No built-in datasets available")

        first_ds = builtin[0]
        responses_lib.add(
            responses_lib.GET, first_ds.url, body=b"data", status=200
        )
        result = fetcher.fetch_by_key(first_ds.key)
        assert result is not None
        assert result.dataset_key == first_ds.key


# ---------------------------------------------------------------------------
# Module-level convenience function
# ---------------------------------------------------------------------------


class TestFetchDatasetConvenienceFunction:
    """Tests for the module-level fetch_dataset function."""

    @responses_lib.activate
    def test_returns_fetch_result(self, tmp_path: Path) -> None:
        """The convenience function returns a FetchResult."""
        responses_lib.add(
            responses_lib.GET, TEST_URL, body=TEST_CONTENT, status=200
        )
        result = fetch_dataset(_entry(), data_dir=tmp_path)
        assert isinstance(result, FetchResult)
        assert result.success is True

    @responses_lib.activate
    def test_works_without_store(self, tmp_path: Path) -> None:
        """The convenience function works without a store."""
        responses_lib.add(
            responses_lib.GET, TEST_URL, body=TEST_CONTENT, status=200
        )
        result = fetch_dataset(_entry(), data_dir=tmp_path, store=None)
        assert result.success is True
        assert result.snapshot_id is None

    @responses_lib.activate
    def test_persists_to_store_when_provided(
        self, tmp_path: Path, store: SnapshotStore
    ) -> None:
        """The convenience function persists to the store when one is provided."""
        responses_lib.add(
            responses_lib.GET, TEST_URL, body=TEST_CONTENT, status=200
        )
        result = fetch_dataset(_entry(), data_dir=tmp_path, store=store)
        assert result.snapshot_id is not None
        assert store.count_snapshots() == 1


# ---------------------------------------------------------------------------
# FetchResult.to_dict
# ---------------------------------------------------------------------------


class TestFetchResultToDict:
    """Tests for FetchResult.to_dict."""

    @responses_lib.activate
    def test_to_dict_has_expected_keys(self, fetcher: DatasetFetcher) -> None:
        """to_dict contains all expected keys."""
        responses_lib.add(
            responses_lib.GET, TEST_URL, body=TEST_CONTENT, status=200
        )
        result = fetcher.fetch_dataset(_entry())
        d = result.to_dict()
        for key in (
            "dataset_key",
            "fetched_at",
            "success",
            "snapshot_id",
            "content_hash",
            "file_path",
            "byte_size",
            "http_status",
            "is_duplicate",
            "error",
        ):
            assert key in d, f"Missing key '{key}' in to_dict result"

    @responses_lib.activate
    def test_to_dict_file_path_is_string(self, fetcher: DatasetFetcher) -> None:
        """file_path in to_dict is a string, not a Path."""
        responses_lib.add(
            responses_lib.GET, TEST_URL, body=TEST_CONTENT, status=200
        )
        result = fetcher.fetch_dataset(_entry())
        d = result.to_dict()
        assert isinstance(d["file_path"], str)

    def test_to_dict_failure_result(self, fetcher: DatasetFetcher) -> None:
        """to_dict works correctly for a failed FetchResult."""
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).isoformat()
        result = FetchResult(
            dataset_key="test_ds",
            fetched_at=ts,
            success=False,
            http_status=404,
            error=ValueError("not found"),
        )
        d = result.to_dict()
        assert d["success"] is False
        assert d["file_path"] is None
        assert "not found" in d["error"]
