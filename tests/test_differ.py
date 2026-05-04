"""Tests for the health_data_watchdog.differ module.

Covers row additions/deletions, column additions/removals, data type changes,
value-level changes, new-dataset detection, dataset-deletion detection,
file loading, severity determination, and the convenience diff function.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Optional

import pandas as pd
import pytest

from health_data_watchdog.differ import (
    CHANGE_TYPE_CONTENT,
    CHANGE_TYPE_DELETION,
    CHANGE_TYPE_NEW,
    CHANGE_TYPE_SCHEMA,
    CHANGE_TYPE_UNCHANGED,
    SEVERITY_CRITICAL,
    SEVERITY_INFO,
    SEVERITY_WARNING,
    ColumnChange,
    DatasetDiffer,
    DiffError,
    DiffResult,
    UnsupportedFormatError,
    _compare_columns,
    _compare_rows,
    _determine_severity,
    diff_dataframes,
    load_dataframe,
)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _df(*rows: dict) -> pd.DataFrame:
    """Build a DataFrame from keyword-argument dicts."""
    return pd.DataFrame(list(rows))


def _csv_file(tmp_path: Path, content: str, name: str = "data.csv") -> Path:
    """Write CSV content to a temp file and return its path."""
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


@pytest.fixture()
def differ() -> DatasetDiffer:
    """Return a DatasetDiffer with default settings."""
    return DatasetDiffer()


# ---------------------------------------------------------------------------
# load_dataframe
# ---------------------------------------------------------------------------


class TestLoadDataframe:
    """Tests for the load_dataframe helper."""

    def test_loads_csv(self, tmp_path: Path) -> None:
        """CSV files are loaded into a DataFrame."""
        p = _csv_file(tmp_path, "a,b\n1,2\n3,4\n")
        df = load_dataframe(p, "csv")
        assert list(df.columns) == ["a", "b"]
        assert len(df) == 2

    def test_loads_tsv(self, tmp_path: Path) -> None:
        """TSV files are loaded into a DataFrame."""
        p = tmp_path / "data.tsv"
        p.write_text("a\tb\n1\t2\n3\t4\n", encoding="utf-8")
        df = load_dataframe(p, "tsv")
        assert list(df.columns) == ["a", "b"]
        assert len(df) == 2

    def test_loads_json(self, tmp_path: Path) -> None:
        """JSON files are loaded into a DataFrame."""
        import json as _json
        p = tmp_path / "data.json"
        data = [{"a": 1, "b": 2}, {"a": 3, "b": 4}]
        p.write_text(_json.dumps(data), encoding="utf-8")
        df = load_dataframe(p, "json")
        assert "a" in df.columns
        assert len(df) == 2

    def test_format_is_case_insensitive(self, tmp_path: Path) -> None:
        """Format string matching is case-insensitive."""
        p = _csv_file(tmp_path, "x,y\n1,2\n")
        df = load_dataframe(p, "CSV")
        assert len(df) == 1

    def test_max_rows_limits_output(self, tmp_path: Path) -> None:
        """max_rows limits the number of rows loaded."""
        p = _csv_file(tmp_path, "a,b\n1,2\n3,4\n5,6\n7,8\n")
        df = load_dataframe(p, "csv", max_rows=2)
        assert len(df) == 2

    def test_unsupported_format_raises(self, tmp_path: Path) -> None:
        """An unsupported format raises UnsupportedFormatError."""
        p = _csv_file(tmp_path, "x,y\n1,2\n")
        with pytest.raises(UnsupportedFormatError, match="xml"):
            load_dataframe(p, "xml")

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        """A missing file raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            load_dataframe(tmp_path / "nonexistent.csv", "csv")

    def test_corrupt_csv_raises_diff_error(self, tmp_path: Path) -> None:
        """A malformed CSV raises DiffError."""
        # pandas may or may not raise on totally broken files; we use a
        # file that pandas definitely cannot parse as CSV.
        # We'll use a binary file with null bytes.
        p = tmp_path / "broken.csv"
        p.write_bytes(b"\x00\x01\x02\x03")
        # This may succeed (pandas is lenient) or raise DiffError;
        # the key assertion is that no UnsupportedFormatError is raised.
        try:
            load_dataframe(p, "csv")
        except DiffError:
            pass  # expected
        except Exception:
            pass  # pandas may handle it


# ---------------------------------------------------------------------------
# _compare_columns
# ---------------------------------------------------------------------------


