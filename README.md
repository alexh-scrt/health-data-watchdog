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

Preview what would be fetched without downloading:

```bash
health-watchdog fetch --dry-run
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

Diff and send alerts for detected changes:

```bash
health-watchdog diff --alert
```

### 4. Generate a changelog report

```bash
health-watchdog report
```

Limit to the last N entries:

```bash
health-watchdog report --limit 10
```

Filter by severity:

```bash
health-watchdog report --severity critical
```

View raw snapshot fetch history:

```bash
health-watchdog report --snapshots
```

Write the report to a Markdown file:

```bash
health-watchdog report --output history.md
```

### 5. Start the periodic watcher

```bash
health-watchdog watch
```

Run a single fetch+diff cycle and exit (useful for cron):

```bash
health-watchdog watch --once
```

Override the schedule interval:

```bash
health-watchdog watch --interval 2
```

The watcher fetches datasets on the configured schedule (default: every 6 hours), diffs them, and sends alerts if changes are detected when `--alert` is used.

### 6. List available datasets

```bash
health-watchdog datasets
```

Show all datasets including disabled ones:

```bash
health-watchdog datasets --all
```

Inspect a specific dataset:

```bash
health-watchdog datasets --key cdc_covid_cases
```

### 7. Inspect the configuration

```bash
health-watchdog configure --show
```

Print the resolved config file path:

```bash
health-watchdog configure --path
```

---

## Configuration

The configuration file is a TOML file located at `~/.config/health_watchdog/config.toml` by default. You can override the path with the `--config` option or the `HEALTH_WATCHDOG_CONFIG` environment variable.

```bash
export HEALTH_WATCHDOG_CONFIG=/etc/health_watchdog/config.toml
health-watchdog fetch
```

### Example `config.toml`

```toml
[general]
data_dir  = "~/.local/share/health_watchdog/snapshots"
db_path   = "~/.local/share/health_watchdog/watchdog.db"
log_level = "INFO"

[schedule]
interval_hours = 6

[thresholds]
# Minimum percentage of rows changed to trigger a CRITICAL alert
critical_row_change_pct = 10.0
# Minimum number of columns dropped to trigger a CRITICAL alert
warning_column_drop_count = 1

[slack]
enabled     = false
webhook_url = ""

[email]
enabled      = false
smtp_host    = "smtp.example.com"
smtp_port    = 587
smtp_user    = ""
smtp_password = ""
from_address = "watchdog@example.com"
to_addresses = []
use_tls      = true
```

---

## Slack Alerting

To enable Slack notifications:

1. Create an [Incoming Webhook](https://api.slack.com/messaging/webhooks) in your Slack workspace.
2. Set the URL in your config file:

```toml
[slack]
enabled     = true
webhook_url = "https://hooks.slack.com/services/T.../B.../..."
```

Alerts are sent when the severity of a detected change meets or exceeds `warning` by default.

---

## Email Alerting

To enable email notifications via SMTP:

```toml
[email]
enabled       = true
smtp_host     = "smtp.gmail.com"
smtp_port     = 587
smtp_user     = "your-account@gmail.com"
smtp_password = "your-app-password"
from_address  = "watchdog@example.com"
to_addresses  = ["researcher@example.com", "advocate@example.org"]
use_tls       = true
```

> **Tip for Gmail:** Use an [App Password](https://support.google.com/accounts/answer/185833) rather than your account password.

---

## Dataset Sources

The tool ships with a built-in registry of well-known public health datasets:

| Key | Source | Format | Description |
|-----|--------|--------|-------------|
| `cdc_covid_cases` | CDC | CSV | COVID-19 case surveillance public use data |
| `cdc_flu_surveillance` | CDC | CSV | Weekly influenza surveillance report data |
| `cdc_vaccination_trends` | CDC | CSV | COVID-19 vaccination trends by jurisdiction |
| `cdc_excess_mortality` | CDC | CSV | Excess deaths associated with COVID-19 |
| `cdc_hospitalization_trends` | CDC | CSV | COVID-NET hospitalization surveillance |
| `cdc_death_counts_weekly` | CDC | CSV | Weekly death counts by state and cause |
| `cdc_wastewater_surveillance` | CDC | CSV | NWSS SARS-CoV-2 wastewater levels |
| `who_covid_global` | WHO | CSV | WHO COVID-19 global daily cases and deaths |
| `who_covid_vaccination` | WHO | CSV | WHO COVID-19 vaccination data by country |
| `who_disease_outbreaks` | WHO | JSON | WHO Disease Outbreak News API feed |
| `who_mortality_database` | WHO | XLSX | WHO World Health Statistics 2023 |

### Adding Custom Datasets

Add custom datasets to your configuration file:

```toml
[[datasets]]
key         = "my_custom_dataset"
url         = "https://example.com/data.csv"
format      = "csv"
source      = "Custom"
description = "My custom health dataset"
enabled     = true
```

Custom entries with the same `key` as a built-in dataset will **override** the built-in definition.

Supported formats: `csv`, `json`, `tsv`, `xlsx`, `parquet`.

---

## CLI Reference

```
Usage: health-watchdog [OPTIONS] COMMAND [ARGS]...

  Health Data Watchdog — Monitor public health datasets for changes.

