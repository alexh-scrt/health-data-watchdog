"""Tests for the health_data_watchdog.datasets module.

Covers the built-in registry contents, lookup functions, filtering,
and merging with custom dataset entries.
"""

from __future__ import annotations

from typing import List

import pytest

from health_data_watchdog.config import CustomDataset
from health_data_watchdog.datasets import (
    BUILTIN_DATASETS,
    DatasetEntry,
    get_all_datasets,
    get_builtin_datasets,
    get_dataset_by_key,
    get_enabled_datasets,
    list_datasets_summary,
)


# ---------------------------------------------------------------------------
# BUILTIN_DATASETS sanity checks
# ---------------------------------------------------------------------------


def test_builtin_datasets_not_empty() -> None:
    """There must be at least one built-in dataset."""
    assert len(BUILTIN_DATASETS) > 0


def test_all_builtin_keys_are_unique() -> None:
    """Every built-in dataset must have a unique key."""
    keys = [ds.key for ds in BUILTIN_DATASETS]
    assert len(keys) == len(set(keys)), "Duplicate keys found in BUILTIN_DATASETS"


def test_all_builtin_datasets_have_required_fields() -> None:
    """Every built-in DatasetEntry must have non-empty required string fields."""
    for ds in BUILTIN_DATASETS:
        assert ds.key.strip(), f"Empty key for dataset: {ds!r}"
        assert ds.url.strip(), f"Empty url for dataset key '{ds.key}'"
        assert ds.source.strip(), f"Empty source for dataset key '{ds.key}'"
        assert ds.format.strip(), f"Empty format for dataset key '{ds.key}'"
        assert ds.description.strip(), f"Empty description for dataset key '{ds.key}'"


def test_all_builtin_formats_are_known() -> None:
    """Every built-in dataset format must be from the accepted set."""
    known = {"csv", "json", "tsv", "xlsx", "parquet"}
    for ds in BUILTIN_DATASETS:
        assert ds.format in known, f"Unknown format '{ds.format}' for dataset '{ds.key}'"


def test_all_builtin_urls_start_with_https() -> None:
    """All built-in URLs should use HTTPS."""
    for ds in BUILTIN_DATASETS:
        assert ds.url.startswith("https://"), (
            f"Dataset '{ds.key}' URL does not use HTTPS: {ds.url}"
        )


# ---------------------------------------------------------------------------
# Expected well-known datasets are present
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "cdc_covid_cases",
        "cdc_flu_surveillance",
        "cdc_vaccination_trends",
        "who_covid_global",
        "who_disease_outbreaks",
    ],
)
def test_known_dataset_key_exists(key: str) -> None:
    """Key well-known dataset entries must exist in the built-in registry."""
    keys = [ds.key for ds in BUILTIN_DATASETS]
    assert key in keys, f"Expected built-in dataset key '{key}' not found"


# ---------------------------------------------------------------------------
# get_builtin_datasets
# ---------------------------------------------------------------------------


def test_get_builtin_datasets_returns_list() -> None:
    """get_builtin_datasets returns a list."""
    result = get_builtin_datasets()
    assert isinstance(result, list)
    assert all(isinstance(ds, DatasetEntry) for ds in result)


def test_get_builtin_datasets_is_copy() -> None:
    """Mutating the returned list does not affect BUILTIN_DATASETS."""
    original_len = len(BUILTIN_DATASETS)
    result = get_builtin_datasets()
    result.clear()
    assert len(BUILTIN_DATASETS) == original_len


# ---------------------------------------------------------------------------
# get_dataset_by_key
# ---------------------------------------------------------------------------


def test_get_dataset_by_key_found() -> None:
    """get_dataset_by_key returns the correct DatasetEntry for a known key."""
    ds = get_dataset_by_key("cdc_covid_cases")
    assert ds is not None
    assert ds.key == "cdc_covid_cases"
    assert ds.source == "CDC"
    assert ds.format == "csv"


def test_get_dataset_by_key_not_found() -> None:
    """get_dataset_by_key returns None for an unknown key."""
    result = get_dataset_by_key("this_key_does_not_exist_xyz")
    assert result is None


# ---------------------------------------------------------------------------
# get_enabled_datasets
# ---------------------------------------------------------------------------


def test_get_enabled_datasets_only_enabled() -> None:
    """get_enabled_datasets returns only entries with enabled=True."""
    enabled = get_enabled_datasets()
    assert all(ds.enabled for ds in enabled)