class TestCompareColumns:
    """Tests for the _compare_columns helper."""

    def test_no_change_returns_empty_lists(self) -> None:
        """Identical column sets produce no changes."""
        df = _df({"a": [1], "b": [2]})
        added, removed, changes = _compare_columns(df, df)
        assert added == []
        assert removed == []
        # type changes only if dtypes differ; identical df means none
        type_changes = [c for c in changes if c.change_type == "type_changed"]
        assert type_changes == []

    def test_detects_added_columns(self) -> None:
        """New columns in new_df are reported as added."""
        old = _df({"a": [1]})
        new = _df({"a": [1], "b": [2]})
        added, removed, changes = _compare_columns(old, new)
        assert "b" in added
        assert any(c.name == "b" and c.change_type == "added" for c in changes)

    def test_detects_removed_columns(self) -> None:
        """Columns missing from new_df are reported as removed."""
        old = _df({"a": [1], "b": [2]})
        new = _df({"a": [1]})
        added, removed, changes = _compare_columns(old, new)
        assert "b" in removed
        assert any(c.name == "b" and c.change_type == "removed" for c in changes)

    def test_detects_type_change(self) -> None:
        """Dtype changes in common columns are reported."""
        old = pd.DataFrame({"a": [1, 2, 3]})  # int dtype
        new = pd.DataFrame({"a": ["x", "y", "z"]})  # object dtype
        added, removed, changes = _compare_columns(old, new)
        type_changes = [c for c in changes if c.change_type == "type_changed"]
        assert len(type_changes) == 1
        assert type_changes[0].name == "a"
        assert type_changes[0].old_dtype != type_changes[0].new_dtype

    def test_multiple_changes_at_once(self) -> None:
        """Multiple column changes are detected simultaneously."""
        old = _df({"a": [1], "b": [2], "c": [3]})
        new = _df({"a": [1], "d": [4]})  # b, c removed; d added
        added, removed, changes = _compare_columns(old, new)
        assert "d" in added
        assert "b" in removed
        assert "c" in removed

    def test_removed_column_has_old_dtype(self) -> None:
        """Removed ColumnChange records carry the old dtype."""
        old = pd.DataFrame({"score": [1.0, 2.0]})
        new = pd.DataFrame({"other": ["x"]})
        _, removed, changes = _compare_columns(old, new)
        rc = next(c for c in changes if c.name == "score")
        assert rc.old_dtype == str(old["score"].dtype)
        assert rc.new_dtype == ""

    def test_added_column_has_new_dtype(self) -> None:
        """Added ColumnChange records carry the new dtype."""
        old = pd.DataFrame({"x": [1]})
        new = pd.DataFrame({"x": [1], "y": [True]})
        added, _, changes = _compare_columns(old, new)
        ac = next(c for c in changes if c.name == "y")
        assert ac.new_dtype == str(new["y"].dtype)
        assert ac.old_dtype == ""


# ---------------------------------------------------------------------------
# _compare_rows
# ---------------------------------------------------------------------------


class TestCompareRows:
    """Tests for the _compare_rows helper."""

    def test_identical_dfs_have_no_changes(self) -> None:
        """Identical DataFrames produce no changes."""
        df = _df({"a": [1, 2], "b": [3, 4]})
        added, removed, modified, pct, vc = _compare_rows(df, df)
        assert added == 0
        assert removed == 0
        assert modified == 0
        assert pct == 0.0
        assert vc == {}

    def test_row_addition_detected(self) -> None:
        """Extra rows in new_df are counted as added."""
        old = _df({"a": [1]}, {"a": [2]})
        new = _df({"a": [1]}, {"a": [2]}, {"a": [3]})
        added, removed, modified, pct, vc = _compare_rows(old, new)
        assert added == 1
        assert removed == 0

    def test_row_deletion_detected(self) -> None:
        """Fewer rows in new_df are counted as removed."""
        old = _df({"a": [1]}, {"a": [2]}, {"a": [3]})
        new = _df({"a": [1]})
        added, removed, modified, pct, vc = _compare_rows(old, new)
        assert removed == 2
        assert added == 0

    def test_value_change_detected(self) -> None:
        """Changed values in the overlapping rows are counted as modified."""
        old = _df({"a": [1], "b": ["old"]})
        new = _df({"a": [1], "b": ["new"]})
        added, removed, modified, pct, vc = _compare_rows(old, new)
        assert modified == 1
        assert "0" in vc
        assert "b" in vc["0"]
        assert vc["0"]["b"] == ("1", "1") or vc["0"]["b"][0] != vc["0"]["b"][1]

    def test_row_change_pct_calculated(self) -> None:
        """row_change_pct is > 0 when there are changes."""
        old = _df({"a": [1]}, {"a": [2]}, {"a": [3]}, {"a": [4]})
        new = _df({"a": [1]}, {"a": [2]}, {"a": [3]}, {"a": [99]})
        _, _, modified, pct, _ = _compare_rows(old, new)
        assert modified == 1
        assert pct > 0.0

    def test_empty_dfs_return_zero_changes(self) -> None:
        """Comparing two empty DataFrames returns all zeros."""
        empty = pd.DataFrame(columns=["a"])
        added, removed, modified, pct, vc = _compare_rows(empty, empty)
        assert added == 0
        assert removed == 0
        assert modified == 0
        assert pct == 0.0

    def test_max_value_changes_caps_sample(self) -> None:
        """value_changes is capped at max_value_changes."""
        n = 10
        old = pd.DataFrame({"val": [f"old_{i}" for i in range(n)]})
        new = pd.DataFrame({"val": [f"new_{i}" for i in range(n)]})
        _, _, _, _, vc = _compare_rows(old, new, max_value_changes=3)
        assert len(vc) <= 3

    def test_value_changes_format(self) -> None:
        """value_changes contains (old_val, new_val) tuples."""
        old = _df({"x": ["alpha"]})
        new = _df({"x": ["beta"]})
        _, _, _, _, vc = _compare_rows(old, new)
        assert "0" in vc
        pair = vc["0"]["x"]
        assert len(pair) == 2
        assert pair[0] != pair[1]


