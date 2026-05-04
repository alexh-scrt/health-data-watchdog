"""Registry of known public health surveillance datasets.

This module defines a static registry of well-known CDC and WHO dataset
URLs with associated metadata (source organisation, file format,
description, and enabled flag).  The registry is used by the fetcher
and CLI to discover datasets without any configuration.

Custom datasets defined in the user's config file are merged with this
built-in registry at runtime via :func:`get_all_datasets`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


# ---------------------------------------------------------------------------
# Dataset metadata dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DatasetEntry:
    """Metadata for a single public health dataset.

    Attributes:
        key: Unique machine-readable identifier (e.g. ``"cdc_covid_cases"``).
        url: Full URL from which the dataset can be fetched.
        source: Human-readable name of the data provider (e.g. ``"CDC"``).
        format: Expected file format; one of ``csv``, ``json``, ``tsv``,
                ``xlsx``, or ``parquet``.
        description: Short human-readable description of the dataset.
        enabled: Whether the dataset is fetched by default.  Datasets that
                 are known to have unreliable URLs can be disabled here.
        extra_headers: Optional HTTP headers required to access the resource
                       (e.g. ``Accept`` headers for content negotiation).
    """

    key: str
    url: str
    source: str
    format: str
    description: str
    enabled: bool = True
    extra_headers: Dict[str, str] = field(default_factory=dict, compare=False, hash=False)


# ---------------------------------------------------------------------------
# Built-in dataset registry
# ---------------------------------------------------------------------------

#: Ordered list of built-in dataset entries.
BUILTIN_DATASETS: List[DatasetEntry] = [
    # ------------------------------------------------------------------
    # CDC datasets
    # ------------------------------------------------------------------
    DatasetEntry(
        key="cdc_covid_cases",
        url=(
            "https://data.cdc.gov/api/views/vbim-akqf/rows.csv?accessType=DOWNLOAD"
        ),
        source="CDC",
        format="csv",
        description=(
            "COVID-19 case surveillance public use data. Contains de-identified "
            "individual-level records of COVID-19 cases reported to CDC."
        ),
        enabled=True,
    ),
    DatasetEntry(
        key="cdc_flu_surveillance",
        url=(
            "https://data.cdc.gov/api/views/kvib-3txy/rows.csv?accessType=DOWNLOAD"
        ),
        source="CDC",
        format="csv",
        description=(
            "Weekly U.S. influenza surveillance data including ILINet "
            "(Influenza-Like Illness surveillance network) statistics."
        ),
        enabled=True,
    ),
    DatasetEntry(
        key="cdc_vaccination_trends",
        url=(
            "https://data.cdc.gov/api/views/rh2h-3yt2/rows.csv?accessType=DOWNLOAD"
        ),
        source="CDC",
        format="csv",
        description=(
            "COVID-19 vaccination trends in the United States by jurisdiction, "
            "including daily and cumulative doses administered."
        ),
        enabled=True,
    ),
    DatasetEntry(
        key="cdc_excess_mortality",
        url=(
            "https://data.cdc.gov/api/views/xkkf-xrst/rows.csv?accessType=DOWNLOAD"
        ),
        source="CDC",
        format="csv",
        description=(
            "Excess deaths associated with COVID-19, by week, state, and cause group. "
            "Compares observed deaths to statistically expected counts."
        ),
        enabled=True,
    ),
    DatasetEntry(
        key="cdc_hospitalization_trends",
        url=(
            "https://data.cdc.gov/api/views/39z2-9zu6/rows.csv?accessType=DOWNLOAD"
        ),
        source="CDC",
        format="csv",
        description=(
            "COVID-NET: COVID-19-Associated Hospitalization Surveillance Network data "
            "tracking laboratory-confirmed COVID-19 hospitalizations."
        ),
        enabled=True,
    ),
    DatasetEntry(
        key="cdc_death_counts_weekly",
        url=(
            "https://data.cdc.gov/api/views/muzy-jte6/rows.csv?accessType=DOWNLOAD"
        ),
        source="CDC",
        format="csv",
        description=(
            "Weekly counts of deaths by state and select causes, allowing "
            "monitoring of mortality trends beyond COVID-19."
        ),
        enabled=True,
    ),
    DatasetEntry(
        key="cdc_wastewater_surveillance",
        url=(
            "https://data.cdc.gov/api/views/2ew6-ywp6/rows.csv?accessType=DOWNLOAD"
        ),
        source="CDC",
        format="csv",
        description=(
            "National Wastewater Surveillance System (NWSS) data tracking "
            "SARS-CoV-2 levels in wastewater as an early-warning indicator."
        ),
        enabled=True,
    ),
    # ------------------------------------------------------------------
    # WHO datasets
    # ------------------------------------------------------------------
    DatasetEntry(
        key="who_covid_global",
        url="https://covid19.who.int/WHO-COVID-19-global-data.csv",
        source="WHO",
        format="csv",
        description=(
            "WHO COVID-19 global daily reported cases and deaths, updated daily "
            "for all countries and territories."
        ),
        enabled=True,
    ),
    DatasetEntry(
        key="who_covid_vaccination",
        url="https://covid19.who.int/who-data/vaccination-data.csv",
        source="WHO",
        format="csv",
        description=(
            "WHO COVID-19 vaccination data by country: total doses administered, "
            "persons vaccinated with at least one dose, and fully vaccinated persons."
        ),
        enabled=True,
    ),
    DatasetEntry(
        key="who_disease_outbreaks",
        url="https://www.who.int/api/news/diseaseoutbreaksnews",
        source="WHO",
        format="json",
        description=(
            "WHO Disease Outbreak News (DON) API feed. Lists current and recent "
            "international disease outbreak events reported by WHO member states."
        ),
        enabled=True,
        extra_headers={"Accept": "application/json"},
    ),
    DatasetEntry(
        key="who_mortality_database",
        url=(
            "https://cdn.who.int/media/docs/default-source/world-health-statistics/"
            "2023/annexes/annex-1-sdgs.xlsx"
        ),
        source="WHO",
        format="xlsx",
        description=(
            "WHO World Health Statistics 2023 — Annex 1: SDG health-related targets "
            "and indicators. Covers mortality, morbidity, and risk factor data."
        ),
        enabled=False,  # Disabled by default: large binary file, infrequent updates.
    ),
]


# ---------------------------------------------------------------------------
# Internal index built once at import time
# ---------------------------------------------------------------------------

_BUILTIN_INDEX: Dict[str, DatasetEntry] = {ds.key: ds for ds in BUILTIN_DATASETS}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_builtin_datasets() -> List[DatasetEntry]:
    """Return a copy of the built-in dataset registry.

    Returns:
        List of all :class:`DatasetEntry` objects in the built-in registry,
        in definition order.
    """
    return list(BUILTIN_DATASETS)


def get_dataset_by_key(key: str) -> Optional[DatasetEntry]:
    """Look up a built-in dataset entry by its unique key.

    Args:
        key: The machine-readable dataset key (e.g. ``"cdc_covid_cases"``).

    Returns:
        The matching :class:`DatasetEntry`, or ``None`` if not found.
    """
    return _BUILTIN_INDEX.get(key)


def get_enabled_datasets() -> List[DatasetEntry]:
    """Return only the built-in datasets that are enabled by default.

    Returns:
        Filtered list of enabled :class:`DatasetEntry` objects.
    """
    return [ds for ds in BUILTIN_DATASETS if ds.enabled]


def get_all_datasets(
    custom_entries: Optional[List["CustomDatasetEntry"]] = None,
) -> List[DatasetEntry]:
    """Return the combined dataset registry: built-in entries plus any custom ones.

    Custom entries that share a key with a built-in dataset will *override*
    the built-in definition, allowing users to customise URLs or toggle
    the enabled flag without editing package code.

    Args:
        custom_entries: Optional list of custom dataset definitions sourced
                        from the user's config file.  Each item must expose
                        the same attributes as :class:`DatasetEntry`
                        (``key``, ``url``, ``source``, ``format``,
                        ``description``, ``enabled``).

    Returns:
        Merged list of :class:`DatasetEntry` objects.  Built-in entries
        appear first, followed by any purely custom (non-overriding) entries.
    """
    # Start with built-in entries keyed for easy override lookup.
    merged: Dict[str, DatasetEntry] = dict(_BUILTIN_INDEX)

    if custom_entries:
        for custom in custom_entries:
            merged[custom.key] = DatasetEntry(
                key=custom.key,
                url=custom.url,
                source=custom.source,
                format=custom.format,
                description=custom.description,
                enabled=custom.enabled,
            )

    # Preserve a stable order: built-ins first (original order), then new custom keys.
    ordered_keys: List[str] = [
        ds.key for ds in BUILTIN_DATASETS if ds.key in merged
    ]
    if custom_entries:
        for custom in custom_entries:
            if custom.key not in _BUILTIN_INDEX:
                ordered_keys.append(custom.key)

    return [merged[k] for k in ordered_keys]


def list_datasets_summary() -> List[Dict[str, str]]:
    """Return a lightweight summary list suitable for display in the CLI.

    Each element is a plain dict with keys ``key``, ``source``,
    ``format``, ``enabled``, and ``description`` — all string values.

    Returns:
        List of summary dicts, one per built-in dataset.
    """
    return [
        {
            "key": ds.key,
            "source": ds.source,
            "format": ds.format,
            "enabled": "yes" if ds.enabled else "no",
            "description": ds.description,
        }
        for ds in BUILTIN_DATASETS
    ]


# Convenience type alias for callers that import from config.py
try:
    from health_data_watchdog.config import CustomDataset as CustomDatasetEntry  # noqa: F401
except Exception:  # pragma: no cover — avoids circular import issues during early bootstrap
    CustomDatasetEntry = object  # type: ignore[assignment,misc]
