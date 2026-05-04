"""Tests for the health_data_watchdog.alerting module.

Covers Slack webhook formatting and delivery, SMTP email formatting and
delivery, severity filtering, AlertManager orchestration, and edge cases
like disabled channels and network errors.  SMTP and HTTP calls are
fully mocked to avoid real network I/O.
"""

from __future__ import annotations

import json
import smtplib
from typing import List, Optional
from unittest.mock import MagicMock, Mock, patch

import pytest
import responses as responses_lib

from health_data_watchdog.alerting import (
    AlertManager,
    AlertResult,
    _build_mime_message,
    format_email_body_html,
    format_email_body_text,
    format_email_subject,
    format_slack_message,
    format_summary_email,
    format_summary_slack_message,
    send_email_alert,
    send_email_summary,
    send_slack_alert,
    send_slack_summary,
    severity_gte,
)
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
    ColumnChange,
    DiffResult,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TEST_WEBHOOK_URL = "https://hooks.slack.com/services/TEST/WEBHOOK"


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _result(
    key: str = "test_ds",
    change_type: str = CHANGE_TYPE_CONTENT,
    severity: str = SEVERITY_WARNING,
    old_row_count: int = 100,
    new_row_count: int = 110,
    row_delta: int = 10,
    row_change_pct: float = 5.0,
    added_columns: Optional[List[str]] = None,
    removed_columns: Optional[List[str]] = None,
    column_changes: Optional[List[ColumnChange]] = None,
    summary: str = "Some rows changed.",
    rows_added: int = 10,
    rows_removed: int = 0,
    rows_modified: int = 0,
) -> DiffResult:
    """Build a minimal DiffResult for testing."""
    return DiffResult(
        dataset_key=key,
        change_type=change_type,
        severity=severity,
        old_row_count=old_row_count,
        new_row_count=new_row_count,
        row_delta=row_delta,
        row_change_pct=row_change_pct,
        added_columns=added_columns or [],
        removed_columns=removed_columns or [],
        column_changes=column_changes or [],
        summary=summary,
        rows_added=rows_added,
        rows_removed=rows_removed,
        rows_modified=rows_modified,
        diff_json={"old_row_count": old_row_count, "new_row_count": new_row_count},
    )


def _slack_config(
    enabled: bool = True,
    webhook_url: str = TEST_WEBHOOK_URL,
) -> SlackConfig:
    return SlackConfig(enabled=enabled, webhook_url=webhook_url)


def _email_config(
    enabled: bool = True,
    smtp_host: str = "smtp.example.com",
    smtp_port: int = 587,
    smtp_user: str = "user@example.com",
    smtp_password: str = "secret",
    from_address: str = "watchdog@example.com",
    to_addresses: Optional[List[str]] = None,
    use_tls: bool = True,
) -> EmailConfig:
    return EmailConfig(
        enabled=enabled,
        smtp_host=smtp_host,
        smtp_port=smtp_port,
        smtp_user=smtp_user,
        smtp_password=smtp_password,
        from_address=from_address,
        to_addresses=to_addresses or ["recipient@example.com"],
        use_tls=use_tls,
    )


def _config(
    slack_enabled: bool = False,
    email_enabled: bool = False,
) -> Config:
    """Build a minimal Config for testing."""
    return Config(
        slack=_slack_config(enabled=slack_enabled),
        email=_email_config(enabled=email_enabled),
    )


# ---------------------------------------------------------------------------
# severity_gte
# ---------------------------------------------------------------------------


class TestSeverityGte:
    """Tests for the severity_gte helper."""

    def test_same_severity_is_gte(self) -> None:
        assert severity_gte(SEVERITY_WARNING, SEVERITY_WARNING) is True

    def test_critical_gte_warning(self) -> None:
        assert severity_gte(SEVERITY_CRITICAL, SEVERITY_WARNING) is True

    def test_critical_gte_info(self) -> None:
        assert severity_gte(SEVERITY_CRITICAL, SEVERITY_INFO) is True

    def test_warning_gte_info(self) -> None:
        assert severity_gte(SEVERITY_WARNING, SEVERITY_INFO) is True

    def test_info_not_gte_warning(self) -> None:
        assert severity_gte(SEVERITY_INFO, SEVERITY_WARNING) is False

    def test_info_not_gte_critical(self) -> None:
        assert severity_gte(SEVERITY_INFO, SEVERITY_CRITICAL) is False

    def test_warning_not_gte_critical(self) -> None:
        assert severity_gte(SEVERITY_WARNING, SEVERITY_CRITICAL) is False

    def test_unknown_severity_defaults_to_zero(self) -> None:
        # Unknown severities map to 0 — equivalent to INFO or below.
        assert severity_gte("unknown", SEVERITY_INFO) is False


# ---------------------------------------------------------------------------
# AlertResult
# ---------------------------------------------------------------------------


