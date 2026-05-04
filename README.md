# Health Data Watchdog 🔍

> An early warning system for public health data — know the moment CDC or WHO datasets change, disappear, or get quietly modified.

Health Data Watchdog periodically fetches key public health surveillance datasets from the CDC, WHO, and other sources, then diffs them against cached snapshots to detect changes, deletions, or schema alterations. It generates human-readable changelogs, maintains a local audit trail, and fires Slack or email alerts — so researchers and public health advocates are never caught off guard when critical pandemic data shifts.

---

## Quick Start

**Install:**

```bash
pip install health-data-watchdog
```

Or from source:

```bash
git clone https://github.com/your-org/health-data-watchdog
cd health-data-watchdog
pip install -e .
```

**Basic usage:**

```bash
# Fetch all built-in CDC/WHO datasets and store snapshots
health-watchdog fetch

# Diff the two most recent snapshots for every dataset
health-watchdog diff

# Run fetch + diff on a schedule (default: every 6 hours)
health-watchdog watch

# View stored change history
health-watchdog report
```

That's it. After the first two fetches, any changes will be printed to your terminal automatically.

---

## Features

- **Pre-configured Dataset Registry** — Built-in list of CDC and WHO surveillance dataset URLs with metadata; no manual URL hunting required.
- **SHA-256 Content Hashing + Structural Diff** — Detects any byte-level modification, plus row additions/removals, column drops, data type shifts, and full dataset disappearance.
- **Rich Terminal Output & Markdown Reports** — Colour-coded tables and panels in your terminal, with exportable timestamped Markdown changelogs for archiving or sharing.
- **Slack & Email Alerting** — Webhook and SMTP notifications with configurable severity thresholds so you only get paged for changes that matter.
- **SQLite Snapshot History** — Every dataset state is stored locally, enabling point-in-time rollback comparisons and a full audit trail.

---

## Usage Examples

### Fetch specific datasets

```bash
# List all available built-in datasets
health-watchdog datasets

# Fetch only a specific dataset by key
health-watchdog fetch --dataset cdc_covid_cases

# Fetch with a custom config file
health-watchdog --config /path/to/watchdog.toml fetch
```

### Diff and report

```bash
# Diff a specific dataset
health-watchdog diff --dataset who_flu_surveillance

# Export a Markdown diff report to a file
health-watchdog diff --output report.md

# Show the last 10 change events from history
health-watchdog report --limit 10

# Filter history by severity
health-watchdog report --severity high
```

### Run as a background watcher

```bash
# Watch on the default schedule (from config)
health-watchdog watch

# Override the interval (in minutes)
health-watchdog watch --interval 120
```

### Configure the tool

```bash
# Generate a default config file at ~/.config/watchdog.toml
health-watchdog configure --init

# Validate your current config
health-watchdog configure --validate
```

---

## Project Structure

```
health-data-watchdog/
├── pyproject.toml                    # Project metadata, dependencies, CLI entry point
├── README.md
├── health_data_watchdog/
│   ├── __init__.py                   # Package init, version export
│   ├── cli.py                        # Click commands: fetch, diff, watch, report, configure
│   ├── datasets.py                   # Built-in CDC/WHO dataset registry
│   ├── fetcher.py                    # HTTP downloader, SHA-256 hashing, snapshot persistence
│   ├── differ.py                     # Structural diff engine (deepdiff + pandas)
│   ├── store.py                      # SQLite snapshot store (history, hashes, change records)
│   ├── reporter.py                   # Rich terminal output + Markdown report writer
│   ├── alerting.py                   # Slack webhook and SMTP email alerting
│   └── config.py                     # TOML config loader and validator
└── tests/
    ├── test_differ.py
    ├── test_fetcher.py
    ├── test_store.py
    ├── test_alerting.py
    ├── test_config.py
    └── test_datasets.py
```

---

## Configuration

Generate a starter config:

```bash
health-watchdog configure --init
# creates ~/.config/watchdog.toml
```

Or set the path explicitly:

```bash
export HEALTH_WATCHDOG_CONFIG=/etc/watchdog/config.toml
```

**Example `watchdog.toml`:**

```toml
[general]
data_dir = "~/.local/share/watchdog/snapshots"
db_path  = "~/.local/share/watchdog/watchdog.db"
log_level = "INFO"

[schedule]
interval_minutes = 360   # fetch every 6 hours

[alerts]
min_severity = "medium"  # options: low | medium | high | critical

[slack]
enabled = true
webhook_url = "https://hooks.slack.com/services/YOUR/WEBHOOK/URL"

[email]
enabled = false
smtp_host = "smtp.example.com"
smtp_port = 587
username  = "alerts@example.com"
password  = "secret"
from_addr = "alerts@example.com"
to_addrs  = ["researcher@example.com"]

# Add custom datasets beyond the built-in registry
[[custom_datasets]]
key         = "my_state_health_dept"
url         = "https://example.gov/covid-data.csv"
format      = "csv"
source      = "State Health Dept"
description = "Weekly county-level case counts"
enabled     = true
```

| Option | Default | Description |
|---|---|---|
| `general.data_dir` | `~/.local/share/watchdog/snapshots` | Where raw snapshot files are stored |
| `general.db_path` | `~/.local/share/watchdog/watchdog.db` | SQLite database path |
| `schedule.interval_minutes` | `360` | Fetch frequency when using `watch` |
| `alerts.min_severity` | `medium` | Minimum severity level to trigger alerts |
| `slack.enabled` | `false` | Enable Slack webhook notifications |
| `email.enabled` | `false` | Enable SMTP email notifications |

---

## License

MIT — see [LICENSE](LICENSE) for details.

---

*Built with [Jitter](https://github.com/jitter-ai) — an AI agent that ships code daily.*
