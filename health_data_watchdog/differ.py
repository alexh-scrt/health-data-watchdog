"""Structural diff engine for Health Data Watchdog.

This module compares current and previous dataset snapshots to detect:
- Row-level additions and deletions
- Column additions and removals
- Data type (schema) changes
- Value-level changes within rows
- Complete dataset disappearance or first-time appearance

It uses ``deepdiff`` for deep comparison of serialised row data and
``pandas`` for structured parsing of CSV/JSON/TSV files.  Results are
returned as :class:`DiffResult` objects suitable for storage, reporting,
and alerting.

Typical usage::

    from pathlib import Path
    from health_data_watchdog.differ import DatasetDiffer, load_dataframe

    old_df = load_dataframe(Path("snapshots/cdc_covid/2024-01-01.csv"), "csv")
    new_df = load_dataframe(Path("snapshots/cdc_covid/2024-01-02.csv"), "csv")
    differ = DatasetDiffer()
    result = differ.diff("cdc_covid", old_df, new_df)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import pandas as pd
from deepdiff import DeepDiff

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Supported file formats and their pandas read function names.
SUPPORTED_FORMATS: Set[str] = {"csv", "json", "tsv", "xlsx", "parquet"}

#: Change type identifiers.
CHANGE_TYPE_NEW: str = "new"
CHANGE_TYPE_DELETION: str = "deletion"
CHANGE_TYPE_SCHEMA: str = "schema"
CHANGE_TYPE_CONTENT: str = "content"
CHANGE_TYPE_UNCHANGED: str = "unchanged"

#: Severity levels.
SEVERITY_INFO: str = "info"
SEVERITY_WARNING: str = "warning"
SEVERITY_CRITICAL: str = "critical"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class DiffError(Exception):
    """Raised when a diff operation cannot be completed."""


class UnsupportedFormatError(DiffError):
    """Raised when an unsupported file format is requested."""


# ---------------------------------------------------------------------------
# DiffResult dataclass
# ---------------------------------------------------------------------------


@dataclass
class ColumnChange:
    """Describes a change to a single column.

    Attributes:
        name: Column name.
        change_type: One of ``'added'``, ``'removed'``, ``'type_changed'``.
        old_dtype: Previous dtype string (may be empty for added columns).
        new_dtype: New dtype string (may be empty for removed columns).
    """

    name: str
    change_type: str  # 'added', 'removed', 'type_changed'
    old_dtype: str = ""
    new_dtype: str = ""


@dataclass
class DiffResult:
    """The outcome of comparing two dataset snapshots.

    Attributes:
        dataset_key: Machine-readable dataset identifier.
        change_type: High-level change category (``'new'``, ``'deletion'``,
            ``'schema'``, ``'content'``, ``'unchanged'``).
        severity: Alert severity (``'info'``, ``'warning'``, ``'critical'``).
        old_row_count: Number of rows in the previous snapshot (``-1`` if
            the previous snapshot did not exist).
        new_row_count: Number of rows in the current snapshot (``-1`` if
            the current snapshot does not exist / was deleted).
        row_delta: Net change in row count (``new_row_count - old_row_count``).
        old_columns: Ordered list of column names in the previous snapshot.
        new_columns: Ordered list of column names in the current snapshot.
        added_columns: Columns present in the new snapshot but not the old.
        removed_columns: Columns present in the old snapshot but not the new.
        column_changes: Detailed per-column change records.
        rows_added: Approximate number of rows added.
        rows_removed: Approximate number of rows removed.
        rows_modified: Approximate number of rows with value changes.
        row_change_pct: Percentage of rows that changed relative to the
            larger of the two snapshots.
        value_changes: Mapping of ``row_index -> {column: (old_val, new_val)}``
            for a sample of changed rows (capped at :attr:`max_value_changes`).
        summary: Short human-readable description of the change.
        diff_json: JSON-serialisable dict of the detailed diff data; can be
            stored in the database or exported to Markdown.
        error: If set, indicates that diffing failed with an exception.
    """

    dataset_key: str
    change_type: str = CHANGE_TYPE_UNCHANGED
    severity: str = SEVERITY_INFO
    old_row_count: int = -1
    new_row_count: int = -1
    row_delta: int = 0
    old_columns: List[str] = field(default_factory=list)
    new_columns: List[str] = field(default_factory=list)
    added_columns: List[str] = field(default_factory=list)
    removed_columns: List[str] = field(default_factory=list)
    column_changes: List[ColumnChange] = field(default_factory=list)
    rows_added: int = 0
    rows_removed: int = 0
    rows_modified: int = 0
    row_change_pct: float = 0.0
    value_changes: Dict[str, Dict[str, Tuple[Any, Any]]] = field(default_factory=dict)
    summary: str = ""
    diff_json: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None

    def has_changes(self) -> bool:
        """Return ``True`` if any change was detected."""
        return self.change_type not in (CHANGE_TYPE_UNCHANGED,)

    def to_store_dict(self) -> Dict[str, Any]:
        """Return a dict suitable for creating a :class:`~health_data_watchdog.store.ChangeRecord`.

        Returns:
            Dict with keys matching ChangeRecord constructor parameters.
        """
        return {
            "change_type": self.change_type,
            "severity": self.severity,
            "row_delta": self.row_delta,
            "col_delta": len(self.added_columns) - len(self.removed_columns),
            "summary": self.summary,
            "diff_json": json.dumps(self.diff_json, default=str),
        }


# ---------------------------------------------------------------------------
# File loading helpers
# ---------------------------------------------------------------------------


def load_dataframe(
    path: Path,
    fmt: str,
    *,
    max_rows: Optional[int] = None,
) -> pd.DataFrame:
    """Load a snapshot file into a :class:`pandas.DataFrame`.

    Args:
        path: Filesystem path to the snapshot file.
        fmt: File format; one of ``csv``, ``json``, ``tsv``, ``xlsx``,
            ``parquet``.
        max_rows: If set, load at most this many rows (useful for large
            datasets during testing or preview).

    Returns:
        A :class:`pandas.DataFrame` with the file contents.

    Raises:
        UnsupportedFormatError: If *fmt* is not in :data:`SUPPORTED_FORMATS`.
        DiffError: If the file cannot be read or parsed.
        FileNotFoundError: If *path* does not exist.
    """
    fmt = fmt.lower().strip()
    if fmt not in SUPPORTED_FORMATS:
        raise UnsupportedFormatError(
            f"Unsupported format '{fmt}'. Must be one of: {', '.join(sorted(SUPPORTED_FORMATS))}."
        )

    if not path.exists():
        raise FileNotFoundError(f"Snapshot file not found: {path}")

    try:
        if fmt == "csv":
            return pd.read_csv(path, nrows=max_rows, low_memory=False)
        elif fmt == "tsv":
            return pd.read_csv(path, sep="\t", nrows=max_rows, low_memory=False)
        elif fmt == "json":
            df = pd.read_json(path)
            if max_rows is not None:
                return df.head(max_rows)
            return df
        elif fmt == "xlsx":
            df = pd.read_excel(path, nrows=max_rows)
            return df
        elif fmt == "parquet":
            df = pd.read_parquet(path)
            if max_rows is not None:
                return df.head(max_rows)
            return df
        else:  # pragma: no cover — guarded by the check above
            raise UnsupportedFormatError(f"Unhandled format: {fmt}")
    except (UnsupportedFormatError, FileNotFoundError):
        raise
    except Exception as exc:
        raise DiffError(
            f"Failed to load '{path}' as {fmt!r}: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Column diff helpers
# ---------------------------------------------------------------------------


def _compare_columns(
    old_df: pd.DataFrame,
    new_df: pd.DataFrame,
) -> Tuple[List[str], List[str], List[ColumnChange]]:
    """Identify added, removed, and type-changed columns.

    Args:
        old_df: The previous snapshot DataFrame.
        new_df: The current snapshot DataFrame.

    Returns:
        A 3-tuple of ``(added_columns, removed_columns, column_changes)``
        where ``column_changes`` includes type-change entries in addition
        to add/remove entries.
    """
    old_cols: Set[str] = set(old_df.columns.tolist())
    new_cols: Set[str] = set(new_df.columns.tolist())

    added = sorted(new_cols - old_cols)
    removed = sorted(old_cols - new_cols)
    common = old_cols & new_cols

    changes: List[ColumnChange] = []

    for col in removed:
        changes.append(
            ColumnChange(
                name=col,
                change_type="removed",
                old_dtype=str(old_df[col].dtype),
                new_dtype="",
            )
        )

    for col in added:
        changes.append(
            ColumnChange(
                name=col,
                change_type="added",
                old_dtype="",
                new_dtype=str(new_df[col].dtype),
            )
        )

    for col in sorted(common):
        old_dtype = str(old_df[col].dtype)
        new_dtype = str(new_df[col].dtype)
        if old_dtype != new_dtype:
            changes.append(
                ColumnChange(
                    name=col,
                    change_type="type_changed",
                    old_dtype=old_dtype,
                    new_dtype=new_dtype,
                )
            )

    return added, removed, changes


# ---------------------------------------------------------------------------
# Row diff helpers
# ---------------------------------------------------------------------------


def _normalise_for_diff(df: pd.DataFrame) -> pd.DataFrame:
    """Normalise a DataFrame for consistent diffing.

    - Converts all values to strings to avoid type comparison issues
      across format round-trips.
    - Resets the index so row positions are stable.

    Args:
        df: Input DataFrame.

    Returns:
        Normalised copy with string values and a default RangeIndex.
    """
    return df.reset_index(drop=True).astype(str)


def _compare_rows(
    old_df: pd.DataFrame,
    new_df: pd.DataFrame,
    *,
    max_value_changes: int = 50,
) -> Tuple[int, int, int, float, Dict[str, Dict[str, Tuple[Any, Any]]]]:
    """Compute row-level change statistics between two DataFrames.

    Only columns present in *both* DataFrames are compared so that column
    additions/removals do not artificially inflate the changed-row count.

    Args:
        old_df: Previous snapshot DataFrame.
        new_df: Current snapshot DataFrame.
        max_value_changes: Maximum number of changed-row value samples to
            collect (to avoid huge diff objects).

    Returns:
        5-tuple of::

            (rows_added, rows_removed, rows_modified,
             row_change_pct, value_changes)

        where *value_changes* maps a string row index to a dict of
        ``{column: (old_val, new_val)}``.
    """
    common_cols = [c for c in old_df.columns if c in new_df.columns]

    old_norm = _normalise_for_diff(old_df[common_cols] if common_cols else old_df)
    new_norm = _normalise_for_diff(new_df[common_cols] if common_cols else new_df)

    old_len = len(old_norm)
    new_len = len(new_norm)
    min_len = min(old_len, new_len)
    max_len = max(old_len, new_len)

    rows_added = max(0, new_len - old_len)
    rows_removed = max(0, old_len - new_len)
    rows_modified = 0
    value_changes: Dict[str, Dict[str, Tuple[Any, Any]]] = {}

    if min_len > 0 and common_cols:
        old_slice = old_norm.iloc[:min_len]
        new_slice = new_norm.iloc[:min_len]

        # Compare row-by-row for the overlapping portion
        diff_mask = (old_slice != new_slice).any(axis=1)
        modified_indices = diff_mask[diff_mask].index.tolist()
        rows_modified = len(modified_indices)

        # Collect sample value changes
        for idx in modified_indices[:max_value_changes]:
            row_diffs: Dict[str, Tuple[Any, Any]] = {}
            for col in common_cols:
                old_val = old_slice.at[idx, col]
                new_val = new_slice.at[idx, col]
                if old_val != new_val:
                    row_diffs[col] = (old_val, new_val)
            if row_diffs:
                value_changes[str(idx)] = row_diffs

    total_changed = rows_added + rows_removed + rows_modified
    row_change_pct = (
        (total_changed / max_len) * 100.0 if max_len > 0 else 0.0
    )

    return rows_added, rows_removed, rows_modified, row_change_pct, value_changes


# ---------------------------------------------------------------------------
# Severity determination
# ---------------------------------------------------------------------------


def _determine_severity(
    change_type: str,
    removed_columns: List[str],
    row_change_pct: float,
    *,
    critical_row_change_pct: float = 10.0,
    warning_column_drop_count: int = 1,
) -> str:
    """Determine the alert severity for a detected change.

    Rules (applied in priority order):

    1. ``'deletion'`` or ``'new'`` → ``'critical'``
    2. Column removals >= *warning_column_drop_count* → ``'critical'``
    3. Row change percentage >= *critical_row_change_pct* → ``'critical'``
    4. Any schema change (column add/type change) → ``'warning'``
    5. Any content change → ``'warning'`` if row_change_pct > 0 else ``'info'``
    6. Unchanged → ``'info'``

    Args:
        change_type: The high-level change category.
        removed_columns: List of removed column names.
        row_change_pct: Percentage of rows that changed.
        critical_row_change_pct: Threshold above which row change is critical.
        warning_column_drop_count: Minimum column drops for a critical alert.

    Returns:
        Severity string: ``'info'``, ``'warning'``, or ``'critical'``.
    """
    if change_type in (CHANGE_TYPE_DELETION, CHANGE_TYPE_NEW):
        return SEVERITY_CRITICAL

    if len(removed_columns) >= warning_column_drop_count:
        return SEVERITY_CRITICAL

    if row_change_pct >= critical_row_change_pct:
        return SEVERITY_CRITICAL

    if change_type == CHANGE_TYPE_SCHEMA:
        return SEVERITY_WARNING

    if change_type == CHANGE_TYPE_CONTENT:
        return SEVERITY_WARNING if row_change_pct > 0 else SEVERITY_INFO

    return SEVERITY_INFO


# ---------------------------------------------------------------------------
# Summary builder
# ---------------------------------------------------------------------------


def _build_summary(
    change_type: str,
    dataset_key: str,
    old_row_count: int,
    new_row_count: int,
    row_delta: int,
    added_columns: List[str],
    removed_columns: List[str],
    rows_added: int,
    rows_removed: int,
    rows_modified: int,
    row_change_pct: float,
    column_changes: List[ColumnChange],
) -> str:
    """Build a concise human-readable summary string for a diff result.

    Args:
        change_type: High-level change category.
        dataset_key: Dataset identifier (for the message).
        old_row_count: Row count in old snapshot.
        new_row_count: Row count in new snapshot.
        row_delta: Net row change.
        added_columns: Names of added columns.
        removed_columns: Names of removed columns.
        rows_added: Rows added in overlapping position range.
        rows_removed: Rows removed in overlapping position range.
        rows_modified: Rows with value changes.
        row_change_pct: Percentage of rows that changed.
        column_changes: List of column change records.

    Returns:
        Summary string.
    """
    if change_type == CHANGE_TYPE_NEW:
        return (
            f"Dataset '{dataset_key}' appeared for the first time "
            f"with {new_row_count} rows."
        )

    if change_type == CHANGE_TYPE_DELETION:
        return (
            f"Dataset '{dataset_key}' was removed or became unavailable "
            f"(previously had {old_row_count} rows)."
        )

    if change_type == CHANGE_TYPE_UNCHANGED:
        return f"Dataset '{dataset_key}' is unchanged ({new_row_count} rows)."

    parts: List[str] = []

    if removed_columns:
        parts.append(f"{len(removed_columns)} column(s) removed: {', '.join(removed_columns)}")

    if added_columns:
        parts.append(f"{len(added_columns)} column(s) added: {', '.join(added_columns)}")

    type_changed = [c for c in column_changes if c.change_type == "type_changed"]
    if type_changed:
        parts.append(
            f"{len(type_changed)} column(s) changed type: "
            + ", ".join(f"{c.name} ({c.old_dtype}→{c.new_dtype})" for c in type_changed)
        )

    if rows_added:
        parts.append(f"{rows_added} row(s) added")
    if rows_removed:
        parts.append(f"{rows_removed} row(s) removed")
    if rows_modified:
        parts.append(f"{rows_modified} row(s) modified")

    if row_delta != 0:
        sign = "+" if row_delta > 0 else ""
        parts.append(f"net row delta: {sign}{row_delta}")

    if row_change_pct > 0:
        parts.append(f"{row_change_pct:.1f}% of rows affected")

    if not parts:
        return f"Dataset '{dataset_key}' changed (details in diff)."

    return f"Dataset '{dataset_key}' changed: " + "; ".join(parts) + "."


# ---------------------------------------------------------------------------
# Main differ class
# ---------------------------------------------------------------------------


class DatasetDiffer:
    """Compares two dataset snapshots and produces a structured :class:`DiffResult`.

    Args:
        critical_row_change_pct: Percentage of changed rows that triggers a
            CRITICAL severity (default 10.0).
        warning_column_drop_count: Minimum number of dropped columns that
            triggers a CRITICAL severity (default 1).
        max_value_changes: Maximum number of row-level value change samples
            to include in the result (default 50).
    """

    def __init__(
        self,
        critical_row_change_pct: float = 10.0,
        warning_column_drop_count: int = 1,
        max_value_changes: int = 50,
    ) -> None:
        self._critical_row_change_pct = critical_row_change_pct
        self._warning_column_drop_count = warning_column_drop_count
        self._max_value_changes = max_value_changes

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def diff(
        self,
        dataset_key: str,
        old_df: Optional[pd.DataFrame],
        new_df: Optional[pd.DataFrame],
    ) -> DiffResult:
        """Compare *old_df* and *new_df* and return a :class:`DiffResult`.

        Special cases:

        - If *old_df* is ``None`` and *new_df* is not: ``change_type='new'``.
        - If *old_df* is not ``None`` and *new_df* is ``None``: ``change_type='deletion'``.
        - If both are ``None``: returns an unchanged result with a warning.

        Args:
            dataset_key: Machine-readable dataset identifier.
            old_df: The previous snapshot as a DataFrame, or ``None`` if no
                previous snapshot exists.
            new_df: The current snapshot as a DataFrame, or ``None`` if the
                dataset has been deleted or is unavailable.

        Returns:
            A :class:`DiffResult` describing all detected changes.
        """
        if old_df is None and new_df is None:
            logger.warning(
                "diff() called with both old_df and new_df as None for '%s'",
                dataset_key,
            )
            return DiffResult(
                dataset_key=dataset_key,
                change_type=CHANGE_TYPE_UNCHANGED,
                severity=SEVERITY_INFO,
                summary=f"No data available for dataset '{dataset_key}'.",
            )

        if old_df is None and new_df is not None:
            return self._diff_new(dataset_key, new_df)

        if old_df is not None and new_df is None:
            return self._diff_deletion(dataset_key, old_df)

        # Both snapshots exist — perform a structural diff.
        assert old_df is not None and new_df is not None  # type narrowing
        return self._diff_content(dataset_key, old_df, new_df)

    def diff_files(
        self,
        dataset_key: str,
        old_path: Optional[Path],
        new_path: Optional[Path],
        fmt: str,
    ) -> DiffResult:
        """Load two snapshot files and diff them.

        Convenience wrapper around :meth:`diff` that handles file loading
        and returns a :class:`DiffResult` with ``error`` set on failure.

        Args:
            dataset_key: Machine-readable dataset identifier.
            old_path: Path to the previous snapshot file, or ``None``.
            new_path: Path to the current snapshot file, or ``None``.
            fmt: File format (``csv``, ``json``, etc.).

        Returns:
            A :class:`DiffResult` — ``error`` is set if loading fails.
        """
        old_df: Optional[pd.DataFrame] = None
        new_df: Optional[pd.DataFrame] = None

        if old_path is not None:
            try:
                old_df = load_dataframe(old_path, fmt)
            except Exception as exc:
                logger.error(
                    "Failed to load old snapshot for '%s': %s", dataset_key, exc
                )
                return DiffResult(
                    dataset_key=dataset_key,
                    change_type=CHANGE_TYPE_UNCHANGED,
                    severity=SEVERITY_INFO,
                    summary=f"Failed to load old snapshot: {exc}",
                    error=str(exc),
                )

        if new_path is not None:
            try:
                new_df = load_dataframe(new_path, fmt)
            except Exception as exc:
                logger.error(
                    "Failed to load new snapshot for '%s': %s", dataset_key, exc
                )
                return DiffResult(
                    dataset_key=dataset_key,
                    change_type=CHANGE_TYPE_UNCHANGED,
                    severity=SEVERITY_INFO,
                    summary=f"Failed to load new snapshot: {exc}",
                    error=str(exc),
                )

        return self.diff(dataset_key, old_df, new_df)

    # ------------------------------------------------------------------
    # Private diff helpers
    # ------------------------------------------------------------------

    def _diff_new(
        self, dataset_key: str, new_df: pd.DataFrame
    ) -> DiffResult:
        """Build a DiffResult for a dataset appearing for the first time."""
        new_row_count = len(new_df)
        new_columns = new_df.columns.tolist()

        summary = (
            f"Dataset '{dataset_key}' appeared for the first time "
            f"with {new_row_count} rows and {len(new_columns)} columns."
        )

        diff_json: Dict[str, Any] = {
            "event": "new_dataset",
            "new_row_count": new_row_count,
            "new_columns": new_columns,
        }

        return DiffResult(
            dataset_key=dataset_key,
            change_type=CHANGE_TYPE_NEW,
            severity=SEVERITY_CRITICAL,
            old_row_count=-1,
            new_row_count=new_row_count,
            row_delta=new_row_count,
            old_columns=[],
            new_columns=new_columns,
            added_columns=new_columns,
            removed_columns=[],
            rows_added=new_row_count,
            summary=summary,
            diff_json=diff_json,
        )

    def _diff_deletion(
        self, dataset_key: str, old_df: pd.DataFrame
    ) -> DiffResult:
        """Build a DiffResult for a dataset that has disappeared."""
        old_row_count = len(old_df)
        old_columns = old_df.columns.tolist()

        summary = (
            f"Dataset '{dataset_key}' was removed or became unavailable "
            f"(previously had {old_row_count} rows and {len(old_columns)} columns)."
        )

        diff_json: Dict[str, Any] = {
            "event": "dataset_deleted",
            "old_row_count": old_row_count,
            "old_columns": old_columns,
        }

        return DiffResult(
            dataset_key=dataset_key,
            change_type=CHANGE_TYPE_DELETION,
            severity=SEVERITY_CRITICAL,
            old_row_count=old_row_count,
            new_row_count=-1,
            row_delta=-old_row_count,
            old_columns=old_columns,
            new_columns=[],
            removed_columns=old_columns,
            added_columns=[],
            rows_removed=old_row_count,
            summary=summary,
            diff_json=diff_json,
        )

    def _diff_content(
        self,
        dataset_key: str,
        old_df: pd.DataFrame,
        new_df: pd.DataFrame,
    ) -> DiffResult:
        """Perform a full structural diff between two non-None DataFrames."""
        old_row_count = len(old_df)
        new_row_count = len(new_df)
        row_delta = new_row_count - old_row_count
        old_columns = old_df.columns.tolist()
        new_columns = new_df.columns.tolist()

        # Column-level diff
        added_columns, removed_columns, column_changes = _compare_columns(
            old_df, new_df
        )

        # Row-level diff (on common columns)
        (
            rows_added,
            rows_removed,
            rows_modified,
            row_change_pct,
            value_changes,
        ) = _compare_rows(
            old_df,
            new_df,
            max_value_changes=self._max_value_changes,
        )

        # Determine change type
        has_schema_change = bool(column_changes)
        has_content_change = (
            rows_added > 0
            or rows_removed > 0
            or rows_modified > 0
            or row_delta != 0
        )

        if has_schema_change and has_content_change:
            change_type = CHANGE_TYPE_SCHEMA
        elif has_schema_change:
            change_type = CHANGE_TYPE_SCHEMA
        elif has_content_change:
            change_type = CHANGE_TYPE_CONTENT
        else:
            change_type = CHANGE_TYPE_UNCHANGED

        # Severity
        severity = _determine_severity(
            change_type,
            removed_columns,
            row_change_pct,
            critical_row_change_pct=self._critical_row_change_pct,
            warning_column_drop_count=self._warning_column_drop_count,
        )

        # Summary
        summary = _build_summary(
            change_type=change_type,
            dataset_key=dataset_key,
            old_row_count=old_row_count,
            new_row_count=new_row_count,
            row_delta=row_delta,
            added_columns=added_columns,
            removed_columns=removed_columns,
            rows_added=rows_added,
            rows_removed=rows_removed,
            rows_modified=rows_modified,
            row_change_pct=row_change_pct,
            column_changes=column_changes,
        )

        # Diff JSON payload
        diff_json: Dict[str, Any] = {
            "old_row_count": old_row_count,
            "new_row_count": new_row_count,
            "row_delta": row_delta,
            "old_columns": old_columns,
            "new_columns": new_columns,
            "added_columns": added_columns,
            "removed_columns": removed_columns,
            "column_type_changes": [
                {
                    "name": c.name,
                    "change_type": c.change_type,
                    "old_dtype": c.old_dtype,
                    "new_dtype": c.new_dtype,
                }
                for c in column_changes
                if c.change_type == "type_changed"
            ],
            "rows_added": rows_added,
            "rows_removed": rows_removed,
            "rows_modified": rows_modified,
            "row_change_pct": round(row_change_pct, 4),
            "value_changes_sample": {
                k: {col: list(vals) for col, vals in v.items()}
                for k, v in value_changes.items()
            },
        }

        return DiffResult(
            dataset_key=dataset_key,
            change_type=change_type,
            severity=severity,
            old_row_count=old_row_count,
            new_row_count=new_row_count,
            row_delta=row_delta,
            old_columns=old_columns,
            new_columns=new_columns,
            added_columns=added_columns,
            removed_columns=removed_columns,
            column_changes=column_changes,
            rows_added=rows_added,
            rows_removed=rows_removed,
            rows_modified=rows_modified,
            row_change_pct=row_change_pct,
            value_changes=value_changes,
            summary=summary,
            diff_json=diff_json,
        )


# ---------------------------------------------------------------------------
# Module-level convenience function
# ---------------------------------------------------------------------------


def diff_dataframes(
    dataset_key: str,
    old_df: Optional[pd.DataFrame],
    new_df: Optional[pd.DataFrame],
    *,
    critical_row_change_pct: float = 10.0,
    warning_column_drop_count: int = 1,
) -> DiffResult:
    """Convenience function to diff two DataFrames without managing a differ.

    Args:
        dataset_key: Machine-readable dataset identifier.
        old_df: Previous snapshot, or ``None``.
        new_df: Current snapshot, or ``None``.
        critical_row_change_pct: Threshold for critical severity.
        warning_column_drop_count: Minimum column drops for critical severity.

    Returns:
        A :class:`DiffResult` describing detected changes.
    """
    differ = DatasetDiffer(
        critical_row_change_pct=critical_row_change_pct,
        warning_column_drop_count=warning_column_drop_count,
    )
    return differ.diff(dataset_key, old_df, new_df)