class TestAlertResult:
    """Tests for the AlertResult dataclass."""

    def test_to_dict_has_expected_keys(self) -> None:
        ar = AlertResult(channel="slack", success=True, dataset_key="ds", message="ok")
        d = ar.to_dict()
        for key in ("channel", "success", "dataset_key", "error", "message"):
            assert key in d

    def test_to_dict_success_true(self) -> None:
        ar = AlertResult(channel="email", success=True)
        assert ar.to_dict()["success"] is True

    def test_to_dict_error_is_string_when_set(self) -> None:
        exc = ValueError("boom")
        ar = AlertResult(channel="slack", success=False, error=exc)
        d = ar.to_dict()
        assert isinstance(d["error"], str)
        assert "boom" in d["error"]

    def test_to_dict_error_is_none_when_success(self) -> None:
        ar = AlertResult(channel="slack", success=True)
        assert ar.to_dict()["error"] is None


# ---------------------------------------------------------------------------
# format_slack_message
# ---------------------------------------------------------------------------


class TestFormatSlackMessage:
    """Tests for format_slack_message."""

    def test_returns_dict(self) -> None:
        msg = format_slack_message(_result())
        assert isinstance(msg, dict)

    def test_has_blocks_key(self) -> None:
        msg = format_slack_message(_result())
        assert "blocks" in msg

    def test_has_text_fallback(self) -> None:
        msg = format_slack_message(_result())
        assert "text" in msg
        assert len(msg["text"]) > 0

    def test_blocks_is_list(self) -> None:
        msg = format_slack_message(_result())
        assert isinstance(msg["blocks"], list)

    def test_dataset_key_in_header(self) -> None:
        msg = format_slack_message(_result(key="cdc_covid"))
        header_block = msg["blocks"][0]
        assert "cdc_covid" in header_block["text"]["text"]

    def test_critical_severity_in_message(self) -> None:
        result = _result(severity=SEVERITY_CRITICAL)
        msg = format_slack_message(result)
        # Check the fallback text contains severity info
        all_text = json.dumps(msg)
        assert "CRITICAL" in all_text or "\U0001f534" in all_text

    def test_deletion_change_type_in_message(self) -> None:
        result = _result(change_type=CHANGE_TYPE_DELETION, severity=SEVERITY_CRITICAL)
        msg = format_slack_message(result)
        all_text = json.dumps(msg, ensure_ascii=False)
        assert "DELETION" in all_text or "❌" in all_text

    def test_removed_columns_in_message(self) -> None:
        result = _result(
            change_type=CHANGE_TYPE_SCHEMA,
            removed_columns=["col_a", "col_b"],
        )
        msg = format_slack_message(result)
        all_text = json.dumps(msg)
        assert "col_a" in all_text

    def test_added_columns_in_message(self) -> None:
        result = _result(
            change_type=CHANGE_TYPE_SCHEMA,
            added_columns=["new_col"],
        )
        msg = format_slack_message(result)
        all_text = json.dumps(msg)
        assert "new_col" in all_text

    def test_row_delta_positive_shows_plus_sign(self) -> None:
        result = _result(row_delta=50)
        msg = format_slack_message(result)
        all_text = json.dumps(msg)
        assert "+50" in all_text

    def test_summary_text_included(self) -> None:
        result = _result(summary="Important dataset changed drastically.")
        msg = format_slack_message(result)
        all_text = json.dumps(msg)
        assert "Important dataset changed drastically." in all_text

    def test_is_json_serialisable(self) -> None:
        msg = format_slack_message(_result())
        try:
            json.dumps(msg)
        except (TypeError, ValueError) as exc:
            pytest.fail(f"Slack message is not JSON-serialisable: {exc}")


# ---------------------------------------------------------------------------
# format_email_subject
# ---------------------------------------------------------------------------


class TestFormatEmailSubject:
    """Tests for format_email_subject."""

    def test_contains_severity(self) -> None:
        subject = format_email_subject(_result(severity=SEVERITY_CRITICAL))
        assert "CRITICAL" in subject

    def test_contains_dataset_key(self) -> None:
        subject = format_email_subject(_result(key="who_covid_global"))
        assert "who_covid_global" in subject

    def test_contains_change_type(self) -> None:
        subject = format_email_subject(_result(change_type=CHANGE_TYPE_DELETION))
        assert "DELETION" in subject

    def test_is_non_empty_string(self) -> None:
        subject = format_email_subject(_result())
        assert isinstance(subject, str)
        assert len(subject) > 0

    def test_watchdog_branding_present(self) -> None:
        subject = format_email_subject(_result())
        assert "Watchdog" in subject or "watchdog" in subject.lower()


# ---------------------------------------------------------------------------
# format_email_body_text
# ---------------------------------------------------------------------------