def test_get_enabled_datasets_not_empty() -> None:
    """At least one built-in dataset must be enabled."""
    assert len(get_enabled_datasets()) > 0


def test_get_enabled_datasets_subset_of_builtin() -> None:
    """Enabled datasets are a subset of all built-in datasets."""
    enabled_keys = {ds.key for ds in get_enabled_datasets()}
    all_keys = {ds.key for ds in BUILTIN_DATASETS}
    assert enabled_keys.issubset(all_keys)


# ---------------------------------------------------------------------------
# get_all_datasets — no custom entries
# ---------------------------------------------------------------------------


def test_get_all_datasets_no_custom_equals_builtin() -> None:
    """get_all_datasets with no custom entries returns the same as get_builtin_datasets."""
    all_ds = get_all_datasets()
    builtin_ds = get_builtin_datasets()
    assert [ds.key for ds in all_ds] == [ds.key for ds in builtin_ds]


# ---------------------------------------------------------------------------
# get_all_datasets — with custom entries
# ---------------------------------------------------------------------------


def _make_custom(
    key: str = "custom_ds",
    url: str = "https://example.com/data.csv",
    fmt: str = "csv",
    source: str = "Custom",
    description: str = "A custom dataset",
    enabled: bool = True,
) -> CustomDataset:
    return CustomDataset(
        key=key,
        url=url,
        format=fmt,
        source=source,
        description=description,
        enabled=enabled,
    )


def test_get_all_datasets_appends_new_custom() -> None:
    """A custom dataset with a new key is appended after built-in entries."""
    custom = _make_custom(key="new_custom")
    result = get_all_datasets([custom])
    keys = [ds.key for ds in result]
    assert "new_custom" in keys
    # Built-in keys should appear before the custom one.
    builtin_keys = [ds.key for ds in BUILTIN_DATASETS]
    last_builtin_idx = max(keys.index(k) for k in builtin_keys if k in keys)
    custom_idx = keys.index("new_custom")
    assert custom_idx > last_builtin_idx


def test_get_all_datasets_custom_overrides_builtin() -> None:
    """A custom dataset with an existing built-in key overrides the built-in definition."""
    custom = _make_custom(
        key="cdc_covid_cases",
        url="https://custom-override.example.com/data.csv",
    )
    result = get_all_datasets([custom])
    entry = next(ds for ds in result if ds.key == "cdc_covid_cases")
    assert entry.url == "https://custom-override.example.com/data.csv"


def test_get_all_datasets_does_not_duplicate_overridden_key() -> None:
    """Overriding a built-in key must not result in duplicate entries."""
    custom = _make_custom(key="cdc_covid_cases")
    result = get_all_datasets([custom])
    keys = [ds.key for ds in result]
    assert keys.count("cdc_covid_cases") == 1


def test_get_all_datasets_multiple_custom() -> None:
    """Multiple custom entries are all included."""
    customs = [
        _make_custom(key="custom_a"),
        _make_custom(key="custom_b"),
    ]
    result = get_all_datasets(customs)
    result_keys = {ds.key for ds in result}
    assert "custom_a" in result_keys
    assert "custom_b" in result_keys


# ---------------------------------------------------------------------------
# list_datasets_summary
# ---------------------------------------------------------------------------


def test_list_datasets_summary_returns_list_of_dicts() -> None:
    """list_datasets_summary returns a list of dicts with expected keys."""
    summary = list_datasets_summary()
    assert isinstance(summary, list)
    assert len(summary) == len(BUILTIN_DATASETS)
    for item in summary:
        for required_key in ("key", "source", "format", "enabled", "description"):
            assert required_key in item, f"Missing key '{required_key}' in summary item"
            assert isinstance(item[required_key], str)


def test_list_datasets_summary_enabled_values() -> None:
    """The 'enabled' field in summary is 'yes' or 'no'."""
    for item in list_datasets_summary():
        assert item["enabled"] in ("yes", "no")


# ---------------------------------------------------------------------------
# DatasetEntry immutability
# ---------------------------------------------------------------------------


def test_dataset_entry_is_frozen() -> None:
    """DatasetEntry instances must be immutable (frozen dataclass)."""
    ds = get_dataset_by_key("cdc_covid_cases")
    assert ds is not None
    with pytest.raises((AttributeError, TypeError)):
        ds.key = "modified"  # type: ignore[misc]