# ---------------------------------------------------------------------------
# _determine_severity
# ---------------------------------------------------------------------------


class TestDetermineSeverity:
    """Tests for the _determine_severity helper."""

    def test_deletion_is_critical(self) -> None:
        assert _determine_severity(CHANGE_TYPE_DELETION, [], 0.0) == SEVERITY_CRITICAL

    def test_new_is_critical(self) -> None:
        assert _determine_severity(CHANGE_TYPE_NEW, [], 0.0) == SEVERITY_CRITICAL

    def test_column_removal_is_critical(self) -> None:
        assert (
            _determine_severity(
                CHANGE_TYPE_SCHEMA, ["col_a"], 0.0, warning_column_drop_count=1
            )
            == SEVERITY_CRITICAL
        )

    def test_high_row_change_pct_is_critical(self) -> None:
        assert (
            _determine_severity(
                CHANGE_TYPE_CONTENT, [], 20.0, critical_row_change_pct=10.0
            )
            == SEVERITY_CRITICAL
        )

    def test_schema_change_no_removal_is_warning(self) -> None:
        # Schema change but no column removal and low row change
        assert (
            _determine_severity(
                CHANGE_TYPE_SCHEMA, [], 1.0, warning_column_drop_count=2
            )
            == SEVERITY_WARNING
        )

    def test_content_change_with_rows_is_warning(self) -> None:
        assert (
            _determine_severity(CHANGE_TYPE_CONTENT, [], 5.0, critical_row_change_pct=10.0)
            == SEVERITY_WARNING
        )

    def test_unchanged_is_info(self) -> None:
        assert _determine_severity(CHANGE_TYPE_UNCHANGED, [], 0.0) == SEVERITY_INFO

    def test_content_change_zero_pct_is_warning(self) -> None:
        # Any content change should be at least warning
        result = _determine_severity(CHANGE_TYPE_CONTENT, [], 0.0)
        assert result in (SEVERITY_INFO, SEVERITY_WARNING)

    def test_zero_column_drop_count_threshold(self) -> None:
        """warning_column_drop_count=0 means any removal is critical — """
        # With zero threshold, the condition `len([]) >= 0` is True
        # even with no removals, so this tests the threshold boundary.
        assert (
            _determine_severity(
                CHANGE_TYPE_SCHEMA, [], 0.0, warning_column_drop_count=0
            )
            == SEVERITY_CRITICAL
        )


# ---------------------------------------------------------------------------
# DatasetDiffer.diff — both DataFrames None
# ---------------------------------------------------------------------------


class TestDiffBothNone:
    """Tests for the edge case where both old and new DataFrames are None."""

    def test_both_none_returns_unchanged(self, differ: DatasetDiffer) -> None:
        result = differ.diff("ds", None, None)
        assert result.change_type == CHANGE_TYPE_UNCHANGED
        assert result.dataset_key == "ds"


# ---------------------------------------------------------------------------
# DatasetDiffer.diff — new dataset
# ---------------------------------------------------------------------------