class TestFormatEmailBodyText:
    """Tests for format_email_body_text."""

    def test_contains_dataset_key(self) -> None:
        body = format_email_body_text(_result(key="cdc_flu"))
        assert "cdc_flu" in body

    def test_contains_severity(self) -> None:
        body = format_email_body_text(_result(severity=SEVERITY_CRITICAL))
        assert "CRITICAL" in body

    def test_contains_change_type(self) -> None:
        body = format_email_body_text(_result(change_type=CHANGE_TYPE_SCHEMA))
        assert "SCHEMA" in body

    def test_contains_summary(self) -> None:
        body = format_email_body_text(_result(summary="Unique summary text."))
        assert "Unique summary text." in body

    def test_removed_columns_mentioned(self) -> None:
        body = format_email_body_text(
            _result(removed_columns=["dropped_col"])
        )
        assert "dropped_col" in body

    def test_added_columns_mentioned(self) -> None:
        body = format_email_body_text(
            _result(added_columns=["shiny_new"])
        )
        assert "shiny_new" in body

    def test_column_type_changes_mentioned(self) -> None:
        cc = ColumnChange(
            name="score", change_type="type_changed",
            old_dtype="int64", new_dtype="object"
        )
        body = format_email_body_text(_result(column_changes=[cc]))
        assert "score" in body
        assert "int64" in body
        assert "object" in body

    def test_is_plain_string(self) -> None:
        body = format_email_body_text(_result())
        assert isinstance(body, str)


# ---------------------------------------------------------------------------
# format_email_body_html
# ---------------------------------------------------------------------------


class TestFormatEmailBodyHtml:
    """Tests for format_email_body_html."""

    def test_is_html_string(self) -> None:
        html = format_email_body_html(_result())
        assert "<html" in html.lower() or "<!doctype" in html.lower()

    def test_contains_dataset_key(self) -> None:
        html = format_email_body_html(_result(key="who_disease"))
        assert "who_disease" in html

    def test_contains_severity_color(self) -> None:
        # Critical should use a reddish color
        html = format_email_body_html(_result(severity=SEVERITY_CRITICAL))
        assert "#d93025" in html or "CRITICAL" in html

    def test_contains_summary(self) -> None:
        html = format_email_body_html(_result(summary="HTML summary text."))
        assert "HTML summary text." in html

    def test_removed_columns_in_html(self) -> None:
        html = format_email_body_html(_result(removed_columns=["bye_col"]))
        assert "bye_col" in html

    def test_added_columns_in_html(self) -> None:
        html = format_email_body_html(_result(added_columns=["hi_col"]))
        assert "hi_col" in html

    def test_type_changes_table_present(self) -> None:
        cc = ColumnChange(
            name="count", change_type="type_changed",
            old_dtype="int64", new_dtype="float64"
        )
        html = format_email_body_html(_result(column_changes=[cc]))
        assert "<table" in html
        assert "count" in html

    def test_warning_color(self) -> None:
        html = format_email_body_html(_result(severity=SEVERITY_WARNING))
        assert "#f6a723" in html or "WARNING" in html

    def test_info_color(self) -> None:
        html = format_email_body_html(_result(severity=SEVERITY_INFO))
        assert "#34a853" in html or "INFO" in html


# ---------------------------------------------------------------------------
# format_summary_slack_message
# ---------------------------------------------------------------------------


class TestFormatSummarySlackMessage:
    """Tests for format_summary_slack_message."""

    def test_returns_dict(self) -> None:
        msg = format_summary_slack_message([])
        assert isinstance(msg, dict)

    def test_empty_results_returns_no_changes_message(self) -> None:
        msg = format_summary_slack_message([])
        all_text = json.dumps(msg)
        assert "No significant" in all_text or "no" in all_text.lower()

    def test_multiple_results_included(self) -> None:
        results = [
            _result(key="ds_a", severity=SEVERITY_CRITICAL),
            _result(key="ds_b", severity=SEVERITY_WARNING),
        ]
        msg = format_summary_slack_message(results)
        all_text = json.dumps(msg)
        assert "ds_a" in all_text
        assert "ds_b" in all_text

    def test_min_severity_filters_results(self) -> None:
        results = [
            _result(key="critical_ds", severity=SEVERITY_CRITICAL),
            _result(key="info_ds", severity=SEVERITY_INFO),
        ]
        msg = format_summary_slack_message(results, min_severity=SEVERITY_WARNING)
        all_text = json.dumps(msg)
        assert "critical_ds" in all_text
        assert "info_ds" not in all_text

    def test_has_blocks(self) -> None:
        results = [_result()]
        msg = format_summary_slack_message(results)
        assert "blocks" in msg
        assert isinstance(msg["blocks"], list)

    def test_is_json_serialisable(self) -> None:
        results = [_result()]
        msg = format_summary_slack_message(results)
        json.dumps(msg)  # should not raise


# ---------------------------------------------------------------------------
# format_summary_email
# ---------------------------------------------------------------------------


