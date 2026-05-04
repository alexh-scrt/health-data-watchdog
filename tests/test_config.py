"""Tests for the health_data_watchdog.config module.

Covers default loading, TOML parsing, validation errors, environment
variable resolution, and default config file generation.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from health_data_watchdog.config import (
    Config,
    ConfigValidationError,
    EmailConfig,
    GeneralConfig,
    ScheduleConfig,
    SlackConfig,
    ThresholdsConfig,
    load_config,
    write_default_config,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_toml(tmp_path: Path, content: str) -> Path:
    """Write TOML content to a temp file and return its path."""
    p = tmp_path / "config.toml"
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Default config (no file)
# ---------------------------------------------------------------------------


def test_load_config_defaults_when_no_file(tmp_path: Path) -> None:
    """Loading config from a non-existent path returns safe defaults."""
    missing = tmp_path / "nonexistent.toml"
    cfg = load_config(missing)
    assert isinstance(cfg, Config)
    assert cfg.schedule.interval_hours == 6.0
    assert cfg.thresholds.critical_row_change_pct == 10.0
    assert cfg.thresholds.warning_column_drop_count == 1
    assert cfg.slack.enabled is False
    assert cfg.email.enabled is False
    assert cfg.datasets == []


# ---------------------------------------------------------------------------
# Successful TOML parsing
# ---------------------------------------------------------------------------


def test_load_config_general_section(tmp_path: Path) -> None:
    """[general] section values are parsed correctly."""
    p = _write_toml(
        tmp_path,
        """
[general]
data_dir = "/tmp/snapshots"
db_path  = "/tmp/watchdog.db"
log_level = "DEBUG"
""",
    )
    cfg = load_config(p)
    assert cfg.general.data_dir == Path("/tmp/snapshots")
    assert cfg.general.db_path == Path("/tmp/watchdog.db")
    assert cfg.general.log_level == "DEBUG"


def test_load_config_schedule_section(tmp_path: Path) -> None:
    """[schedule] section values are parsed correctly."""
    p = _write_toml(tmp_path, "[schedule]\ninterval_hours = 12\n")
    cfg = load_config(p)
    assert cfg.schedule.interval_hours == 12.0


def test_load_config_thresholds_section(tmp_path: Path) -> None:
    """[thresholds] section values are parsed correctly."""
    p = _write_toml(
        tmp_path,
        "[thresholds]\ncritical_row_change_pct = 5.5\nwarning_column_drop_count = 2\n",
    )
    cfg = load_config(p)
    assert cfg.thresholds.critical_row_change_pct == 5.5
    assert cfg.thresholds.warning_column_drop_count == 2


def test_load_config_slack_enabled(tmp_path: Path) -> None:
    """[slack] section with enabled=true and a webhook URL parses correctly."""
    p = _write_toml(
        tmp_path,
        '[slack]\nenabled = true\nwebhook_url = "https://hooks.slack.com/xxx"\n',
    )
    cfg = load_config(p)
    assert cfg.slack.enabled is True
    assert cfg.slack.webhook_url == "https://hooks.slack.com/xxx"


def test_load_config_email_enabled(tmp_path: Path) -> None:
    """[email] section with enabled=true parses correctly."""
    p = _write_toml(
        tmp_path,
        """
[email]
enabled = true
smtp_host = "mail.example.com"
smtp_port = 465
smtp_user = "user"
smtp_password = "secret"
from_address = "a@example.com"
to_addresses = ["b@example.com", "c@example.com"]
use_tls = true
""",
    )
    cfg = load_config(p)
    assert cfg.email.enabled is True
    assert cfg.email.smtp_host == "mail.example.com"
    assert cfg.email.smtp_port == 465
    assert cfg.email.to_addresses == ["b@example.com", "c@example.com"]


def test_load_config_custom_datasets(tmp_path: Path) -> None:
    """[[datasets]] array is parsed into CustomDataset objects."""
    p = _write_toml(
        tmp_path,
        """
[[datasets]]
key         = "my_ds"
url         = "https://example.com/data.csv"
format      = "csv"
source      = "Custom"
description = "A test dataset"
enabled     = true
""",
    )
    cfg = load_config(p)
    assert len(cfg.datasets) == 1
    ds = cfg.datasets[0]
    assert ds.key == "my_ds"
    assert ds.url == "https://example.com/data.csv"
    assert ds.format == "csv"
    assert ds.enabled is True


# ---------------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------------


def test_invalid_log_level_raises(tmp_path: Path) -> None:
    """An unrecognised log level raises ConfigValidationError."""
    p = _write_toml(tmp_path, '[general]\nlog_level = "VERBOSE"\n')
    with pytest.raises(ConfigValidationError, match="log_level"):
        load_config(p)


def test_invalid_interval_zero_raises(tmp_path: Path) -> None:
    """interval_hours = 0 raises ConfigValidationError."""
    p = _write_toml(tmp_path, "[schedule]\ninterval_hours = 0\n")
    with pytest.raises(ConfigValidationError, match="interval_hours"):
        load_config(p)


def test_invalid_interval_negative_raises(tmp_path: Path) -> None:
    """Negative interval_hours raises ConfigValidationError."""
    p = _write_toml(tmp_path, "[schedule]\ninterval_hours = -3\n")
    with pytest.raises(ConfigValidationError, match="interval_hours"):
        load_config(p)


def test_slack_enabled_without_url_raises(tmp_path: Path) -> None:
    """Slack enabled=true with an empty webhook_url raises ConfigValidationError."""
    p = _write_toml(tmp_path, '[slack]\nenabled = true\nwebhook_url = ""\n')
    with pytest.raises(ConfigValidationError, match="webhook_url"):
        load_config(p)


def test_email_enabled_without_host_raises(tmp_path: Path) -> None:
    """Email enabled=true with empty smtp_host raises ConfigValidationError."""
    p = _write_toml(
        tmp_path,
        """