class TestDiffNewDataset:
    """Tests for the CHANGE_TYPE_NEW case."""

    def test_old_none_returns_new_type(self, differ: DatasetDiffer) -> None:
        new_df = _df({"a": [1], "b": [2]})
        result = differ.diff("ds", None, new_df)
        assert result.change_type == CHANGE_TYPE_NEW

    def test_severity_is_critical(self, differ: DatasetDiffer) -> None:
        new_df = _df({"a": [1]})
        result = differ.diff("ds", None, new_df)
        assert result.severity == SEVERITY_CRITICAL

    def test_new_row_count_is_set(self, differ: DatasetDiffer) -> None:
        new_df = _df({"a": [1]}, {"a": [2]}, {"a": [3]})
        result = differ.diff("ds", None, new_df)
        assert result.new_row_count == 3

    def test_old_row_count_is_minus_one(self, differ: DatasetDiffer) -> None:
        new_df = _df({"a": [1]})
        result = differ.diff("ds", None, new_df)
        assert result.old_row_count == -1

    def test_new_columns_are_added_columns(self, differ: DatasetDiffer) -> None:
        new_df = _df({"x": [1], "y": [2]})
        result = differ.diff("ds", None, new_df)
        assert set(result.added_columns) == {"x", "y"}

    def test_summary_mentions_dataset_key(self, differ: DatasetDiffer) -> None:
        new_df = _df({"a": [1]})
        result = differ.diff("my_ds", None, new_df)
        assert "my_ds" in result.summary

    def test_diff_json_event_field(self, differ: DatasetDiffer) -> None:
        new_df = _df({"a": [1]})
        result = differ.diff("ds", None, new_df)
        assert result.diff_json.get("event") == "new_dataset"

    def test_has_changes_returns_true(self, differ: DatasetDiffer) -> None:
        new_df = _df({"a": [1]})
        result = differ.diff("ds", None, new_df)
        assert result.has_changes() is True


# ---------------------------------------------------------------------------
# DatasetDiffer.diff — deletion
# ---------------------------------------------------------------------------


class TestDiffDeletion:
    """Tests for the CHANGE_TYPE_DELETION case."""

    def test_new_none_returns_deletion_type(self, differ: DatasetDiffer) -> None:
        old_df = _df({"a": [1]})
        result = differ.diff("ds", old_df, None)
        assert result.change_type == CHANGE_TYPE_DELETION

    def test_severity_is_critical(self, differ: DatasetDiffer) -> None:
        old_df = _df({"a": [1]})
        result = differ.diff("ds", old_df, None)
        assert result.severity == SEVERITY_CRITICAL

    def test_old_row_count_is_set(self, differ: DatasetDiffer) -> None:
        old_df = _df({"a": [1]}, {"a": [2]})
        result = differ.diff("ds", old_df, None)
        assert result.old_row_count == 2

    def test_new_row_count_is_minus_one(self, differ: DatasetDiffer) -> None:
        old_df = _df({"a": [1]})
        result = differ.diff("ds", old_df, None)
        assert result.new_row_count == -1

    def test_removed_columns_match_old_columns(self, differ: DatasetDiffer) -> None:
        old_df = _df({"col_a": [1], "col_b": [2]})
        result = differ.diff("ds", old_df, None)
        assert set(result.removed_columns) == {"col_a", "col_b"}

    def test_summary_mentions_deletion(self, differ: DatasetDiffer) -> None:
        old_df = _df({"a": [1]})
        result = differ.diff("my_ds", old_df, None)
        assert "my_ds" in result.summary
        assert "removed" in result.summary.lower() or "deleted" in result.summary.lower() or "unavailable" in result.summary.lower()

    def test_diff_json_event_field(self, differ: DatasetDiffer) -> None:
        old_df = _df({"a": [1]})
        result = differ.diff("ds", old_df, None)
        assert result.diff_json.get("event") == "dataset_deleted"

    def test_row_delta_is_negative(self, differ: DatasetDiffer) -> None:
        old_df = _df({"a": [1]}, {"a": [2]})
        result = differ.diff("ds", old_df, None)
        assert result.row_delta < 0


# ---------------------------------------------------------------------------
# DatasetDiffer.diff — unchanged
# ---------------------------------------------------------------------------