class TestFormatSummaryEmail:
    """Tests for format_summary_email."""

    def test_returns_tuple(self) -> None:
        result_tuple = format_summary_email([])
        assert isinstance(result_tuple, tuple)
        assert len(result_tuple) == 2

    def test_subject_is_string(self) -> None:
        subject, _ = format_summary_email([_result()])
        assert isinstance(subject, str)

    def test_body_is_string(self) -> None:
        _, body = format_summary_email([_result()])
        assert isinstance(body, str)

    def test_subject_contains_change_count(self) -> None:
        results = [_result(key="a"), _result(key="b")]
        subject, _ = format_summary_email(results)
        assert "2" in subject

    def test_body_contains_dataset_keys(self) -> None:
        results = [_result(key="ds_a"), _result(key="ds_b")]
        _, body = format_summary_email(results)
        assert "ds_a" in body
        assert "ds_b" in body

    def test_min_severity_filters_body(self) -> None:
        results = [
            _result(key="critical_ds", severity=SEVERITY_CRITICAL),
            _result(key="info_ds", severity=SEVERITY_INFO),
        ]
        subject, body = format_summary_email(results, min_severity=SEVERITY_WARNING)
        assert "critical_ds" in body
        assert "info_ds" not in body

    def test_critical_severity_in_subject(self) -> None:
        results = [_result(severity=SEVERITY_CRITICAL)]
        subject, _ = format_summary_email(results)
        assert "CRITICAL" in subject

    def test_empty_results_no_change_message(self) -> None:
        _, body = format_summary_email([])
        assert "No significant" in body or "no" in body.lower()


# ---------------------------------------------------------------------------
# send_slack_alert
# ---------------------------------------------------------------------------


class TestSendSlackAlert:
    """Tests for send_slack_alert."""

    @responses_lib.activate
    def test_sends_post_request(self) -> None:
        """A POST request is sent to the webhook URL."""
        responses_lib.add(responses_lib.POST, TEST_WEBHOOK_URL, body=b"ok", status=200)
        result = send_slack_alert(_slack_config(), _result())
        assert len(responses_lib.calls) == 1
        assert responses_lib.calls[0].request.url == TEST_WEBHOOK_URL

    @responses_lib.activate
    def test_returns_success_on_200(self) -> None:
        """A 200 response produces a successful AlertResult."""
        responses_lib.add(responses_lib.POST, TEST_WEBHOOK_URL, body=b"ok", status=200)
        ar = send_slack_alert(_slack_config(), _result())
        assert ar.success is True
        assert ar.channel == "slack"

    @responses_lib.activate
    def test_dataset_key_in_result(self) -> None:
        """The dataset_key is set on the AlertResult."""
        responses_lib.add(responses_lib.POST, TEST_WEBHOOK_URL, body=b"ok", status=200)
        ar = send_slack_alert(_slack_config(), _result(key="my_ds"))
        assert ar.dataset_key == "my_ds"

    @responses_lib.activate
    def test_sends_valid_json_body(self) -> None:
        """The request body is valid JSON with expected keys."""
        responses_lib.add(responses_lib.POST, TEST_WEBHOOK_URL, body=b"ok", status=200)
        send_slack_alert(_slack_config(), _result())
        request_body = json.loads(responses_lib.calls[0].request.body)
        assert "blocks" in request_body or "text" in request_body

    @responses_lib.activate
    def test_returns_failure_on_http_error(self) -> None:
        """A non-200 HTTP response produces a failed AlertResult."""
        responses_lib.add(responses_lib.POST, TEST_WEBHOOK_URL, status=400, body=b"bad")
        ar = send_slack_alert(_slack_config(), _result())
        assert ar.success is False
        assert ar.error is not None

    @responses_lib.activate
    def test_returns_failure_on_connection_error(self) -> None:
        """A connection error produces a failed AlertResult."""
        from requests.exceptions import ConnectionError as ReqConnError
        responses_lib.add(
            responses_lib.POST, TEST_WEBHOOK_URL,
            body=ReqConnError("refused")
        )
        ar = send_slack_alert(_slack_config(), _result())
        assert ar.success is False
        assert ar.channel == "slack"

    def test_disabled_slack_returns_failure_without_request(self) -> None:
        """When Slack is disabled, no HTTP request is made and success=False."""
        ar = send_slack_alert(_slack_config(enabled=False), _result())
        assert ar.success is False
        assert ar.error is None  # not an error, just disabled

    def test_empty_webhook_url_returns_failure(self) -> None:
        """An empty webhook URL returns a failed AlertResult."""
        ar = send_slack_alert(_slack_config(webhook_url=""), _result())
        assert ar.success is False

    def test_channel_is_slack(self) -> None:
        ar = send_slack_alert(_slack_config(enabled=False), _result())
        assert ar.channel == "slack"


# ---------------------------------------------------------------------------
# send_slack_summary
# ---------------------------------------------------------------------------


