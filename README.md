# Health Data Watchdog

A CLI tool that periodically fetches key public health surveillance datasets from sources like the **CDC** and **WHO**, then diffs them against previously cached snapshots to detect changes, deletions, or schema alterations. It generates human-readable changelogs, stores a local history of dataset states, and can post alerts to Slack or send email notifications — giving researchers and public health advocates an early warning system when critical pandemic data may have been quietly removed or modified.

---

## Features

- **Dataset Registry** — Pre-configured list of CDC and WHO dataset URLs with metadata (source, format, description).
- **SHA-256 Content Hashing** — Detects any modification to a dataset by comparing content hashes between fetches.
- **Structural Diff Engine** — Detects row-level changes, column additions/removals, data type shifts, and complete dataset disappearance using `deepdiff` and `pandas`.
- **Human-Readable Changelogs** — Rich terminal output with tables and exportable Markdown diff reports with timestamped history.
- **Alerting** — Slack webhook and SMTP email notifications with configurable severity thresholds.
- **SQLite Snapshot History** — Local audit trail of all dataset states, enabling point-in-time rollback comparisons.

---

## Requirements

- Python 3.10 or newer
- pip

---

## Installation

### From source

```bash
git clone https://github.com/example/health-data-watchdog.git
cd health-data-watchdog
pip install -e .
```

### From PyPI (when published)

```bash
pip install health-data-watchdog
```

---

## Quick Start

### 1. Generate a default configuration file

```bash
health-watchdog configure --init
```

This creates `~/.config/health_watchdog/config.toml` with sensible defaults.

### 2. Fetch all registered datasets

```bash
health-watchdog fetch
```

Fetch a specific dataset by its key:

```bash
health-watchdog fetch --dataset cdc_covid_cases
```

### 3. Diff current data against the previous snapshot

```bash
health-watchdog diff
```

Diff a specific dataset:

```bash
health-watchdog diff --dataset cdc_covid_cases
```

Output a Markdown report to a file:

```bash
health-watchdog diff --output report.md
```

### 4. Generate a changelog report

```bash
health-watchdog report
```

Limit to the last N entries:

```bash
health-watchdog report --limit 10
```

### 5. Start the periodic watcher

```bash
health-watchdog watch
```

The watcher fetches datasets on the configured schedule (default: every 6 hours), diffs them, and sends alerts if changes are detected.

---

## Configuration

The configuration file is a TOML file located at `~/.config/health_watchdog/config.toml` by default. You can override the path with the `--config` option or the `HEALTH_WATCHDOG_CONFIG` environment variable.

### Example `config.toml`

```toml
[general]
data_dir = "~/.local/share/health_watchdog/snapshots"
db_path  = "~/.local/share/health_watchdog/watchdog.db"
log_level = "INFO"

[schedule]
interval_hours = 6

[thresholds]
# Minimum percentage of rows changed to trigger a CRITICAL alert
critical_row_change_pct = 10.0
# Minimum number of columns dropped to trigger a WARNING alert
warning_column_drop_count = 1

[slack]
enabled = false
webhook_url = ""

[email]
enabled = false
smtp_host = "smtp.example.com"
smtp_port = 587
smtp_user = ""
smtp_password = ""
from_address = "watchdog@example.com"
to_addresses = []
use_tls = true
```

---

## Dataset Sources

The tool ships with a built-in registry of well-known public health datasets:

| Key | Source | Description |
|-----|--------|-------------|
| `cdc_covid_cases` | CDC | COVID-19 case surveillance public use data |
| `cdc_flu_surveillance` | CDC | Weekly influenza surveillance report data |
| `cdc_vaccination_trends` | CDC | COVID-19 vaccination trends by jurisdiction |
| `who_covid_global` | WHO | WHO COVID-19 global daily cases and deaths |
| `who_disease_outbreaks` | WHO | WHO disease outbreak news feed (JSON) |

Custom datasets can be added to the configuration file:

```toml
[[datasets]]
key         = "my_custom_dataset"
url         = "https://example.com/data.csv"
format      = "csv"
source      = "Custom"
description = "My custom health dataset"
enabled     = true
```

---

## Development

### Setup

```bash
pip install -e ".[dev]"
```

### Run tests

```bash
pytest
```

### Run tests with coverage

```bash
pytest --cov=health_data_watchdog --cov-report=term-missing
```

---

## Project Structure

```
health-data-watchdog/
├── pyproject.toml
├── README.md
├── health_data_watchdog/
│   ├── __init__.py       # Package init, version
│   ├── cli.py            # Click CLI entry point
│   ├── config.py         # TOML config loader/validator
│   ├── datasets.py       # Registry of known dataset URLs
│   ├── fetcher.py        # HTTP downloader + hashing
│   ├── differ.py         # Structural diff engine
│   ├── store.py          # SQLite snapshot store
│   ├── reporter.py       # Terminal + Markdown changelog renderer
│   └── alerting.py       # Slack + SMTP alerting
└── tests/
    ├── __init__.py
    ├── test_differ.py
    ├── test_fetcher.py
    ├── test_store.py
    └── test_alerting.py
```

---

## License

MIT License. See [LICENSE](LICENSE) for details.
