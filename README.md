# health-data-watchdog
health_data_watchdog is a CLI tool that periodically fetches key public health surveillance datasets from sources like CDC and WHO, then diffs them against previously cached snapshots to detect changes, deletions, or schema alterations. It generates human-readable changelogs, stores a local history of dataset states, and can post alerts to Slack or