class TestSendSlackSummary:
    """Tests for send_slack_summary."""

    @responses_lib.activate
    def test_sends_single_request(self) -> None:
        """Only one POST request is sent even for multiple results."""
        responses_lib.add(responses_lib.POST, TEST_WEBHOOK_URL, body=b"ok", status=200)
        results = [_result(key="a"), _result(key="b")]
        send_slack_summary(_slack_config(), results)
        assert len(responses_lib.calls) == 1

    @responses_lib.activate
    def test_returns_success_on_200(self) -> None:
        responses_lib.add(responses_lib.POST, TEST_WEBHOOK_URL, body=b"ok", status=200)
        ar = send_slack_summary(_slack_config(), [_result()])
        assert ar.success is True

    @responses_lib.activate
    def test_returns_failure_on_http_error(self) -> None:
        responses_lib.add(responses_lib.POST, TEST_WEBHOOK_URL, status=500, body=b"err")
        ar = send_slack_summary(_slack_config(), [_result()])
        assert ar.success is False

    def test_disabled_returns_failure(self) -> None:
        ar = send_slack_summary(_slack_config(enabled=False), [_result()])
        assert ar.success is False

    def test_empty_webhook_returns_failure(self) -> None:
        ar = send_slack_summary(_slack_config(webhook_url=""), [_result()])
        assert ar.success is False

    @responses_lib.activate
    def test_channel_is_slack(self) -> None:
        responses_lib.add(responses_lib.POST, TEST_WEBHOOK_URL, body=b"ok", status=200)
        ar = send_slack_summary(_slack_config(), [_result()])
        assert ar.channel == "slack"


# ---------------------------------------------------------------------------
# send_email_alert
# ---------------------------------------------------------------------------


class TestSendEmailAlert:
    """Tests for send_email_alert using a mocked SMTP connection."""

    def test_disabled_email_returns_failure(self) -> None:
        """When email is disabled, success=False without SMTP call."""
        ar = send_email_alert(_email_config(enabled=False), _result())
        assert ar.success is False
        assert ar.channel == "email"

    @patch("health_data_watchdog.alerting._send_smtp")
    def test_enabled_email_returns_success(self, mock_smtp: MagicMock) -> None:
        """A successful SMTP delivery returns success=True."""
        mock_smtp.return_value = None
        ar = send_email_alert(_email_config(), _result())
        assert ar.success is True
        assert ar.channel == "email"

    @patch("health_data_watchdog.alerting._send_smtp")
    def test_smtp_called_once(self, mock_smtp: MagicMock) -> None:
        """_send_smtp is called exactly once per alert."""
        mock_smtp.return_value = None
        send_email_alert(_email_config(), _result())
        mock_smtp.assert_called_once()

    @patch("health_data_watchdog.alerting._send_smtp")
    def test_dataset_key_in_result(self, mock_smtp: MagicMock) -> None:
        """The dataset_key is set on the AlertResult."""
        mock_smtp.return_value = None
        ar = send_email_alert(_email_config(), _result(key="cdc_flu"))
        assert ar.dataset_key == "cdc_flu"

    @patch("health_data_watchdog.alerting._send_smtp")
    def test_smtp_exception_returns_failure(self, mock_smtp: MagicMock) -> None:
        """An SMTP exception produces a failed AlertResult."""
        mock_smtp.side_effect = smtplib.SMTPException("connection refused")
        ar = send_email_alert(_email_config(), _result())
        assert ar.success is False
        assert ar.error is not None

    @patch("health_data_watchdog.alerting._send_smtp")
    def test_error_message_contains_exception_text(
        self, mock_smtp: MagicMock
    ) -> None:
        """The AlertResult message contains information about the failure."""
        mock_smtp.side_effect = smtplib.SMTPException("auth failed")
        ar = send_email_alert(_email_config(), _result())
        assert "auth failed" in ar.message or "failed" in ar.message.lower()

    @patch("health_data_watchdog.alerting._send_smtp")
    def test_smtp_receives_email_config(
        self, mock_smtp: MagicMock
    ) -> None:
        """The email config is forwarded to _send_smtp."""
        mock_smtp.return_value = None
        cfg = _email_config(from_address="from@example.com")
        send_email_alert(cfg, _result())
        call_args = mock_smtp.call_args
        assert call_args is not None
        passed_config = call_args[0][0]  # first positional arg
        assert passed_config.from_address == "from@example.com"


# ---------------------------------------------------------------------------
# send_email_summary
# ---------------------------------------------------------------------------


class TestSendEmailSummary:
    """Tests for send_email_summary."""

    def test_disabled_returns_failure(self) -> None:
        ar = send_email_summary(_email_config(enabled=False), [_result()])
        assert ar.success is False

    @patch("health_data_watchdog.alerting._send_smtp")
    def test_enabled_returns_success(self, mock_smtp: MagicMock) -> None:
        mock_smtp.return_value = None
        ar = send_email_summary(_email_config(), [_result()])
        assert ar.success is True

    @patch("health_data_watchdog.alerting._send_smtp")
    def test_smtp_called_once_for_multiple_results(
        self, mock_smtp: MagicMock
    ) -> None:
        """Only one SMTP call is made even for multiple results."""
        mock_smtp.return_value = None
        send_email_summary(_email_config(), [_result(key="a"), _result(key="b")])
        mock_smtp.assert_called_once()

    @patch("health_data_watchdog.alerting._send_smtp")
    def test_smtp_exception_returns_failure(self, mock_smtp: MagicMock) -> None:
        mock_smtp.side_effect = OSError("network unreachable")
        ar = send_email_summary(_email_config(), [_result()])
        assert ar.success is False
        assert ar.error is not None

    @patch("health_data_watchdog.alerting._send_smtp")
    def test_channel_is_email(self, mock_smtp: MagicMock) -> None:
        mock_smtp.return_value = None
        ar = send_email_summary(_email_config(), [_result()])
        assert ar.channel == "email"


