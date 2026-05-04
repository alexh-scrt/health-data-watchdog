"""Configuration loader and validator for Health Data Watchdog.

Loads TOML-based configuration from disk, applies defaults, validates
all required fields, and exposes a typed Config dataclass for the rest
of the application to consume.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

# Python < 3.11 requires the third-party tomli; 3.11+ ships tomllib in stdlib.
if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "tomli is required on Python < 3.11. Install it with: pip install tomli"
        ) from exc


# ---------------------------------------------------------------------------
# Default paths
# ---------------------------------------------------------------------------

DEFAULT_CONFIG_DIR: Path = Path.home() / ".config" / "health_watchdog"
DEFAULT_CONFIG_PATH: Path = DEFAULT_CONFIG_DIR / "config.toml"
DEFAULT_DATA_DIR: Path = Path.home() / ".local" / "share" / "health_watchdog" / "snapshots"
DEFAULT_DB_PATH: Path = Path.home() / ".local" / "share" / "health_watchdog" / "watchdog.db"

# Environment variable that can override the config file path.
CONFIG_ENV_VAR: str = "HEALTH_WATCHDOG_CONFIG"


# ---------------------------------------------------------------------------
# Dataclasses for typed config sections
# ---------------------------------------------------------------------------


@dataclass
class GeneralConfig:
    """General application settings."""

    data_dir: Path = field(default_factory=lambda: DEFAULT_DATA_DIR)
    db_path: Path = field(default_factory=lambda: DEFAULT_DB_PATH)
    log_level: str = "INFO"


@dataclass
class ScheduleConfig:
    """Scheduler settings for the watch command."""

    interval_hours: float = 6.0


@dataclass
class ThresholdsConfig:
    """Alert threshold settings."""

    critical_row_change_pct: float = 10.0
    warning_column_drop_count: int = 1


@dataclass
class SlackConfig:
    """Slack webhook alerting settings."""

    enabled: bool = False
    webhook_url: str = ""


@dataclass
class EmailConfig:
    """SMTP email alerting settings."""

    enabled: bool = False
    smtp_host: str = "smtp.example.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    from_address: str = "watchdog@example.com"
    to_addresses: List[str] = field(default_factory=list)
    use_tls: bool = True


@dataclass
class CustomDataset:
    """A user-defined custom dataset entry."""

    key: str
    url: str
    format: str
    source: str
    description: str
    enabled: bool = True


@dataclass
class Config:
    """Top-level configuration object for Health Data Watchdog."""

    general: GeneralConfig = field(default_factory=GeneralConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    thresholds: ThresholdsConfig = field(default_factory=ThresholdsConfig)
    slack: SlackConfig = field(default_factory=SlackConfig)
    email: EmailConfig = field(default_factory=EmailConfig)
    datasets: List[CustomDataset] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


class ConfigValidationError(ValueError):
    """Raised when the configuration file contains invalid values."""


def _validate_log_level(level: str) -> str:
    """Ensure the log level is one of the accepted standard values.

    Args:
        level: The log level string to validate.

    Returns:
        The uppercased, validated log level string.

    Raises:
        ConfigValidationError: If the level is not recognised.
    """
    valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
    upper = level.upper()
    if upper not in valid:
        raise ConfigValidationError(
            f"Invalid log_level '{level}'. Must be one of: {', '.join(sorted(valid))}"
        )
    return upper


def _validate_positive_float(value: float, name: str) -> float:
    """Ensure a numeric value is strictly positive.

    Args:
        value: The value to check.
        name: Field name used in the error message.

    Returns:
        The original value if valid.

    Raises:
        ConfigValidationError: If value is not positive.
    """
    if value <= 0:
        raise ConfigValidationError(f"'{name}' must be a positive number, got {value!r}.")
    return value


def _validate_port(port: int) -> int:
    """Ensure an SMTP port is in the valid range 1–65535.

    Args:
        port: The port number to validate.

    Returns:
        The original port if valid.

    Raises:
        ConfigValidationError: If the port is out of range.
    """
    if not (1 <= port <= 65535):
        raise ConfigValidationError(
            f"smtp_port must be between 1 and 65535, got {port!r}."
        )
    return port


def _validate_slack(slack: SlackConfig) -> None:
    """Validate Slack configuration.

    Raises:
        ConfigValidationError: If Slack is enabled but no webhook URL is set.
    """
    if slack.enabled and not slack.webhook_url.strip():
        raise ConfigValidationError(
            "[slack] enabled = true but webhook_url is empty. "
            "Provide a valid Slack webhook URL."
        )


def _validate_email(email: EmailConfig) -> None:
    """Validate email configuration.

    Raises:
        ConfigValidationError: If email is enabled but required fields are missing.
    """
    if not email.enabled:
        return
    if not email.smtp_host.strip():
        raise ConfigValidationError(
            "[email] enabled = true but smtp_host is empty."
        )
    if not email.from_address.strip():
        raise ConfigValidationError(
            "[email] enabled = true but from_address is empty."
        )
    if not email.to_addresses:
        raise ConfigValidationError(
            "[email] enabled = true but to_addresses list is empty."
        )
    _validate_port(email.smtp_port)


def _validate_custom_dataset(ds: CustomDataset, index: int) -> None:
    """Validate a single custom dataset entry.

    Args:
        ds: The dataset to validate.
        index: The zero-based index in the [[datasets]] array (for error messages).

    Raises:
        ConfigValidationError: If any required field is blank or the format is unknown.
    """
    known_formats = {"csv", "json", "tsv", "xlsx", "parquet"}
    for attr in ("key", "url", "format", "source", "description"):
        if not getattr(ds, attr, "").strip():
            raise ConfigValidationError(
                f"[[datasets]][{index}] field '{attr}' must not be empty."
            )
    if ds.format.lower() not in known_formats:
        raise ConfigValidationError(
            f"[[datasets]][{index}] unsupported format '{ds.format}'. "
            f"Must be one of: {', '.join(sorted(known_formats))}."
        )


def _validate_config(config: Config) -> None:
    """Run all validation rules against the fully-built Config object.

    Args:
        config: The Config object to validate.

    Raises:
        ConfigValidationError: On the first validation failure encountered.
    """
    _validate_log_level(config.general.log_level)
    _validate_positive_float(config.schedule.interval_hours, "interval_hours")
    _validate_positive_float(
        config.thresholds.critical_row_change_pct, "critical_row_change_pct"
    )
    if config.thresholds.warning_column_drop_count < 0:
        raise ConfigValidationError(
            "'warning_column_drop_count' must be >= 0, "
            f"got {config.thresholds.warning_column_drop_count!r}."
        )
    _validate_slack(config.slack)
    _validate_email(config.email)
    for i, ds in enumerate(config.datasets):
        _validate_custom_dataset(ds, i)


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _parse_general(raw: dict) -> GeneralConfig:
    """Parse the [general] table from raw TOML data."""
    section = raw.get("general", {})
    return GeneralConfig(
        data_dir=Path(section.get("data_dir", DEFAULT_DATA_DIR)).expanduser(),
        db_path=Path(section.get("db_path", DEFAULT_DB_PATH)).expanduser(),
        log_level=str(section.get("log_level", "INFO")),
    )


def _parse_schedule(raw: dict) -> ScheduleConfig:
    """Parse the [schedule] table from raw TOML data."""
    section = raw.get("schedule", {})
    return ScheduleConfig(
        interval_hours=float(section.get("interval_hours", 6.0)),
    )


def _parse_thresholds(raw: dict) -> ThresholdsConfig:
    """Parse the [thresholds] table from raw TOML data."""
    section = raw.get("thresholds", {})
    return ThresholdsConfig(
        critical_row_change_pct=float(section.get("critical_row_change_pct", 10.0)),
        warning_column_drop_count=int(section.get("warning_column_drop_count", 1)),
    )


def _parse_slack(raw: dict) -> SlackConfig:
    """Parse the [slack] table from raw TOML data."""
    section = raw.get("slack", {})
    return SlackConfig(
        enabled=bool(section.get("enabled", False)),
        webhook_url=str(section.get("webhook_url", "")),
    )


def _parse_email(raw: dict) -> EmailConfig:
    """Parse the [email] table from raw TOML data."""
    section = raw.get("email", {})
    return EmailConfig(
        enabled=bool(section.get("enabled", False)),
        smtp_host=str(section.get("smtp_host", "smtp.example.com")),
        smtp_port=int(section.get("smtp_port", 587)),
        smtp_user=str(section.get("smtp_user", "")),
        smtp_password=str(section.get("smtp_password", "")),
        from_address=str(section.get("from_address", "watchdog@example.com")),
        to_addresses=list(section.get("to_addresses", [])),
        use_tls=bool(section.get("use_tls", True)),
    )


def _parse_custom_datasets(raw: dict) -> List[CustomDataset]:
    """Parse the [[datasets]] array from raw TOML data."""
    entries = raw.get("datasets", [])
    result: List[CustomDataset] = []
    for entry in entries:
        result.append(
            CustomDataset(
                key=str(entry.get("key", "")),
                url=str(entry.get("url", "")),
                format=str(entry.get("format", "")),
                source=str(entry.get("source", "")),
                description=str(entry.get("description", "")),
                enabled=bool(entry.get("enabled", True)),
            )
        )
    return result


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_config(path: Optional[Path] = None) -> Config:
    """Load and validate the watchdog configuration from a TOML file.

    Resolution order for the config file path:
    1. ``path`` argument if provided.
    2. ``HEALTH_WATCHDOG_CONFIG`` environment variable.
    3. ``~/.config/health_watchdog/config.toml`` (default).

    If the resolved file does not exist, default values are returned
    without raising an error (making first-run use convenient).

    Args:
        path: Explicit path to the TOML configuration file, or ``None``
              to use environment/default resolution.

    Returns:
        A validated :class:`Config` instance.

    Raises:
        ConfigValidationError: If the configuration contains invalid values.
        OSError: If the file exists but cannot be read.
        tomllib.TOMLDecodeError: If the file is not valid TOML.
    """
    resolved: Path
    if path is not None:
        resolved = Path(path).expanduser().resolve()
    else:
        env_path = os.environ.get(CONFIG_ENV_VAR)
        if env_path:
            resolved = Path(env_path).expanduser().resolve()
        else:
            resolved = DEFAULT_CONFIG_PATH.expanduser().resolve()

    raw: dict = {}
    if resolved.exists():
        with open(resolved, "rb") as fh:
            raw = tomllib.load(fh)

    config = Config(
        general=_parse_general(raw),
        schedule=_parse_schedule(raw),
        thresholds=_parse_thresholds(raw),
        slack=_parse_slack(raw),
        email=_parse_email(raw),
        datasets=_parse_custom_datasets(raw),
    )

    _validate_config(config)
    return config


def write_default_config(path: Optional[Path] = None) -> Path:
    """Write a default configuration file to disk.

    Existing files are **not** overwritten; the function is idempotent.

    Args:
        path: Destination path for the config file.  Defaults to
              ``~/.config/health_watchdog/config.toml``.

    Returns:
        The :class:`Path` to which the config was written (or already existed).

    Raises:
        OSError: If the directory cannot be created or the file written.
    """
    target = Path(path).expanduser() if path else DEFAULT_CONFIG_PATH.expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)

    if target.exists():
        return target

    default_content = """\
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

# Add custom datasets below, e.g.:
# [[datasets]]
# key         = "my_dataset"
# url         = "https://example.com/data.csv"
# format      = "csv"
# source      = "Custom"
# description = "My custom health dataset"
# enabled     = true
"""
    target.write_text(default_content, encoding="utf-8")
    return target