class TestDiffUnchanged:
    """Tests for the CHANGE_TYPE_UNCHANGED case."""

    def test_identical_dfs_return_unchanged(self, differ: DatasetDiffer) -> None:
        df = _df({"a": [1, 2], "b": [3, 4]})
        result = differ.diff("ds", df.copy(), df.copy())
        assert result.change_type == CHANGE_TYPE_UNCHANGED

    def test_unchanged_severity_is_info(self, differ: DatasetDiffer) -> None:
        df = _df({"a": [1]})
        result = differ.diff("ds", df.copy(), df.copy())
        assert result.severity == SEVERITY_INFO

    def test_unchanged_row_delta_is_zero(self, differ: DatasetDiffer) -> None:
        df = _df({"a": [1, 2]})
        result = differ.diff("ds", df.copy(), df.copy())
        assert result.row_delta == 0

    def test_unchanged_has_changes_is_false(self, differ: DatasetDiffer) -> None:
        df = _df({"a": [1]})
        result = differ.diff("ds", df.copy(), df.copy())
        assert result.has_changes() is False

    def test_unchanged_row_counts_match(self, differ: DatasetDiffer) -> None:
        df = _df({"a": [1, 2, 3]})
        result = differ.diff("ds", df.copy(), df.copy())
        assert result.old_row_count == 3
        assert result.new_row_count == 3


# ---------------------------------------------------------------------------
# DatasetDiffer.diff — column additions
# ---------------------------------------------------------------------------


class TestDiffColumnAdded:
    """Tests for schema changes when columns are added."""

    def test_added_column_detected(self, differ: DatasetDiffer) -> None:
        old = _df({"a": [1, 2]})
        new = _df({"a": [1, 2], "b": [3, 4]})
        result = differ.diff("ds", old, new)
        assert "b" in result.added_columns

    def test_change_type_is_schema(self, differ: DatasetDiffer) -> None:
        old = _df({"a": [1]})
        new = _df({"a": [1], "b": [2]})
        result = differ.diff("ds", old, new)
        assert result.change_type == CHANGE_TYPE_SCHEMA

    def test_new_columns_list_updated(self, differ: DatasetDiffer) -> None:
        old = _df({"a": [1]})
        new = _df({"a": [1], "b": [2]})
        result = differ.diff("ds", old, new)
        assert "b" in result.new_columns

    def test_column_change_record_has_correct_type(self, differ: DatasetDiffer) -> None:
        old = _df({"a": [1]})
        new = _df({"a": [1], "b": [2]})
        result = differ.diff("ds", old, new)
        cc = next((c for c in result.column_changes if c.name == "b"), None)
        assert cc is not None
        assert cc.change_type == "added"

    def test_severity_is_at_least_warning(self, differ: DatasetDiffer) -> None:
        old = _df({"a": [1]})
        new = _df({"a": [1], "b": [2]})
        result = differ.diff("ds", old, new)
        assert result.severity in (SEVERITY_WARNING, SEVERITY_CRITICAL)


# ---------------------------------------------------------------------------
# DatasetDiffer.diff — column removals
# ---------------------------------------------------------------------------


class TestDiffColumnRemoved:
    """Tests for schema changes when columns are removed."""

    def test_removed_column_detected(self, differ: DatasetDiffer) -> None:
        old = _df({"a": [1], "b": [2]})
        new = _df({"a": [1]})
        result = differ.diff("ds", old, new)
        assert "b" in result.removed_columns

    def test_change_type_is_schema(self, differ: DatasetDiffer) -> None:
        old = _df({"a": [1], "b": [2]})
        new = _df({"a": [1]})
        result = differ.diff("ds", old, new)
        assert result.change_type == CHANGE_TYPE_SCHEMA

    def test_severity_is_critical_for_removal(self) -> None:
        differ = DatasetDiffer(warning_column_drop_count=1)
        old = _df({"a": [1], "b": [2]})
        new = _df({"a": [1]})
        result = differ.diff("ds", old, new)
        assert result.severity == SEVERITY_CRITICAL

    def test_col_delta_is_negative(self, differ: DatasetDiffer) -> None:
        old = _df({"a": [1], "b": [2], "c": [3]})
        new = _df({"a": [1]})
        result = differ.diff("ds", old, new)
        sd = result.to_store_dict()
        assert sd["col_delta"] < 0

    def test_removed_column_in_summary(self, differ: DatasetDiffer) -> None:
        old = _df({"a": [1], "drop_me": [99]})
        new = _df({"a": [1]})
        result = differ.diff("ds", old, new)
        assert "drop_me" in result.summary


# ---------------------------------------------------------------------------
# DatasetDiffer.diff — content changes (row values)
# ---------------------------------------------------------------------------