# ---------------------------------------------------------------------------
# _build_mime_message
# ---------------------------------------------------------------------------


class TestBuildMimeMessage:
    """Tests for the _build_mime_message helper."""

    def test_subject_is_set(self) -> None:
        cfg = _email_config()
        msg = _build_mime_message(cfg, "Test subject", "plain body", None)
        assert msg["Subject"] == "Test subject"

    def test_from_is_set(self) -> None:
        cfg = _email_config(from_address="from@test.com")
        msg = _build_mime_message(cfg, "s", "b", None)
        assert msg["From"] == "from@test.com"

    def test_to_contains_recipients(self) -> None:
        cfg = _email_config(to_addresses=["a@test.com", "b@test.com"])
        msg = _build_mime_message(cfg, "s", "b", None)
        assert "a@test.com" in msg["To"]
        assert "b@test.com" in msg["To"]

    def test_plain_text_part_present(self) -> None:
        cfg = _email_config()
        msg = _build_mime_message(cfg, "s", "plain body here", None)
        payloads = msg.get_payload()
        assert any(
            isinstance(p, object)
            and getattr(p, "get_content_type", lambda: "")() == "text/plain"
            for p in payloads
        )

    def test_html_part_present_when_provided(self) -> None:
        cfg = _email_config()
        msg = _build_mime_message(cfg, "s", "plain", "<html>hi</html>")
        payloads = msg.get_payload()
        assert any(
            isinstance(p, object)
            and getattr(p, "get_content_type", lambda: "")() == "text/html"
            for p in payloads
        )

    def test_no_html_when_none(self) -> None:
        cfg = _email_config()
        msg = _build_mime_message(cfg, "s", "plain", None)
        payloads = msg.get_payload()
        html_parts = [
            p for p in payloads
            if getattr(p, "get_content_type", lambda: "")() == "text/html"
        ]
        assert len(html_parts) == 0


# ---------------------------------------------------------------------------
# AlertManager.should_alert
# ---------------------------------------------------------------------------


class TestAlertManagerShouldAlert:
    """Tests for AlertManager.should_alert."""

    def test_critical_meets_warning_threshold(self) -> None:
        manager = AlertManager(_config(), min_severity=SEVERITY_WARNING)
        assert manager.should_alert(_result(severity=SEVERITY_CRITICAL)) is True

    def test_warning_meets_warning_threshold(self) -> None:
        manager = AlertManager(_config(), min_severity=SEVERITY_WARNING)
        assert manager.should_alert(_result(severity=SEVERITY_WARNING)) is True

    def test_info_does_not_meet_warning_threshold(self) -> None:
        manager = AlertManager(_config(), min_severity=SEVERITY_WARNING)
        assert manager.should_alert(_result(severity=SEVERITY_INFO)) is False

    def test_info_meets_info_threshold(self) -> None:
        manager = AlertManager(_config(), min_severity=SEVERITY_INFO)
        assert manager.should_alert(_result(severity=SEVERITY_INFO)) is True

    def test_critical_meets_critical_threshold(self) -> None:
        manager = AlertManager(_config(), min_severity=SEVERITY_CRITICAL)
        assert manager.should_alert(_result(severity=SEVERITY_CRITICAL)) is True

    def test_warning_does_not_meet_critical_threshold(self) -> None:
        manager = AlertManager(_config(), min_severity=SEVERITY_CRITICAL)
        assert manager.should_alert(_result(severity=SEVERITY_WARNING)) is False


# ---------------------------------------------------------------------------
# AlertManager.send_alerts
# ---------------------------------------------------------------------------