Options:
  --version              Show version and exit.
  --config PATH          Path to the TOML configuration file.
  --help                 Show this message and exit.

Commands:
  configure  Manage the Health Data Watchdog configuration file.
  datasets   List all registered datasets (built-in and custom).
  diff       Diff the two most recent snapshots for each dataset.
  fetch      Download datasets and save snapshots to disk.
  report     Display the stored change history from the local database.
  watch      Start the periodic watcher for all enabled datasets.
```

### `fetch`

```
Usage: health-watchdog fetch [OPTIONS]

  Download datasets and save snapshots to disk.

Options:
  --dataset KEY        Fetch only this dataset.
  --include-disabled   Also fetch disabled datasets.
  --dry-run            Print what would be fetched without downloading.
  --help               Show this message and exit.
```

### `diff`

```
Usage: health-watchdog diff [OPTIONS]

  Diff the two most recent snapshots for each dataset.

Options:
  --dataset KEY   Diff only this dataset.
  --output FILE   Write a Markdown diff report to FILE.
  --alert         Send alerts for detected changes.
  --help          Show this message and exit.
```

### `watch`

```
Usage: health-watchdog watch [OPTIONS]

  Start the periodic watcher for all enabled datasets.

Options:
  --interval HOURS   Override the schedule interval in hours.
  --alert            Send alerts for detected changes.
  --once             Run a single cycle and exit.
  --help             Show this message and exit.
```

### `report`

```
Usage: health-watchdog report [OPTIONS]

  Display the stored change history from the local database.

Options:
  --dataset KEY                     Limit to this dataset.
  --limit N                         Max records to display.  [default: 50]
  --severity [info|warning|critical] Filter by severity.
  --output FILE                     Write Markdown report to FILE.
  --snapshots                       Show snapshot history instead.
  --help                            Show this message and exit.
```

### `configure`

```
Usage: health-watchdog configure [OPTIONS]

  Manage the Health Data Watchdog configuration file.

Options:
  --init   Write a default configuration file.
  --show   Print the current effective configuration.
  --path   Print the resolved config file path.
  --help   Show this message and exit.
```

### `datasets`

```
Usage: health-watchdog datasets [OPTIONS]

  List all registered datasets (built-in and custom).

Options:
  --all        Show all datasets including disabled ones.
  --key KEY    Show detailed info for a single dataset.
  --help       Show this message and exit.
```

---

## Automation with Cron

Run a single watchdog cycle from cron every 6 hours:

```crontab
0 */6 * * * /usr/local/bin/health-watchdog watch --once --alert >> /var/log/health_watchdog.log 2>&1
```

Or fetch and diff as separate steps:

```crontab
0 */6 * * * /usr/local/bin/health-watchdog fetch >> /var/log/health_watchdog.log 2>&1
5 */6 * * * /usr/local/bin/health-watchdog diff --alert >> /var/log/health_watchdog.log 2>&1
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

## How It Works

1. **Fetch** — `DatasetFetcher` streams each dataset URL to a temporary file, computes its SHA-256 hash, moves it to `<data_dir>/<dataset_key>/<timestamp>.<fmt>`, and records metadata in SQLite.

2. **Diff** — `DatasetDiffer` loads the two most recent non-duplicate snapshots into pandas DataFrames and computes:
   - Row count delta (additions, removals)
   - Column additions and removals
   - Per-column dtype changes
   - Value-level changes (sample of up to 50 modified rows)
   - A severity level (`info` / `warning` / `critical`)

3. **Report** — `Reporter` renders the diff as a Rich terminal table or exports a Markdown changelog.

4. **Alert** — `AlertManager` dispatches Slack webhook messages and/or SMTP emails for any change meeting the configured severity threshold.

5. **Store** — `SnapshotStore` (SQLite) keeps a full audit trail of all fetches and detected changes, enabling `report` queries and historical comparisons.

---

## Severity Thresholds

| Condition | Default Severity |
|-----------|------------------|
| Dataset deleted or first appeared | **CRITICAL** |
| ≥ 1 column removed | **CRITICAL** |
| ≥ 10% of rows changed | **CRITICAL** |
| Column added or dtype changed | **WARNING** |
| Any row-level content change | **WARNING** |
| No changes | INFO |

Thresholds are configurable:

```toml
[thresholds]
critical_row_change_pct   = 5.0   # lower threshold → more CRITICAL alerts
warning_column_drop_count = 1     # columns dropped triggers CRITICAL
```

---

## License

MIT License. See [LICENSE](LICENSE) for details.