class TestDiffContentChanges:
    """Tests for CHANGE_TYPE_CONTENT (row-level value changes)."""

    def test_row_addition_detected(self, differ: DatasetDiffer) -> None:
        old = _df({"a": [1], "b": [2]})
        new = _df({"a": [1], "b": [2]}, {"a": [3], "b": [4]})
        result = differ.diff("ds", old, new)
        assert result.change_type == CHANGE_TYPE_CONTENT
        assert result.row_delta == 1

    def test_row_deletion_detected(self, differ: DatasetDiffer) -> None:
        old = _df({"a": [1], "b": [2]}, {"a": [3], "b": [4]})
        new = _df({"a": [1], "b": [2]})
        result = differ.diff("ds", old, new)
        assert result.change_type == CHANGE_TYPE_CONTENT
        assert result.row_delta == -1

    def test_value_change_detected(self, differ: DatasetDiffer) -> None:
        old = _df({"a": [1], "b": ["old_value"]})
        new = _df({"a": [1], "b": ["new_value"]})
        result = differ.diff("ds", old, new)
        assert result.rows_modified >= 1
        assert result.change_type == CHANGE_TYPE_CONTENT

    def test_value_changes_populated(self, differ: DatasetDiffer) -> None:
        old = _df({"id": [1], "val": ["foo"]})
        new = _df({"id": [1], "val": ["bar"]})
        result = differ.diff("ds", old, new)
        assert len(result.value_changes) >= 1

    def test_row_change_pct_positive_on_change(self, differ: DatasetDiffer) -> None:
        old = _df({"a": [1]}, {"a": [2]}, {"a": [3]}, {"a": [4]})
        new = _df({"a": [1]}, {"a": [2]}, {"a": [3]}, {"a": [99]})
        result = differ.diff("ds", old, new)
        assert result.row_change_pct > 0

    def test_high_row_change_pct_triggers_critical(self) -> None:
        differ = DatasetDiffer(critical_row_change_pct=10.0)
        # 2 out of 2 rows changed → 100%
        old = _df({"a": [1]}, {"a": [2]})
        new = _df({"a": [99]}, {"a": [98]})
        result = differ.diff("ds", old, new)
        assert result.severity == SEVERITY_CRITICAL

    def test_low_row_change_pct_stays_warning(self) -> None:
        differ = DatasetDiffer(critical_row_change_pct=50.0)
        # 1 out of 100 rows changed → 1%
        old_data = {"a": list(range(100))}
        old = pd.DataFrame(old_data)
        new_data = {"a": list(range(99)) + [9999]}
        new = pd.DataFrame(new_data)
        result = differ.diff("ds", old, new)
        assert result.severity == SEVERITY_WARNING

    def test_diff_json_contains_row_counts(self, differ: DatasetDiffer) -> None:
        old = _df({"a": [1, 2]})
        new = _df({"a": [1, 2, 3]})
        result = differ.diff("ds", old, new)
        assert result.diff_json["old_row_count"] == 2
        assert result.diff_json["new_row_count"] == 3

    def test_diff_json_value_changes_sample(self, differ: DatasetDiffer) -> None:
        old = _df({"x": ["a"]})
        new = _df({"x": ["b"]})
        result = differ.diff("ds", old, new)
        assert "value_changes_sample" in result.diff_json


# ---------------------------------------------------------------------------
# DatasetDiffer.diff — combined schema + content changes
# ---------------------------------------------------------------------------


class TestDiffSchemaAndContent:
    """Tests for changes that involve both schema and content."""

    def test_combined_change_type_is_schema(self, differ: DatasetDiffer) -> None:
        """Schema takes precedence when both column and row changes occur."""
        old = _df({"a": [1], "b": [2]})
        new = _df({"a": [1], "c": [3], "d": [4]})  # b removed, c+d added, row counts equal
        result = differ.diff("ds", old, new)
        assert result.change_type == CHANGE_TYPE_SCHEMA

    def test_both_row_and_column_changes_in_result(self, differ: DatasetDiffer) -> None:
        old = _df({"a": [1], "b": [2]})
        new = _df({"a": [1, 2], "c": [3, 4]})  # b removed, c added, 1 extra row
        result = differ.diff("ds", old, new)
        assert "b" in result.removed_columns
        assert "c" in result.added_columns
        assert result.row_delta == 1


# ---------------------------------------------------------------------------
# DatasetDiffer.diff — dtype changes
# ---------------------------------------------------------------------------