class TestAlertManagerSendAlerts:
    """Tests for AlertManager.send_alerts."""

    def test_no_channels_enabled_returns_empty_list(self) -> None:
        """With all channels disabled, no alerts are sent."""
        manager = AlertManager(_config(slack_enabled=False, email_enabled=False))
        results = manager.send_alerts([_result(severity=SEVERITY_CRITICAL)])
        assert results == []

    def test_below_threshold_results_are_skipped(self) -> None:
        """Results below the severity threshold are silently ignored."""
        manager = AlertManager(
            _config(slack_enabled=True, email_enabled=False),
            min_severity=SEVERITY_CRITICAL,
        )
        with responses_lib.RequestsMock():
            results = manager.send_alerts([_result(severity=SEVERITY_WARNING)])
        assert results == []

    @responses_lib.activate
    def test_slack_alert_sent_for_qualifying_result(self) -> None:
        """A Slack alert is dispatched for each qualifying result."""
        responses_lib.add(
            responses_lib.POST, TEST_WEBHOOK_URL, body=b"ok", status=200
        )
        cfg = Config(
            slack=_slack_config(enabled=True, webhook_url=TEST_WEBHOOK_URL),
            email=_email_config(enabled=False),
        )
        manager = AlertManager(cfg, min_severity=SEVERITY_WARNING)
        alert_results = manager.send_alerts([_result(severity=SEVERITY_CRITICAL)])
        assert len(alert_results) == 1
        assert alert_results[0].channel == "slack"
        assert alert_results[0].success is True

    @patch("health_data_watchdog.alerting._send_smtp")
    def test_email_alert_sent_for_qualifying_result(
        self, mock_smtp: MagicMock
    ) -> None:
        """An email alert is dispatched for each qualifying result."""
        mock_smtp.return_value = None
        cfg = Config(
            slack=_slack_config(enabled=False),
            email=_email_config(enabled=True),
        )
        manager = AlertManager(cfg, min_severity=SEVERITY_WARNING)
        alert_results = manager.send_alerts([_result(severity=SEVERITY_CRITICAL)])
        assert len(alert_results) == 1
        assert alert_results[0].channel == "email"
        assert alert_results[0].success is True

    @responses_lib.activate
    @patch("health_data_watchdog.alerting._send_smtp")
    def test_both_channels_produce_two_results(
        self, mock_smtp: MagicMock
    ) -> None:
        """When both channels are enabled, two AlertResults are produced per result."""
        responses_lib.add(
            responses_lib.POST, TEST_WEBHOOK_URL, body=b"ok", status=200
        )
        mock_smtp.return_value = None
        cfg = Config(
            slack=_slack_config(enabled=True, webhook_url=TEST_WEBHOOK_URL),
            email=_email_config(enabled=True),
        )
        manager = AlertManager(cfg, min_severity=SEVERITY_INFO)
        alert_results = manager.send_alerts([_result(severity=SEVERITY_INFO)])
        channels = [ar.channel for ar in alert_results]
        assert "slack" in channels
        assert "email" in channels
        assert len(alert_results) == 2

    @responses_lib.activate
    def test_multiple_results_send_multiple_alerts(self) -> None:
        """Each qualifying result sends its own Slack alert."""
        for _ in range(3):
            responses_lib.add(
                responses_lib.POST, TEST_WEBHOOK_URL, body=b"ok", status=200
            )
        cfg = Config(
            slack=_slack_config(enabled=True, webhook_url=TEST_WEBHOOK_URL),
            email=_email_config(enabled=False),
        )
        manager = AlertManager(cfg, min_severity=SEVERITY_INFO)
        results = [
            _result(key="a", severity=SEVERITY_CRITICAL),
            _result(key="b", severity=SEVERITY_WARNING),
            _result(key="c", severity=SEVERITY_INFO),
        ]
        alert_results = manager.send_alerts(results)
        assert len(alert_results) == 3

    @responses_lib.activate
    def test_unchanged_result_below_warning_threshold_skipped(self) -> None:
        """Unchanged (info) results are skipped when threshold is warning."""
        cfg = Config(
            slack=_slack_config(enabled=True, webhook_url=TEST_WEBHOOK_URL),
            email=_email_config(enabled=False),
        )
        manager = AlertManager(cfg, min_severity=SEVERITY_WARNING)
        unchanged = _result(
            change_type=CHANGE_TYPE_UNCHANGED, severity=SEVERITY_INFO
        )
        alert_results = manager.send_alerts([unchanged])
        assert alert_results == []
        assert len(responses_lib.calls) == 0


# ---------------------------------------------------------------------------
# AlertManager.send_summary_alerts
# ---------------------------------------------------------------------------


