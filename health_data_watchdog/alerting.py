"""Alerting module for Health Data Watchdog.

This module implements Slack webhook and SMTP email alerting for detected
dataset changes.  Alerts are sent when the severity of a change meets or
exceeds configured thresholds.

Typical usage::

    from health_data_watchdog.alerting import AlertManager
    from health_data_watchdog.config import load_config
    from health_data_watchdog.differ import DiffResult

    config = load_config()
    manager = AlertManager(config)
    manager.send_alerts([result1, result2])

Or send individual alerts directly::

    from health_data_watchdog.alerting import send_slack_alert, send_email_alert
    from health_data_watchdog.config import SlackConfig, EmailConfig

    send_slack_alert(slack_cfg, "Test message", result)
    send_email_alert(email_cfg, "Subject", "Body", result)
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Dict, List, Optional, Sequence

import requests
from requests.exceptions import RequestException

from health_data_watchdog.config import Config, EmailConfig, SlackConfig
from health_data_watchdog.differ import (
    CHANGE_TYPE_CONTENT,
    CHANGE_TYPE_DELETION,
    CHANGE_TYPE_NEW,
    CHANGE_TYPE_SCHEMA,
    CHANGE_TYPE_UNCHANGED,
    SEVERITY_CRITICAL,
    SEVERITY_INFO,
    SEVERITY_WARNING,
    DiffResult,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Severity ordering
# ---------------------------------------------------------------------------

#: Numeric weight for each severity level; higher means more severe.
_SEVERITY_ORDER: Dict[str, int] = {
    SEVERITY_INFO: 0,
    SEVERITY_WARNING: 1,
    SEVERITY_CRITICAL: 2,
}


def severity_gte(a: str, b: str) -> bool:
    """Return ``True`` if severity *a* is greater than or equal to *b*.

    Args:
        a: The severity level to test.
        b: The threshold severity level.

    Returns:
        ``True`` if *a* >= *b* in the INFO < WARNING < CRITICAL ordering.
    """
    return _SEVERITY_ORDER.get(a, 0) >= _SEVERITY_ORDER.get(b, 0)


# ---------------------------------------------------------------------------
# Alert result dataclass
# ---------------------------------------------------------------------------


class AlertResult:
    """Outcome of a single alert delivery attempt.

    Attributes:
        channel: The alert channel used (``'slack'`` or ``'email'``).
        success: ``True`` if the alert was delivered without errors.
        dataset_key: The dataset key that triggered the alert, or ``None``
            for summary-level alerts.
        error: Exception instance if the alert failed, otherwise ``None``.
        message: Short description of the outcome.
    """

    __slots__ = ("channel", "success", "dataset_key", "error", "message")

    def __init__(
        self,
        channel: str,
        success: bool,
        dataset_key: Optional[str] = None,
        error: Optional[Exception] = None,
        message: str = "",
    ) -> None:
        self.channel = channel
        self.success = success
        self.dataset_key = dataset_key
        self.error = error
        self.message = message

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"AlertResult(channel={self.channel!r}, success={self.success!r}, "
            f"dataset_key={self.dataset_key!r}, message={self.message!r})"
        )

    def to_dict(self) -> Dict[str, object]:
        """Serialise to a plain dictionary."""
        return {
            "channel": self.channel,
            "success": self.success,
            "dataset_key": self.dataset_key,
            "error": str(self.error) if self.error else None,
            "message": self.message,
        }


# ---------------------------------------------------------------------------
# Message formatting helpers
# ---------------------------------------------------------------------------

_SEVERITY_EMOJI: Dict[str, str] = {
    SEVERITY_CRITICAL: "\U0001f534",  # 🔴
    SEVERITY_WARNING: "\U0001f7e1",   # 🟡
    SEVERITY_INFO: "\U0001f7e2",      # 🟢
}

_CHANGE_TYPE_EMOJI: Dict[str, str] = {
    CHANGE_TYPE_NEW: "\u2728",       # ✨
    CHANGE_TYPE_DELETION: "\u274c",  # ❌
    CHANGE_TYPE_SCHEMA: "\u26a0\ufe0f",  # ⚠️
    CHANGE_TYPE_CONTENT: "\U0001f4ca",   # 📊
    CHANGE_TYPE_UNCHANGED: "\u2705",     # ✅
}


def format_slack_message(result: DiffResult) -> Dict[str, object]:
    """Build a Slack Block Kit message payload for a diff result.

    The payload uses Slack's ``blocks`` format for rich formatting.  It
    includes a header block, a context block with severity/change type,
    and a section with the human-readable summary.

    Args:
        result: The :class:`~health_data_watchdog.differ.DiffResult` to
            format.

    Returns:
        A dict suitable for JSON-serialisation as the Slack webhook body.
    """
    sev_emoji = _SEVERITY_EMOJI.get(result.severity, "")
    ct_emoji = _CHANGE_TYPE_EMOJI.get(result.change_type, "")

    header_text = (
        f"{sev_emoji} Health Data Watchdog Alert — "
        f"`{result.dataset_key}`"
    )

    stats_parts: List[str] = []
    if result.old_row_count >= 0 or result.new_row_count >= 0:
        old_rows = str(result.old_row_count) if result.old_row_count >= 0 else "N/A"
        new_rows = str(result.new_row_count) if result.new_row_count >= 0 else "N/A"
        delta_sign = "+" if result.row_delta > 0 else ""
        stats_parts.append(
            f"*Rows:* {old_rows} → {new_rows} ({delta_sign}{result.row_delta})"
        )
    if result.removed_columns:
        stats_parts.append(
            f"*Columns removed:* {', '.join(f'`{c}`' for c in result.removed_columns)}"
        )
    if result.added_columns:
        stats_parts.append(
            f"*Columns added:* {', '.join(f'`{c}`' for c in result.added_columns)}"
        )
    if result.row_change_pct > 0:
        stats_parts.append(f"*Row change %:* {result.row_change_pct:.2f}%")

    stats_text = "\n".join(stats_parts) if stats_parts else "See summary below."

    blocks: List[Dict[str, object]] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": header_text,
                "emoji": True,
            },
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        f"{ct_emoji} *Change type:* `{result.change_type.upper()}`   "
                        f"  {sev_emoji} *Severity:* `{result.severity.upper()}`"
                    ),
                }
            ],
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Summary:*\n{result.summary}",
            },
        },
    ]

    if stats_parts:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": stats_text,
                },
            }
        )

    return {"blocks": blocks, "text": f"{sev_emoji} {result.summary}"}


def format_email_subject(result: DiffResult) -> str:
    """Build a concise email subject line for a diff result.

    Args:
        result: The diff result to describe.

    Returns:
        A subject line string such as::

            [CRITICAL] Health Data Watchdog: cdc_covid_cases — DELETION detected
    """
    sev = result.severity.upper()
    ct = result.change_type.upper()
    return f"[{sev}] Health Data Watchdog: {result.dataset_key} — {ct} detected"


def format_email_body_text(result: DiffResult) -> str:
    """Build a plain-text email body for a diff result.

    Args:
        result: The diff result to describe.

    Returns:
        A multi-line plain-text string suitable for the email body.
    """
    lines: List[str] = [
        "Health Data Watchdog — Dataset Change Alert",
        "=" * 44,
        "",
        f"Dataset:     {result.dataset_key}",
        f"Change type: {result.change_type.upper()}",
        f"Severity:    {result.severity.upper()}",
        "",
        "Summary",
        "-" * 40,
        result.summary,
        "",
    ]

    if result.old_row_count >= 0 or result.new_row_count >= 0:
        old_rows = str(result.old_row_count) if result.old_row_count >= 0 else "N/A"
        new_rows = str(result.new_row_count) if result.new_row_count >= 0 else "N/A"
        delta_sign = "+" if result.row_delta > 0 else ""
        lines.append(f"Row count:   {old_rows} → {new_rows} ({delta_sign}{result.row_delta})");

    if result.removed_columns:
        lines.append(f"Columns removed: {', '.join(result.removed_columns)}")

    if result.added_columns:
        lines.append(f"Columns added:   {', '.join(result.added_columns)}")

    if result.row_change_pct > 0:
        lines.append(f"Row change %: {result.row_change_pct:.2f}%")

    col_type_changes = [
        c for c in result.column_changes if c.change_type == "type_changed"
    ]
    if col_type_changes:
        lines.append("")
        lines.append("Column type changes:")
        for c in col_type_changes:
            lines.append(f"  {c.name}: {c.old_dtype} → {c.new_dtype}")

    lines.append("")
    lines.append("-" * 44)
    lines.append("This alert was generated by Health Data Watchdog.")
    lines.append("https://github.com/example/health-data-watchdog")

    return "\n".join(lines)


def format_email_body_html(result: DiffResult) -> str:
    """Build an HTML email body for a diff result.

    Args:
        result: The diff result to describe.

    Returns:
        An HTML string suitable for use as the ``text/html`` part of a
        MIME multipart email.
    """
    sev_colors: Dict[str, str] = {
        SEVERITY_CRITICAL: "#d93025",
        SEVERITY_WARNING: "#f6a723",
        SEVERITY_INFO: "#34a853",
    }
    color = sev_colors.get(result.severity, "#555555")

    old_rows = str(result.old_row_count) if result.old_row_count >= 0 else "N/A"
    new_rows = str(result.new_row_count) if result.new_row_count >= 0 else "N/A"
    delta_sign = "+" if result.row_delta > 0 else ""

    removed_html = ""
    if result.removed_columns:
        cols_html = ", ".join(
            f"<code>{c}</code>" for c in result.removed_columns
        )
        removed_html = f"<p><strong>Columns removed:</strong> {cols_html}</p>"

    added_html = ""
    if result.added_columns:
        cols_html = ", ".join(
            f"<code>{c}</code>" for c in result.added_columns
        )
        added_html = f"<p><strong>Columns added:</strong> {cols_html}</p>"

    type_changes_html = ""
    col_type_changes = [
        c for c in result.column_changes if c.change_type == "type_changed"
    ]
    if col_type_changes:
        rows_html = "".join(
            f"<tr><td><code>{c.name}</code></td>"
            f"<td><code>{c.old_dtype}</code></td>"
            f"<td><code>{c.new_dtype}</code></td></tr>"
            for c in col_type_changes
        )
        type_changes_html = (
            "<p><strong>Column type changes:</strong></p>"
            "<table border='1' cellpadding='4' cellspacing='0'>"
            "<thead><tr><th>Column</th><th>Old Type</th><th>New Type</th></tr></thead>"
            f"<tbody>{rows_html}</tbody></table>"
        )

    pct_html = ""
    if result.row_change_pct > 0:
        pct_html = f"<p><strong>Row change %:</strong> {result.row_change_pct:.2f}%</p>"

    html = f"""\