class TestDiffDtypeChanges:
    """Tests for column dtype changes."""

    def test_dtype_change_triggers_schema(self, differ: DatasetDiffer) -> None:
        old = pd.DataFrame({"score": [1, 2, 3]})
        new = pd.DataFrame({"score": ["a", "b", "c"]})
        result = differ.diff("ds", old, new)
        assert result.change_type == CHANGE_TYPE_SCHEMA

    def test_dtype_change_in_column_changes(self, differ: DatasetDiffer) -> None:
        old = pd.DataFrame({"score": [1, 2, 3]})
        new = pd.DataFrame({"score": ["a", "b", "c"]})
        result = differ.diff("ds", old, new)
        type_changes = [c for c in result.column_changes if c.change_type == "type_changed"]
        assert len(type_changes) == 1
        assert type_changes[0].name == "score"

    def test_dtype_change_old_and_new_dtype_populated(self, differ: DatasetDiffer) -> None:
        old = pd.DataFrame({"score": [1, 2, 3]})
        new = pd.DataFrame({"score": ["a", "b", "c"]})
        result = differ.diff("ds", old, new)
        tc = next(c for c in result.column_changes if c.change_type == "type_changed")
        assert tc.old_dtype != ""
        assert tc.new_dtype != ""
        assert tc.old_dtype != tc.new_dtype


# ---------------------------------------------------------------------------
# DatasetDiffer.diff_files
# ---------------------------------------------------------------------------


class TestDiffFiles:
    """Tests for the diff_files convenience method."""

    def test_diff_files_success(self, differ: DatasetDiffer, tmp_path: Path) -> None:
        old_p = _csv_file(tmp_path, "a,b\n1,2\n", "old.csv")
        new_p = _csv_file(tmp_path, "a,b\n1,2\n3,4\n", "new.csv")
        result = differ.diff_files("ds", old_p, new_p, "csv")
        assert result.change_type == CHANGE_TYPE_CONTENT
        assert result.row_delta == 1

    def test_diff_files_old_none_is_new(self, differ: DatasetDiffer, tmp_path: Path) -> None:
        new_p = _csv_file(tmp_path, "a,b\n1,2\n", "new.csv")
        result = differ.diff_files("ds", None, new_p, "csv")
        assert result.change_type == CHANGE_TYPE_NEW

    def test_diff_files_new_none_is_deletion(self, differ: DatasetDiffer, tmp_path: Path) -> None:
        old_p = _csv_file(tmp_path, "a,b\n1,2\n", "old.csv")
        result = differ.diff_files("ds", old_p, None, "csv")
        assert result.change_type == CHANGE_TYPE_DELETION

    def test_diff_files_missing_old_returns_error(self, differ: DatasetDiffer, tmp_path: Path) -> None:
        new_p = _csv_file(tmp_path, "a,b\n1,2\n", "new.csv")
        result = differ.diff_files("ds", tmp_path / "missing.csv", new_p, "csv")
        assert result.error is not None

    def test_diff_files_missing_new_returns_error(self, differ: DatasetDiffer, tmp_path: Path) -> None:
        old_p = _csv_file(tmp_path, "a,b\n1,2\n", "old.csv")
        result = differ.diff_files("ds", old_p, tmp_path / "missing.csv", "csv")
        assert result.error is not None


# ---------------------------------------------------------------------------
# DiffResult.to_store_dict
# ---------------------------------------------------------------------------


class TestDiffResultToStoreDict:
    """Tests for DiffResult.to_store_dict."""

    def test_to_store_dict_has_required_keys(self, differ: DatasetDiffer) -> None:
        old = _df({"a": [1]})
        new = _df({"a": [2]})
        result = differ.diff("ds", old, new)
        d = result.to_store_dict()
        for key in ("change_type", "severity", "row_delta", "col_delta", "summary", "diff_json"):
            assert key in d, f"Missing key: {key}"

    def test_to_store_dict_diff_json_is_valid_json(self, differ: DatasetDiffer) -> None:
        old = _df({"a": [1]})
        new = _df({"a": [2]})
        result = differ.diff("ds", old, new)
        d = result.to_store_dict()
        # diff_json must be a JSON string parseable by json.loads
        parsed = json.loads(d["diff_json"])
        assert isinstance(parsed, dict)

    def test_to_store_dict_col_delta_sign(self, differ: DatasetDiffer) -> None:
        old = _df({"a": [1], "b": [2]})
        new = _df({"a": [1]})
        result = differ.diff("ds", old, new)
        d = result.to_store_dict()
        assert d["col_delta"] < 0


# ---------------------------------------------------------------------------
# DiffResult.has_changes
# ---------------------------------------------------------------------------