class TestAlertManagerSendSummaryAlerts:
    """Tests for AlertManager.send_summary_alerts."""

    def test_empty_results_returns_empty_when_no_channels(
        self,
    ) -> None:
        """No alerts returned when there are no results and no channels."""
        manager = AlertManager(
            _config(slack_enabled=False, email_enabled=False)
        )
        results = manager.send_summary_alerts([])
        assert results == []

    @responses_lib.activate
    def test_single_slack_summary_sent_for_multiple_results(self) -> None:
        """One Slack summary is sent regardless of result count."""
        responses_lib.add(
            responses_lib.POST, TEST_WEBHOOK_URL, body=b"ok", status=200
        )
        cfg = Config(
            slack=_slack_config(enabled=True, webhook_url=TEST_WEBHOOK_URL),
            email=_email_config(enabled=False),
        )
        manager = AlertManager(cfg, min_severity=SEVERITY_INFO)
        manager.send_summary_alerts([
            _result(key="a"),
            _result(key="b"),
            _result(key="c"),
        ])
        assert len(responses_lib.calls) == 1

    @patch("health_data_watchdog.alerting._send_smtp")
    def test_single_email_summary_sent(
        self, mock_smtp: MagicMock
    ) -> None:
        """One email summary is sent regardless of result count."""
        mock_smtp.return_value = None
        cfg = Config(
            slack=_slack_config(enabled=False),
            email=_email_config(enabled=True),
        )
        manager = AlertManager(cfg, min_severity=SEVERITY_INFO)
        manager.send_summary_alerts([_result(key="a"), _result(key="b")])
        mock_smtp.assert_called_once()

    @responses_lib.activate
    @patch("health_data_watchdog.alerting._send_smtp")
    def test_both_channels_return_two_summary_results(
        self, mock_smtp: MagicMock
    ) -> None:
        """Summary mode returns one AlertResult per enabled channel."""
        responses_lib.add(
            responses_lib.POST, TEST_WEBHOOK_URL, body=b"ok", status=200
        )
        mock_smtp.return_value = None
        cfg = Config(
            slack=_slack_config(enabled=True, webhook_url=TEST_WEBHOOK_URL),
            email=_email_config(enabled=True),
        )
        manager = AlertManager(cfg, min_severity=SEVERITY_INFO)
        results_list = manager.send_summary_alerts([_result()])
        assert len(results_list) == 2
        channels = {ar.channel for ar in results_list}
        assert channels == {"slack", "email"}

    @responses_lib.activate
    def test_summary_only_includes_qualifying_results(self) -> None:
        """The summary Slack payload only includes qualifying results."""
        responses_lib.add(
            responses_lib.POST, TEST_WEBHOOK_URL, body=b"ok", status=200
        )
        cfg = Config(
            slack=_slack_config(enabled=True, webhook_url=TEST_WEBHOOK_URL),
            email=_email_config(enabled=False),
        )
        manager = AlertManager(cfg, min_severity=SEVERITY_WARNING)
        results_list = [
            _result(key="critical_ds", severity=SEVERITY_CRITICAL),
            _result(key="info_ds", severity=SEVERITY_INFO),
        ]
        manager.send_summary_alerts(results_list)
        request_body = json.loads(responses_lib.calls[0].request.body)
        all_text = json.dumps(request_body)
        assert "critical_ds" in all_text
        assert "info_ds" not in all_text


# ---------------------------------------------------------------------------
# Integration: full alert pipeline
# ---------------------------------------------------------------------------


class TestAlertIntegration:
    """Integration tests for the full alerting pipeline."""

    @responses_lib.activate
    @patch("health_data_watchdog.alerting._send_smtp")
    def test_critical_deletion_triggers_all_channels(
        self, mock_smtp: MagicMock
    ) -> None:
        """A CRITICAL dataset deletion triggers both Slack and email alerts."""
        responses_lib.add(
            responses_lib.POST, TEST_WEBHOOK_URL, body=b"ok", status=200
        )
        mock_smtp.return_value = None
        cfg = Config(
            slack=_slack_config(enabled=True, webhook_url=TEST_WEBHOOK_URL),
            email=_email_config(enabled=True),
        )
        manager = AlertManager(cfg, min_severity=SEVERITY_WARNING)
        deletion_result = _result(
            key="cdc_covid_cases",
            change_type=CHANGE_TYPE_DELETION,
            severity=SEVERITY_CRITICAL,
            summary="Dataset was deleted!",
        )
        alert_results = manager.send_alerts([deletion_result])
        assert len(alert_results) == 2
        assert all(ar.success for ar in alert_results)

    @responses_lib.activate
    def test_slack_message_contains_deletion_info(self) -> None:
        """The Slack message for a deletion contains dataset name and type."""
        responses_lib.add(
            responses_lib.POST, TEST_WEBHOOK_URL, body=b"ok", status=200
        )
        result = _result(
            key="who_covid_global",
            change_type=CHANGE_TYPE_DELETION,
            severity=SEVERITY_CRITICAL,
        )
        send_slack_alert(_slack_config(), result)
        body = json.loads(responses_lib.calls[0].request.body)
        all_text = json.dumps(body)
        assert "who_covid_global" in all_text

    @patch("health_data_watchdog.alerting._send_smtp")
    def test_email_subject_for_critical_schema_change(
        self, mock_smtp: MagicMock
    ) -> None:
        """Email subject for a critical schema change is appropriately titled."""
        mock_smtp.return_value = None
        result = _result(
            key="cdc_vaccination",
            change_type=CHANGE_TYPE_SCHEMA,
            severity=SEVERITY_CRITICAL,
        )
        send_email_alert(_email_config(), result)
        call_args = mock_smtp.call_args
        mime_msg = call_args[0][1]  # second positional arg is the MIME message
        assert "CRITICAL" in mime_msg["Subject"]
        assert "cdc_vaccination" in mime_msg["Subject"]
