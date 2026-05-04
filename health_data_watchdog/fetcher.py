"""Dataset fetcher for Health Data Watchdog.

This module handles HTTP downloading of public health datasets, computes
SHA-256 content hashes for change detection, persists raw snapshot files
to disk, and integrates with the SQLite-backed :class:`~health_data_watchdog.store.SnapshotStore`.

Typical usage::

    from pathlib import Path
    from health_data_watchdog.fetcher import DatasetFetcher
    from health_data_watchdog.store import SnapshotStore
    from health_data_watchdog.datasets import get_enabled_datasets

    store = SnapshotStore(Path("watchdog.db"))
    fetcher = DatasetFetcher(data_dir=Path("snapshots"), store=store)
    result = fetcher.fetch_dataset(get_enabled_datasets()[0])
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
from requests import Response
from requests.exceptions import ConnectionError, HTTPError, RequestException, Timeout

from health_data_watchdog.datasets import DatasetEntry
from health_data_watchdog.store import SnapshotRecord, SnapshotStore

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Default HTTP request timeout in seconds (connect, read).
DEFAULT_TIMEOUT: Tuple[int, int] = (15, 60)

#: Default chunk size for streaming downloads (64 KiB).
DEFAULT_CHUNK_SIZE: int = 65536

#: User-Agent header sent with every request.
USER_AGENT: str = (
    "health-data-watchdog/0.1.0 "
    "(+https://github.com/example/health-data-watchdog)"
)


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


class FetchResult:
    """The outcome of a single dataset fetch operation.

    Attributes:
        dataset_key: Machine-readable dataset identifier.
        success: ``True`` if the HTTP request succeeded and the file was
            saved to disk.
        snapshot_id: The ``id`` assigned by the store to the new
            :class:`~health_data_watchdog.store.SnapshotRecord`, or
            ``None`` if the fetch failed or was skipped.
        content_hash: SHA-256 hex digest of the downloaded content, or
            ``None`` on failure.
        file_path: Absolute path to the stored snapshot file, or ``None``
            on failure.
        byte_size: Number of bytes downloaded.
        http_status: HTTP response status code, or ``0`` if the request
            never completed.
        is_duplicate: ``True`` if the hash matches the previous snapshot
            (i.e. no content change detected).
        error: Exception instance if the fetch failed, otherwise ``None``.
        fetched_at: ISO-8601 UTC timestamp of the fetch attempt.
    """

    __slots__ = (
        "dataset_key",
        "success",
        "snapshot_id",
        "content_hash",
        "file_path",
        "byte_size",
        "http_status",
        "is_duplicate",
        "error",
        "fetched_at",
    )

    def __init__(
        self,
        dataset_key: str,
        fetched_at: str,
        success: bool = False,
        snapshot_id: Optional[int] = None,
        content_hash: Optional[str] = None,
        file_path: Optional[Path] = None,
        byte_size: int = 0,
        http_status: int = 0,
        is_duplicate: bool = False,
        error: Optional[Exception] = None,
    ) -> None:
        self.dataset_key = dataset_key
        self.fetched_at = fetched_at
        self.success = success
        self.snapshot_id = snapshot_id
        self.content_hash = content_hash
        self.file_path = file_path
        self.byte_size = byte_size
        self.http_status = http_status
        self.is_duplicate = is_duplicate
        self.error = error

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"FetchResult(dataset_key={self.dataset_key!r}, success={self.success!r}, "
            f"http_status={self.http_status!r}, byte_size={self.byte_size!r}, "
            f"is_duplicate={self.is_duplicate!r})"
        )

    def to_dict(self) -> Dict[str, object]:
        """Serialise to a plain dictionary (useful for logging/reporting)."""
        return {
            "dataset_key": self.dataset_key,
            "fetched_at": self.fetched_at,
            "success": self.success,
            "snapshot_id": self.snapshot_id,
            "content_hash": self.content_hash,
            "file_path": str(self.file_path) if self.file_path else None,
            "byte_size": self.byte_size,
            "http_status": self.http_status,
            "is_duplicate": self.is_duplicate,
            "error": str(self.error) if self.error else None,
        }


# ---------------------------------------------------------------------------
# Hashing helpers
# ---------------------------------------------------------------------------


def compute_sha256(data: bytes) -> str:
    """Compute the SHA-256 hex digest of *data*.

    Args:
        data: Raw bytes to hash.

    Returns:
        Lowercase hex-encoded SHA-256 digest string.
    """
    return hashlib.sha256(data).hexdigest()


def compute_sha256_file(path: Path, chunk_size: int = DEFAULT_CHUNK_SIZE) -> str:
    """Compute the SHA-256 hex digest of a file without loading it entirely.

    Args:
        path: Path to the file to hash.
        chunk_size: Number of bytes to read per iteration.

    Returns:
        Lowercase hex-encoded SHA-256 digest string.

    Raises:
        OSError: If the file cannot be read.
    """
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Snapshot file path helpers
# ---------------------------------------------------------------------------


def _snapshot_dir(data_dir: Path, dataset_key: str) -> Path:
    """Return (and create) the directory for a dataset's snapshot files.

    Args:
        data_dir: Root directory for all snapshots.
        dataset_key: The dataset key (used as a subdirectory name).

    Returns:
        Existing-or-newly-created :class:`Path` for the dataset.
    """
    d = data_dir / dataset_key
    d.mkdir(parents=True, exist_ok=True)
    return d


def _snapshot_filename(fetched_at: str, fmt: str) -> str:
    """Build a snapshot filename from a timestamp and format extension.

    The timestamp is sanitised so it is safe on all filesystems
    (colons replaced with hyphens).

    Args:
        fetched_at: ISO-8601 UTC timestamp string.
        fmt: File format / extension (e.g. ``"csv"``, ``"json"``).

    Returns:
        A filename string like ``"2024-01-15T12-30-00+00-00.csv"``.
    """
    safe_ts = fetched_at.replace(":", "-")
    return f"{safe_ts}.{fmt}"


# ---------------------------------------------------------------------------
# Core fetcher class
# ---------------------------------------------------------------------------


class DatasetFetcher:
    """Downloads datasets from HTTP(S) URLs and persists them as local snapshots.

    The fetcher streams each response to a temporary file, computes its
    SHA-256 hash, moves the file into the permanent snapshot directory
    (named by dataset key and timestamp), and inserts a
    :class:`~health_data_watchdog.store.SnapshotRecord` into the store.

    If the computed hash matches the most-recent stored snapshot for the
    same dataset key, the snapshot is still stored on disk and in the
    database (with ``is_duplicate=True``), so that the fetch history
    remains complete.

    Args:
        data_dir: Root directory under which per-dataset subdirectories
            are created to store raw snapshot files.
        store: An open :class:`~health_data_watchdog.store.SnapshotStore`
            instance.  If ``None``, snapshot metadata is **not** persisted
            to the database (useful for tests or one-off downloads).
        timeout: ``(connect_timeout, read_timeout)`` in seconds passed to
            :func:`requests.get`.
        chunk_size: Bytes per chunk when streaming the response body.
        session: Optional pre-configured :class:`requests.Session`.  If
            not provided, a new session is created automatically.
    """

    def __init__(
        self,
        data_dir: Path,
        store: Optional[SnapshotStore] = None,
        timeout: Tuple[int, int] = DEFAULT_TIMEOUT,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        session: Optional[requests.Session] = None,
    ) -> None:
        self._data_dir = Path(data_dir)
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._store = store
        self._timeout = timeout
        self._chunk_size = chunk_size
        if session is not None:
            self._session = session
            self._owns_session = False
        else:
            self._session = requests.Session()
            self._session.headers.update({"User-Agent": USER_AGENT})
            self._owns_session = True

    # ------------------------------------------------------------------
    # Context manager support
    # ------------------------------------------------------------------

    def __enter__(self) -> "DatasetFetcher":
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        self.close()

    def close(self) -> None:
        """Release the underlying HTTP session (if owned by this fetcher)."""
        if self._owns_session:
            try:
                self._session.close()
            except Exception:  # pragma: no cover
                pass

    # ------------------------------------------------------------------
    # Internal download helper
    # ------------------------------------------------------------------

    def _download_to_temp(
        self,
        url: str,
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> Tuple[Path, int, int, str]:
        """Stream *url* to a temporary file and return metadata.

        Args:
            url: The URL to fetch.
            extra_headers: Additional HTTP headers to merge for this request.

        Returns:
            A 4-tuple of ``(tmp_path, http_status, byte_size, content_hash)``
            where *tmp_path* is the temporary file path (caller must move or
            delete it), *http_status* is the response code, *byte_size* is
            the number of bytes written, and *content_hash* is the SHA-256
            digest.

        Raises:
            requests.HTTPError: If the server returns a 4xx/5xx status.
            requests.RequestException: On network-level errors.
        """
        headers: Dict[str, str] = {}
        if extra_headers:
            headers.update(extra_headers)

        response: Response = self._session.get(
            url,
            headers=headers,
            timeout=self._timeout,
            stream=True,
            allow_redirects=True,
        )
        response.raise_for_status()

        hasher = hashlib.sha256()
        byte_size = 0

        # Write to a temporary file in the data directory so that atomic
        # rename (move) stays on the same filesystem.
        tmp_fd, tmp_path_str = tempfile.mkstemp(dir=self._data_dir, suffix=".tmp")
        tmp_path = Path(tmp_path_str)
        try:
            with open(tmp_fd, "wb") as fh:
                for chunk in response.iter_content(chunk_size=self._chunk_size):
                    if chunk:  # filter out keep-alive empty chunks
                        fh.write(chunk)
                        hasher.update(chunk)
                        byte_size += len(chunk)
        except Exception:
            # Clean up temp file on failure
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:  # pragma: no cover
                pass
            raise

        content_hash = hasher.hexdigest()
        return tmp_path, response.status_code, byte_size, content_hash

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def data_dir(self) -> Path:
        """Return the root data directory for snapshot files."""
        return self._data_dir

    def fetch_dataset(self, dataset: DatasetEntry) -> FetchResult:
        """Fetch a single dataset and persist the result.

        Downloads the dataset at ``dataset.url``, computes its SHA-256
        hash, writes it to
        ``<data_dir>/<dataset_key>/<timestamp>.<format>``, and
        optionally records the snapshot in the store.

        Args:
            dataset: The :class:`~health_data_watchdog.datasets.DatasetEntry`
                to fetch.

        Returns:
            A :class:`FetchResult` describing the outcome.  The
            ``success`` attribute is ``True`` only if the download
            completed without errors and the file was saved.
        """
        fetched_at = datetime.now(timezone.utc).isoformat()
        logger.info("Fetching dataset '%s' from %s", dataset.key, dataset.url)

        tmp_path: Optional[Path] = None
        try:
            tmp_path, http_status, byte_size, content_hash = self._download_to_temp(
                dataset.url,
                extra_headers=dict(dataset.extra_headers),
            )
        except HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else 0
            logger.error(
                "HTTP error fetching '%s': %s", dataset.key, exc
            )
            return FetchResult(
                dataset_key=dataset.key,
                fetched_at=fetched_at,
                success=False,
                http_status=status_code,
                error=exc,
            )
        except (ConnectionError, Timeout) as exc:
            logger.error(
                "Network error fetching '%s': %s", dataset.key, exc
            )
            return FetchResult(
                dataset_key=dataset.key,
                fetched_at=fetched_at,
                success=False,
                http_status=0,
                error=exc,
            )
        except RequestException as exc:
            logger.error(
                "Request error fetching '%s': %s", dataset.key, exc
            )
            return FetchResult(
                dataset_key=dataset.key,
                fetched_at=fetched_at,
                success=False,
                http_status=0,
                error=exc,
            )
        except OSError as exc:
            logger.error(
                "I/O error fetching '%s': %s", dataset.key, exc
            )
            return FetchResult(
                dataset_key=dataset.key,
                fetched_at=fetched_at,
                success=False,
                http_status=0,
                error=exc,
            )

        # Move temp file to permanent location
        snap_dir = _snapshot_dir(self._data_dir, dataset.key)
        filename = _snapshot_filename(fetched_at, dataset.format)
        dest_path = snap_dir / filename

        try:
            shutil.move(str(tmp_path), str(dest_path))
        except OSError as exc:
            logger.error(
                "Failed to move snapshot file for '%s': %s", dataset.key, exc
            )
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:  # pragma: no cover
                pass
            return FetchResult(
                dataset_key=dataset.key,
                fetched_at=fetched_at,
                success=False,
                http_status=http_status,
                error=exc,
            )

        # Determine if this is a duplicate (same hash as previous snapshot)
        is_duplicate = False
        if self._store is not None:
            prev = self._store.get_latest_snapshot(dataset.key)
            if prev is not None and prev.content_hash == content_hash:
                is_duplicate = True
                logger.debug(
                    "Dataset '%s' content unchanged (hash=%s)",
                    dataset.key,
                    content_hash[:12],
                )
            else:
                logger.info(
                    "Dataset '%s' content changed or new (hash=%s)",
                    dataset.key,
                    content_hash[:12],
                )

        # Build and persist the snapshot record
        snap_record = SnapshotRecord(
            dataset_key=dataset.key,
            url=dataset.url,
            fetched_at=fetched_at,
            content_hash=content_hash,
            file_path=str(dest_path.resolve()),
            http_status=http_status,
            byte_size=byte_size,
            is_duplicate=is_duplicate,
        )

        snapshot_id: Optional[int] = None
        if self._store is not None:
            try:
                snapshot_id = self._store.insert_snapshot(
                    snap_record, auto_mark_duplicate=False
                )
            except Exception as exc:  # pragma: no cover
                logger.error(
                    "Failed to persist snapshot record for '%s': %s",
                    dataset.key,
                    exc,
                )

        return FetchResult(
            dataset_key=dataset.key,
            fetched_at=fetched_at,
            success=True,
            snapshot_id=snapshot_id,
            content_hash=content_hash,
            file_path=dest_path,
            byte_size=byte_size,
            http_status=http_status,
            is_duplicate=is_duplicate,
        )

    def fetch_all(
        self,
        datasets: List[DatasetEntry],
        *,
        skip_disabled: bool = True,
    ) -> List[FetchResult]:
        """Fetch every dataset in *datasets* and return all results.

        Args:
            datasets: List of :class:`~health_data_watchdog.datasets.DatasetEntry`
                objects to fetch.
            skip_disabled: If ``True`` (default), datasets with
                ``enabled=False`` are silently skipped.

        Returns:
            List of :class:`FetchResult` objects, one per attempted fetch.
            Disabled datasets are omitted when *skip_disabled* is ``True``.
        """
        results: List[FetchResult] = []
        for dataset in datasets:
            if skip_disabled and not dataset.enabled:
                logger.debug("Skipping disabled dataset '%s'", dataset.key)
                continue
            result = self.fetch_dataset(dataset)
            results.append(result)
        return results

    def fetch_by_key(
        self,
        key: str,
        datasets: Optional[List[DatasetEntry]] = None,
    ) -> Optional[FetchResult]:
        """Fetch a single dataset identified by *key*.

        Looks up the dataset in *datasets* (or the built-in registry if
        *datasets* is ``None``), then fetches it.

        Args:
            key: The machine-readable dataset key to look up.
            datasets: Optional list of datasets to search.  Defaults to
                the full built-in registry.

        Returns:
            A :class:`FetchResult` if the key was found and fetch was
            attempted, or ``None`` if the key was not found.
        """
        if datasets is None:
            from health_data_watchdog.datasets import get_builtin_datasets
            datasets = get_builtin_datasets()

        for dataset in datasets:
            if dataset.key == key:
                return self.fetch_dataset(dataset)

        logger.warning("Dataset key '%s' not found in provided registry.", key)
        return None


# ---------------------------------------------------------------------------
# Module-level convenience functions
# ---------------------------------------------------------------------------


def fetch_dataset(
    dataset: DatasetEntry,
    data_dir: Path,
    store: Optional[SnapshotStore] = None,
    timeout: Tuple[int, int] = DEFAULT_TIMEOUT,
) -> FetchResult:
    """Convenience function to fetch a single dataset without managing a fetcher.

    Creates a :class:`DatasetFetcher`, fetches *dataset*, closes the
    fetcher, and returns the result.

    Args:
        dataset: The dataset to fetch.
        data_dir: Root directory for snapshot storage.
        store: Optional open :class:`~health_data_watchdog.store.SnapshotStore`.
        timeout: HTTP timeout tuple.

    Returns:
        A :class:`FetchResult` describing the outcome.
    """
    with DatasetFetcher(data_dir=data_dir, store=store, timeout=timeout) as fetcher:
        return fetcher.fetch_dataset(dataset)