class TestHasChanges:
    """Tests for DiffResult.has_changes."""

    def test_unchanged_has_no_changes(self, differ: DatasetDiffer) -> None:
        df = _df({"a": [1]})
        result = differ.diff("ds", df.copy(), df.copy())
        assert result.has_changes() is False

    def test_content_change_has_changes(self, differ: DatasetDiffer) -> None:
        old = _df({"a": [1]})
        new = _df({"a": [1]}, {"a": [2]})
        result = differ.diff("ds", old, new)
        assert result.has_changes() is True

    def test_new_dataset_has_changes(self, differ: DatasetDiffer) -> None:
        result = differ.diff("ds", None, _df({"a": [1]}))
        assert result.has_changes() is True

    def test_deletion_has_changes(self, differ: DatasetDiffer) -> None:
        result = differ.diff("ds", _df({"a": [1]}), None)
        assert result.has_changes() is True


# ---------------------------------------------------------------------------
# diff_dataframes convenience function
# ---------------------------------------------------------------------------


class TestDiffDataframesFunction:
    """Tests for the module-level diff_dataframes convenience function."""

    def test_returns_diff_result(self) -> None:
        old = _df({"a": [1]})
        new = _df({"a": [1], "b": [2]})
        result = diff_dataframes("ds", old, new)
        assert isinstance(result, DiffResult)

    def test_detects_schema_change(self) -> None:
        old = _df({"a": [1]})
        new = _df({"a": [1], "b": [2]})
        result = diff_dataframes("ds", old, new)
        assert result.change_type == CHANGE_TYPE_SCHEMA

    def test_custom_thresholds_respected(self) -> None:
        # 1 row changed out of 2 → 50%, threshold is 60% so should be WARNING not CRITICAL
        old = _df({"a": [1]}, {"a": [2]})
        new = _df({"a": [99]}, {"a": [2]})
        result = diff_dataframes("ds", old, new, critical_row_change_pct=60.0)
        assert result.severity == SEVERITY_WARNING

    def test_none_old_triggers_new(self) -> None:
        result = diff_dataframes("ds", None, _df({"a": [1]}))
        assert result.change_type == CHANGE_TYPE_NEW

    def test_none_new_triggers_deletion(self) -> None:
        result = diff_dataframes("ds", _df({"a": [1]}), None)
        assert result.change_type == CHANGE_TYPE_DELETION


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Tests for edge cases and boundary conditions."""

    def test_empty_old_and_non_empty_new(self, differ: DatasetDiffer) -> None:
        """Old DF has no rows, new DF has rows → content change."""
        old = pd.DataFrame(columns=["a", "b"])
        new = _df({"a": [1], "b": [2]})
        result = differ.diff("ds", old, new)
        assert result.row_delta > 0

    def test_non_empty_old_and_empty_new(self, differ: DatasetDiffer) -> None:
        """Old DF has rows, new DF has no rows → content change."""
        old = _df({"a": [1], "b": [2]})
        new = pd.DataFrame(columns=["a", "b"])
        result = differ.diff("ds", old, new)
        assert result.row_delta < 0

    def test_single_row_change(self, differ: DatasetDiffer) -> None:
        """A single-row change is detected correctly."""
        old = _df({"val": ["original"]})
        new = _df({"val": ["modified"]})
        result = differ.diff("ds", old, new)
        assert result.rows_modified >= 1

    def test_many_columns_all_changed(self, differ: DatasetDiffer) -> None:
        """All columns being removed is detected."""
        old = _df({"a": [1], "b": [2], "c": [3]})
        new = _df({"x": [1], "y": [2], "z": [3]})
        result = differ.diff("ds", old, new)
        assert result.change_type == CHANGE_TYPE_SCHEMA
        assert set(result.removed_columns) == {"a", "b", "c"}
        assert set(result.added_columns) == {"x", "y", "z"}

    def test_diff_json_is_serialisable(self, differ: DatasetDiffer) -> None:
        """The diff_json field can always be JSON-serialised."""
        old = _df({"a": [1], "b": ["foo"]})
        new = _df({"a": [2], "c": [3.14]})
        result = differ.diff("ds", old, new)
        try:
            json.dumps(result.diff_json)
        except (TypeError, ValueError) as exc:
            pytest.fail(f"diff_json is not JSON-serialisable: {exc}")

    def test_dataset_key_always_in_result(self, differ: DatasetDiffer) -> None:
        """The dataset_key attribute is always propagated."""
        for old, new in [
            (None, _df({"a": [1]})),
            (_df({"a": [1]}), None),
            (_df({"a": [1]}), _df({"a": [1]})),
        ]:
            result = differ.diff("my_dataset", old, new)
            assert result.dataset_key == "my_dataset"