<!DOCTYPE html>
<html>
<body style="font-family: Arial, sans-serif; max-width: 640px; margin: 0 auto;">
  <div style="background:{color}; color:#fff; padding:12px 16px; border-radius:4px 4px 0 0;">
    <h2 style="margin:0;">Health Data Watchdog Alert</h2>
    <p style="margin:4px 0 0;">
      Severity: <strong>{result.severity.upper()}</strong> &nbsp;|&nbsp;
      Change: <strong>{result.change_type.upper()}</strong>
    </p>
  </div>
  <div style="border:1px solid #ddd; border-top:none; padding:16px; border-radius: 0 0 4px 4px;">
    <p><strong>Dataset:</strong> <code>{result.dataset_key}</code></p>
    <p><strong>Summary:</strong> {result.summary}</p>
    <hr />
    <p><strong>Row count:</strong> {old_rows} &rarr; {new_rows}
       ({delta_sign}{result.row_delta})</p>
    {removed_html}
    {added_html}
    {pct_html}
    {type_changes_html}
    <hr />
    <p style="color:#888; font-size:0.85em;">
      This alert was generated by
      <a href="https://github.com/example/health-data-watchdog">Health Data Watchdog</a>.
    </p>
  </div>
</body>
</html>"""
    return html


def format_summary_slack_message(
    results: Sequence[DiffResult],
    min_severity: str = SEVERITY_INFO,
) -> Dict[str, object]:
    """Build a Slack Block Kit summary message for multiple diff results.

    Includes only results whose severity meets *min_severity*.

    Args:
        results: Sequence of diff results to summarise.
        min_severity: Minimum severity to include in the summary.

    Returns:
        Slack webhook payload dict.
    """
    filtered = [
        r for r in results if severity_gte(r.severity, min_severity)
    ]

    if not filtered:
        return {
            "text": "\U0001f7e2 Health Data Watchdog: No significant changes detected.",
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            "\U0001f7e2 *Health Data Watchdog:* "
                            "No significant changes detected."
                        ),
                    },
                }
            ],
        }

    critical_count = sum(1 for r in filtered if r.severity == SEVERITY_CRITICAL)
    warning_count = sum(1 for r in filtered if r.severity == SEVERITY_WARNING)
    info_count = sum(1 for r in filtered if r.severity == SEVERITY_INFO)

    header_sev_emoji = (
        "\U0001f534" if critical_count
        else ("\U0001f7e1" if warning_count else "\U0001f7e2")
    )
    header_text = (
        f"{header_sev_emoji} Health Data Watchdog — "
        f"{len(filtered)} change(s) detected"
    )

    context_parts: List[str] = []
    if critical_count:
        context_parts.append(f"\U0001f534 {critical_count} CRITICAL")
    if warning_count:
        context_parts.append(f"\U0001f7e1 {warning_count} WARNING")
    if info_count:
        context_parts.append(f"\U0001f7e2 {info_count} INFO")

    blocks: List[Dict[str, object]] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": header_text,
                "emoji": True,
            },
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "   ".join(context_parts),
                }
            ],
        },
        {"type": "divider"},
    ]

    for result in filtered:
        sev_emoji = _SEVERITY_EMOJI.get(result.severity, "")
        ct_emoji = _CHANGE_TYPE_EMOJI.get(result.change_type, "")
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"{sev_emoji} {ct_emoji} *`{result.dataset_key}`*\n"
                        f"{result.summary}"
                    ),
                },
            }
        )

    return {"blocks": blocks, "text": header_text}


def format_summary_email(
    results: Sequence[DiffResult],
    min_severity: str = SEVERITY_INFO,
) -> tuple:
    """Build a summary email subject and body for multiple diff results.

    Args:
        results: Sequence of diff results to summarise.
        min_severity: Minimum severity to include.

    Returns:
        A 2-tuple of ``(subject, plain_text_body)``.
    """
    filtered = [
        r for r in results if severity_gte(r.severity, min_severity)
    ]

    critical_count = sum(1 for r in filtered if r.severity == SEVERITY_CRITICAL)
    warning_count = sum(1 for r in filtered if r.severity == SEVERITY_WARNING)
    info_count = sum(1 for r in filtered if r.severity == SEVERITY_INFO)

    top_severity = (
        SEVERITY_CRITICAL if critical_count
        else (SEVERITY_WARNING if warning_count else SEVERITY_INFO)
    )

    subject = (
        f"[{top_severity.upper()}] Health Data Watchdog: "
        f"{len(filtered)} dataset change(s) detected"
    )

    lines: List[str] = [
        "Health Data Watchdog — Dataset Change Summary",
        "=" * 44,
        "",
        f"Total changes detected: {len(filtered)}",
        f"  Critical : {critical_count}",
        f"  Warning  : {warning_count}",
        f"  Info     : {info_count}",
        "",
    ]

    if not filtered:
        lines.append("No significant changes detected.")
    else:
        for result in filtered:
            lines.append("-" * 44)
            lines.append(f"Dataset:  {result.dataset_key}")
            lines.append(f"Severity: {result.severity.upper()}")
            lines.append(f"Type:     {result.change_type.upper()}")
            lines.append(f"Summary:  {result.summary}")
            lines.append("")

    lines.append("-" * 44)
    lines.append("This alert was generated by Health Data Watchdog.")
    lines.append("https://github.com/example/health-data-watchdog")

    return subject, "\n".join(lines)


# ---------------------------------------------------------------------------
# Slack webhook sender
# ---------------------------------------------------------------------------


def send_slack_alert(
    config: SlackConfig,
    result: DiffResult,
    *,
    timeout: int = 10,
) -> AlertResult:
    """Send a Slack webhook alert for a single diff result.

    The alert is only sent if :attr:`SlackConfig.enabled` is ``True``
    and the ``webhook_url`` is non-empty.

    Args:
        config: Slack configuration with ``enabled`` flag and
            ``webhook_url``.
        result: The diff result to alert about.
        timeout: HTTP request timeout in seconds.

    Returns:
        An :class:`AlertResult` describing the outcome.
    """
    if not config.enabled or not config.webhook_url.strip():
        return AlertResult(
            channel="slack",
            success=False,
            dataset_key=result.dataset_key,
            message="Slack alerting is disabled or webhook_url is empty.",
        )

    payload = format_slack_message(result)

    try:
        response = requests.post(
            config.webhook_url,
            json=payload,
            timeout=timeout,
        )
        response.raise_for_status()
        logger.info(
            "Slack alert sent for dataset '%s' (status %d)",
            result.dataset_key,
            response.status_code,
        )
        return AlertResult(
            channel="slack",
            success=True,
            dataset_key=result.dataset_key,
            message=f"Alert delivered (HTTP {response.status_code}).",
        )
    except RequestException as exc:
        logger.error(
            "Failed to send Slack alert for '%s': %s", result.dataset_key, exc
        )
        return AlertResult(
            channel="slack",
            success=False,
            dataset_key=result.dataset_key,
            error=exc,
            message=f"Slack request failed: {exc}",
        )


def send_slack_summary(
    config: SlackConfig,
    results: Sequence[DiffResult],
    *,
    min_severity: str = SEVERITY_INFO,
    timeout: int = 10,
) -> AlertResult:
    """Send a single Slack summary message covering multiple diff results.

    Args:
        config: Slack configuration.
        results: Sequence of diff results to summarise.
        min_severity: Minimum severity level to include in the summary.
        timeout: HTTP request timeout in seconds.

    Returns:
        An :class:`AlertResult` describing the outcome.
    """
    if not config.enabled or not config.webhook_url.strip():
        return AlertResult(
            channel="slack",
            success=False,
            message="Slack alerting is disabled or webhook_url is empty.",
        )

    payload = format_summary_slack_message(results, min_severity=min_severity)

    try:
        response = requests.post(
            config.webhook_url,
            json=payload,
            timeout=timeout,
        )
        response.raise_for_status()
        logger.info("Slack summary alert sent (status %d)", response.status_code)
        return AlertResult(
            channel="slack",
            success=True,
            message=f"Summary alert delivered (HTTP {response.status_code}).",
        )
    except RequestException as exc:
        logger.error("Failed to send Slack summary alert: %s", exc)
        return AlertResult(
            channel="slack",
            success=False,
            error=exc,
            message=f"Slack summary request failed: {exc}",
        )


# ---------------------------------------------------------------------------
# SMTP email sender
# ---------------------------------------------------------------------------


def send_email_alert(
    config: EmailConfig,
    result: DiffResult,
) -> AlertResult:
    """Send an SMTP email alert for a single diff result.

    The alert is only sent if :attr:`EmailConfig.enabled` is ``True``.
    Both plain-text and HTML parts are included in the multipart message.

    Args:
        config: Email configuration.
        result: The diff result to alert about.

    Returns:
        An :class:`AlertResult` describing the outcome.
    """
    if not config.enabled:
        return AlertResult(
            channel="email",
            success=False,
            dataset_key=result.dataset_key,
            message="Email alerting is disabled.",
        )

    subject = format_email_subject(result)
    plain_body = format_email_body_text(result)
    html_body = format_email_body_html(result)

    try:
        msg = _build_mime_message(
            config=config,
            subject=subject,
            plain_body=plain_body,
            html_body=html_body,
        )
        _send_smtp(config, msg)
        logger.info(
            "Email alert sent for dataset '%s' to %s",
            result.dataset_key,
            config.to_addresses,
        )
        return AlertResult(
            channel="email",
            success=True,
            dataset_key=result.dataset_key,
            message=f"Email alert delivered to {len(config.to_addresses)} recipient(s).",
        )
    except Exception as exc:
        logger.error(
            "Failed to send email alert for '%s': %s", result.dataset_key, exc
        )
        return AlertResult(
            channel="email",
            success=False,
            dataset_key=result.dataset_key,
            error=exc,
            message=f"Email delivery failed: {exc}",
        )


def send_email_summary(
    config: EmailConfig,
    results: Sequence[DiffResult],
    *,
    min_severity: str = SEVERITY_INFO,
) -> AlertResult:
    """Send a single summary email covering multiple diff results.

    Args:
        config: Email configuration.
        results: Sequence of diff results to summarise.
        min_severity: Minimum severity level to include.

    Returns:
        An :class:`AlertResult` describing the outcome.
    """
    if not config.enabled:
        return AlertResult(
            channel="email",
            success=False,
            message="Email alerting is disabled.",
        )

    subject, plain_body = format_summary_email(results, min_severity=min_severity)

    try:
        msg = _build_mime_message(
            config=config,
            subject=subject,
            plain_body=plain_body,
            html_body=None,
        )
        _send_smtp(config, msg)
        logger.info(
            "Email summary alert sent to %s", config.to_addresses
        )
        return AlertResult(
            channel="email",
            success=True,
            message=f"Summary email delivered to {len(config.to_addresses)} recipient(s).",
        )
    except Exception as exc:
        logger.error("Failed to send summary email: %s", exc)
        return AlertResult(
            channel="email",
            success=False,
            error=exc,
            message=f"Summary email delivery failed: {exc}",
        )


def _build_mime_message(
    config: EmailConfig,
    subject: str,
    plain_body: str,
    html_body: Optional[str],
) -> MIMEMultipart:
    """Construct a :class:`email.mime.multipart.MIMEMultipart` message.

    Args:
        config: Email configuration providing addresses.
        subject: Email subject line.
        plain_body: Plain-text body content.
        html_body: HTML body content, or ``None`` for plain-text only.

    Returns:
        A fully constructed MIME message ready for SMTP delivery.
    """
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = config.from_address
    msg["To"] = ", ".join(config.to_addresses)

    msg.attach(MIMEText(plain_body, "plain", "utf-8"))
    if html_body is not None:
        msg.attach(MIMEText(html_body, "html", "utf-8"))

    return msg


def _send_smtp(config: EmailConfig, msg: MIMEMultipart) -> None:
    """Open an SMTP connection and deliver *msg*.

    Supports both STARTTLS (the default, port 587) and plain connections.
    Login is attempted only when ``smtp_user`` is non-empty.

    Args:
        config: Email configuration with SMTP server details.
        msg: The MIME message to send.

    Raises:
        smtplib.SMTPException: On any SMTP protocol error.
        OSError: On network connectivity errors.
    """
    recipients = config.to_addresses

    if config.use_tls:
        context = ssl.create_default_context()
        with smtplib.SMTP(config.smtp_host, config.smtp_port) as smtp:
            smtp.ehlo()
            smtp.starttls(context=context)
            smtp.ehlo()
            if config.smtp_user:
                smtp.login(config.smtp_user, config.smtp_password)
            smtp.sendmail(config.from_address, recipients, msg.as_string())
    else:
        with smtplib.SMTP(config.smtp_host, config.smtp_port) as smtp:
            smtp.ehlo()
            if config.smtp_user:
                smtp.login(config.smtp_user, config.smtp_password)
            smtp.sendmail(config.from_address, recipients, msg.as_string())


# ---------------------------------------------------------------------------
# AlertManager — high-level orchestrator
# ---------------------------------------------------------------------------


class AlertManager:
    """Orchestrates alert delivery across all configured channels.

    The :class:`AlertManager` reads :class:`~health_data_watchdog.config.Config`
    settings to determine which channels are active and what the minimum
    severity threshold is for sending alerts.

    Per-result alerts send one Slack message and one email per changed
    dataset.  Use :meth:`send_alerts` for individual per-result delivery,
    or :meth:`send_summary_alerts` for a single consolidated message.

    Args:
        config: Application configuration.  Only the ``slack``, ``email``,
            and ``thresholds`` sections are used.
        min_severity: Override the minimum severity level.  Defaults to
            ``'warning'`` so that ``info``-level changes are suppressed by
            default.
    """

    def __init__(
        self,
        config: Config,
        min_severity: str = SEVERITY_WARNING,
    ) -> None:
        self._config = config
        self._min_severity = min_severity

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def should_alert(self, result: DiffResult) -> bool:
        """Return ``True`` if *result* meets the minimum severity threshold.

        Args:
            result: The diff result to evaluate.

        Returns:
            ``True`` if the result's severity is >= the configured minimum.
        """
        return severity_gte(result.severity, self._min_severity)

    def send_alerts(
        self,
        results: Sequence[DiffResult],
    ) -> List[AlertResult]:
        """Send individual alerts for each qualifying diff result.

        A separate alert is dispatched per qualifying result and per
        enabled channel.

        Args:
            results: Sequence of diff results to evaluate and alert on.

        Returns:
            List of :class:`AlertResult` objects (one per channel per
            qualifying result).
        """
        alert_results: List[AlertResult] = []
        for result in results:
            if not self.should_alert(result):
                logger.debug(
                    "Skipping alert for '%s' (severity=%s < threshold=%s)",
                    result.dataset_key,
                    result.severity,
                    self._min_severity,
                )
                continue

            if self._config.slack.enabled:
                ar = send_slack_alert(self._config.slack, result)
                alert_results.append(ar)

            if self._config.email.enabled:
                ar = send_email_alert(self._config.email, result)
                alert_results.append(ar)

        return alert_results

    def send_summary_alerts(
        self,
        results: Sequence[DiffResult],
    ) -> List[AlertResult]:
        """Send a single consolidated summary alert for all qualifying results.

        Unlike :meth:`send_alerts`, this method sends **one** message per
        enabled channel containing information about all qualifying results.

        Args:
            results: Sequence of diff results to summarise.

        Returns:
            List of :class:`AlertResult` objects (one per enabled channel).
        """
        qualifying = [
            r for r in results if self.should_alert(r)
        ]

        if not qualifying and not results:
            logger.debug("No results to summarise; no summary alerts sent.")
            return []

        alert_results: List[AlertResult] = []

        if self._config.slack.enabled:
            ar = send_slack_summary(
                self._config.slack,
                qualifying,
                min_severity=self._min_severity,
            )
            alert_results.append(ar)

        if self._config.email.enabled:
            ar = send_email_summary(
                self._config.email,
                qualifying,
                min_severity=self._min_severity,
            )
            alert_results.append(ar)

        return alert_results