[email]
enabled = true
smtp_host = ""
from_address = "a@example.com"
to_addresses = ["b@example.com"]
""",
    )
    with pytest.raises(ConfigValidationError, match="smtp_host"):
        load_config(p)


def test_email_enabled_without_recipients_raises(tmp_path: Path) -> None:
    """Email enabled=true with empty to_addresses raises ConfigValidationError."""
    p = _write_toml(
        tmp_path,
        """
[email]
enabled = true
smtp_host = "mail.example.com"
from_address = "a@example.com"
to_addresses = []
""",
    )
    with pytest.raises(ConfigValidationError, match="to_addresses"):
        load_config(p)


def test_email_invalid_port_raises(tmp_path: Path) -> None:
    """An smtp_port outside 1-65535 raises ConfigValidationError."""
    p = _write_toml(
        tmp_path,
        """
[email]
enabled = true
smtp_host = "mail.example.com"
smtp_port = 99999
from_address = "a@example.com"
to_addresses = ["b@example.com"]
""",
    )
    with pytest.raises(ConfigValidationError, match="smtp_port"):
        load_config(p)


def test_custom_dataset_missing_key_raises(tmp_path: Path) -> None:
    """A custom dataset with an empty key raises ConfigValidationError."""
    p = _write_toml(
        tmp_path,
        """
[[datasets]]
key         = ""
url         = "https://example.com/data.csv"
format      = "csv"
source      = "Custom"
description = "A test dataset"
""",
    )
    with pytest.raises(ConfigValidationError, match="key"):
        load_config(p)


def test_custom_dataset_unknown_format_raises(tmp_path: Path) -> None:
    """A custom dataset with an unsupported format raises ConfigValidationError."""
    p = _write_toml(
        tmp_path,
        """
[[datasets]]
key         = "my_ds"
url         = "https://example.com/data.xml"
format      = "xml"
source      = "Custom"
description = "A test dataset"
""",
    )
    with pytest.raises(ConfigValidationError, match="format"):
        load_config(p)


# ---------------------------------------------------------------------------
# Environment variable resolution
# ---------------------------------------------------------------------------


def test_env_var_overrides_default_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """HEALTH_WATCHDOG_CONFIG env var is used when no explicit path is given."""
    p = _write_toml(tmp_path, "[schedule]\ninterval_hours = 3\n")
    monkeypatch.setenv("HEALTH_WATCHDOG_CONFIG", str(p))
    cfg = load_config()  # no path argument
    assert cfg.schedule.interval_hours == 3.0


def test_explicit_path_takes_precedence_over_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit path argument takes precedence over the env var."""
    env_toml = _write_toml(tmp_path / "env.toml", "[schedule]\ninterval_hours = 99\n")
    (tmp_path / "env.toml").rename(tmp_path / "env_cfg.toml")
    # Write the env toml
    env_cfg = tmp_path / "env_cfg.toml"
    env_cfg.write_text("[schedule]\ninterval_hours = 99\n", encoding="utf-8")
    explicit_cfg = tmp_path / "explicit.toml"
    explicit_cfg.write_text("[schedule]\ninterval_hours = 1\n", encoding="utf-8")

    monkeypatch.setenv("HEALTH_WATCHDOG_CONFIG", str(env_cfg))
    cfg = load_config(explicit_cfg)
    assert cfg.schedule.interval_hours == 1.0


# ---------------------------------------------------------------------------
# write_default_config
# ---------------------------------------------------------------------------


def test_write_default_config_creates_file(tmp_path: Path) -> None:
    """write_default_config creates the file if it does not exist."""
    target = tmp_path / "sub" / "config.toml"
    result = write_default_config(target)
    assert result == target
    assert target.exists()
    content = target.read_text(encoding="utf-8")
    assert "[general]" in content
    assert "[slack]" in content
    assert "[email]" in content


def test_write_default_config_does_not_overwrite(tmp_path: Path) -> None:
    """write_default_config does not overwrite an existing file."""
    target = tmp_path / "config.toml"
    target.write_text("[schedule]\ninterval_hours = 42\n", encoding="utf-8")
    write_default_config(target)
    # Original content must be preserved.
    assert "interval_hours = 42" in target.read_text(encoding="utf-8")


def test_write_default_config_is_valid_toml(tmp_path: Path) -> None:
    """The generated default config can be loaded without errors."""
    target = tmp_path / "config.toml"
    write_default_config(target)
    cfg = load_config(target)
    assert cfg.schedule.interval_hours == 6.0
    assert cfg.general.log_level == "INFO"
