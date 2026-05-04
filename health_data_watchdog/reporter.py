"""Changelog renderer for Health Data Watchdog.

This module renders human-readable changelogs from :class:`~health_data_watchdog.differ.DiffResult`
objects and :class:`~health_data_watchdog.store.ChangeRecord` objects using:

- **Rich** — for colourful, well-formatted terminal output with tables and panels.
- **Markdown** — for exportable timestamped diff reports suitable for sharing or
  archiving.

Typical usage::

    from health_data_watchdog.reporter import Reporter
    from health_data_watchdog.differ import DatasetDiffer
    import pandas as pd

    old_df = pd.read_csv("old_snapshot.csv")
    new_df = pd.read_csv("new_snapshot.csv")
    result = DatasetDiffer().diff("my_dataset", old_df, new_df)

    reporter = Reporter()
    reporter.print_diff_result(result)
    reporter.write_markdown_report([result], output_path="report.md")
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, List, Optional, Sequence

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box

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

# For reading ChangeRecord objects from the store
try:
    from health_data_watchdog.store import ChangeRecord
except ImportError:  # pragma: no cover
    ChangeRecord = None  # type: ignore[assignment,misc]


# ---------------------------------------------------------------------------
# Colour and icon constants
# ---------------------------------------------------------------------------

_SEVERITY_STYLE: dict = {
    SEVERITY_CRITICAL: "bold red",
    SEVERITY_WARNING: "bold yellow",
    SEVERITY_INFO: "bold green",
}

_SEVERITY_ICON: dict = {
    SEVERITY_CRITICAL: "🔴",
    SEVERITY_WARNING: "🟡",
    SEVERITY_INFO: "🟢",
}

_CHANGE_TYPE_STYLE: dict = {
    CHANGE_TYPE_NEW: "bold cyan",
    CHANGE_TYPE_DELETION: "bold red",
    CHANGE_TYPE_SCHEMA: "bold magenta",
    CHANGE_TYPE_CONTENT: "bold yellow",
    CHANGE_TYPE_UNCHANGED: "dim",
}


# ---------------------------------------------------------------------------
# Helper formatting functions
# ---------------------------------------------------------------------------


def _severity_label(severity: str) -> str:
    """Return a Rich-styled severity badge string."""
    style = _SEVERITY_STYLE.get(severity, "")
    icon = _SEVERITY_ICON.get(severity, "")
    return f"[{style}]{icon} {severity.upper()}[/{style}]"


def _change_type_label(change_type: str) -> str:
    """Return a Rich-styled change type label."""
    style = _CHANGE_TYPE_STYLE.get(change_type, "")
    return f"[{style}]{change_type.upper()}[/{style}]"


def _fmt_delta(delta: int) -> str:
    """Format a row/column delta with a leading sign."""
    if delta > 0:
        return f"[green]+{delta}[/green]"
    elif delta < 0:
        return f"[red]{delta}[/red]"
    return "[dim]0[/dim]"


def _markdown_severity(severity: str) -> str:
    """Return a plain Markdown severity badge."""
    icons = {
        SEVERITY_CRITICAL: "🔴 **CRITICAL**",
        SEVERITY_WARNING: "🟡 **WARNING**",
        SEVERITY_INFO: "🟢 **INFO**",
    }
    return icons.get(severity, severity.upper())


def _now_utc_str() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Reporter class
# ---------------------------------------------------------------------------


class Reporter:
    """Renders diff results and change records to the terminal and Markdown.

    Args:
        console: A :class:`rich.console.Console` instance.  A default
            console is created if not provided.
        no_color: If ``True``, disable Rich colour output.
    """

    def __init__(
        self,
        console: Optional[Console] = None,
        no_color: bool = False,
    ) -> None:
        if console is not None:
            self._console = console
        else:
            self._console = Console(no_color=no_color)

    # ------------------------------------------------------------------
    # Terminal output — single DiffResult
    # ------------------------------------------------------------------

    def print_diff_result(self, result: DiffResult) -> None:
        """Print a full diff result to the terminal.

        Renders a panel header, a statistics table, column-change details,
        and a sample of row-level value changes.

        Args:
            result: The :class:`~health_data_watchdog.differ.DiffResult`
                to render.
        """
        self._console.print()
        self._print_diff_panel_header(result)

        if result.change_type == CHANGE_TYPE_UNCHANGED:
            self._console.print(
                f"  [dim]No changes detected for [bold]{result.dataset_key}[/bold].[/dim]"
            )
            self._console.print()
            return

        if result.change_type == CHANGE_TYPE_DELETION:
            self._console.print(
                f"  [bold red]⚠ Dataset '[/bold red]"
                f"[bold]{result.dataset_key}[/bold]"
                f"[bold red]' has been DELETED or is no longer available![/bold red]"
            )
            self._console.print(
                f"  Previously had {result.old_row_count} rows "
                f"and {len(result.old_columns)} columns."
            )
            self._console.print()
            return

        if result.change_type == CHANGE_TYPE_NEW:
            self._console.print(
                f"  [bold cyan]✦ New dataset '[/bold cyan]"
                f"[bold]{result.dataset_key}[/bold]"
                f"[bold cyan]' detected![/bold cyan]"
            )
            self._console.print(
                f"  Contains {result.new_row_count} rows "
                f"and {len(result.new_columns)} columns."
            )
            self._console.print()
            return

        # Stats table
        self._print_stats_table(result)

        # Column changes
        if result.column_changes:
            self._print_column_changes_table(result.column_changes)

        # Value changes sample
        if result.value_changes:
            self._print_value_changes_table(result)

        self._console.print()

    def _print_diff_panel_header(self, result: DiffResult) -> None:
        """Print a Rich panel header for a diff result."""
        severity_str = _severity_label(result.severity)
        change_type_str = _change_type_label(result.change_type)
        title = Text.from_markup(
            f"{severity_str}  {change_type_str}  "
            f"[bold white]{result.dataset_key}[/bold white]"
        )
        self._console.print(
            Panel(title, expand=False, border_style=_SEVERITY_STYLE.get(result.severity, ""))
        )
        if result.summary:
            self._console.print(f"  {result.summary}")

    def _print_stats_table(self, result: DiffResult) -> None:
        """Print a statistics table for a content/schema change."""
        table = Table(
            title="Change Statistics",
            box=box.SIMPLE_HEAD,
            show_header=True,
            header_style="bold",
            padding=(0, 1),
        )
        table.add_column("Metric", style="bold", no_wrap=True)
        table.add_column("Old", justify="right")
        table.add_column("New", justify="right")
        table.add_column("Delta", justify="right")

        table.add_row(
            "Row count",
            str(result.old_row_count) if result.old_row_count >= 0 else "—",
            str(result.new_row_count) if result.new_row_count >= 0 else "—",
            _fmt_delta(result.row_delta),
        )
        table.add_row(
            "Column count",
            str(len(result.old_columns)),
            str(len(result.new_columns)),
            _fmt_delta(len(result.new_columns) - len(result.old_columns)),
        )
        if result.rows_added or result.rows_removed or result.rows_modified:
            table.add_row(
                "Rows added",
                "—",
                str(result.rows_added),
                _fmt_delta(result.rows_added),
            )
            table.add_row(
                "Rows removed",
                str(result.rows_removed),
                "—",
                _fmt_delta(-result.rows_removed),
            )
            table.add_row(
                "Rows modified",
                "—",
                str(result.rows_modified),
                "[yellow]~" + str(result.rows_modified) + "[/yellow]",
            )
        table.add_row(
            "Row change %",
            "—",
            f"{result.row_change_pct:.2f}%",
            "",
        )

        self._console.print(table)

    def _print_column_changes_table(self, changes: List[ColumnChange]) -> None:
        """Print a table of column-level changes."""
        table = Table(
            title="Column Changes",
            box=box.SIMPLE_HEAD,
            show_header=True,
            header_style="bold",
            padding=(0, 1),
        )
        table.add_column("Column", style="bold", no_wrap=True)
        table.add_column("Change", no_wrap=True)
        table.add_column("Old Type")
        table.add_column("New Type")

        style_map = {
            "removed": "red",
            "added": "green",
            "type_changed": "yellow",
        }

        for change in changes:
            style = style_map.get(change.change_type, "")
            table.add_row(
                f"[{style}]{change.name}[/{style}]",
                f"[{style}]{change.change_type.replace('_', ' ').upper()}[/{style}]",
                change.old_dtype or "—",
                change.new_dtype or "—",
            )

        self._console.print(table)

    def _print_value_changes_table(self, result: DiffResult) -> None:
        """Print a sample of row-level value changes."""
        if not result.value_changes:
            return

        table = Table(
            title=f"Value Changes (sample — {len(result.value_changes)} row(s))",
            box=box.SIMPLE_HEAD,
            show_header=True,
            header_style="bold",
            padding=(0, 1),
        )
        table.add_column("Row", justify="right", no_wrap=True)
        table.add_column("Column", no_wrap=True)
        table.add_column("Old Value")
        table.add_column("New Value")

        for row_idx, col_changes in result.value_changes.items():
            for col, (old_val, new_val) in col_changes.items():
                table.add_row(
                    str(row_idx),
                    str(col),
                    f"[red]{_truncate(str(old_val))}[/red]",
                    f"[green]{_truncate(str(new_val))}[/green]",
                )

        self._console.print(table)

    # ------------------------------------------------------------------
    # Terminal output — multiple results / change records
    # ------------------------------------------------------------------

    def print_diff_results(self, results: Sequence[DiffResult]) -> None:
        """Print a list of diff results to the terminal.

        Args:
            results: Sequence of :class:`~health_data_watchdog.differ.DiffResult`
                objects to render.
        """
        if not results:
            self._console.print("[dim]No diff results to display.[/dim]")
            return

        for result in results:
            self.print_diff_result(result)

    def print_change_records(
        self,
        records: Sequence,
        *,
        title: str = "Change History",
        limit: Optional[int] = None,
    ) -> None:
        """Print a summary table of :class:`~health_data_watchdog.store.ChangeRecord` objects.

        Args:
            records: Sequence of ChangeRecord objects.
            title: Table title to display.
            limit: If set, display only the first *limit* records.
        """
        display = list(records)[:limit] if limit else list(records)

        if not display:
            self._console.print("[dim]No change records found.[/dim]")
            return

        table = Table(
            title=title,
            box=box.SIMPLE_HEAD,
            show_header=True,
            header_style="bold",
            padding=(0, 1),
        )
        table.add_column("ID", justify="right", no_wrap=True)
        table.add_column("Dataset Key", no_wrap=True)
        table.add_column("Detected At", no_wrap=True)
        table.add_column("Type", no_wrap=True)
        table.add_column("Severity", no_wrap=True)
        table.add_column("Summary")

        for rec in display:
            severity_style = _SEVERITY_STYLE.get(getattr(rec, "severity", ""), "")
            change_type_style = _CHANGE_TYPE_STYLE.get(
                getattr(rec, "change_type", ""), ""
            )
            table.add_row(
                str(getattr(rec, "id", "—")),
                str(getattr(rec, "dataset_key", "—")),
                str(getattr(rec, "detected_at", "—")),
                (
                    f"[{change_type_style}]"
                    f"{getattr(rec, 'change_type', '—').upper()}"
                    f"[/{change_type_style}]"
                ),
                (
                    f"[{severity_style}]"
                    f"{getattr(rec, 'severity', '—').upper()}"
                    f"[/{severity_style}]"
                ),
                _truncate(str(getattr(rec, "summary", "—")), 80),
            )

        self._console.print(table)

    def print_summary_table(self, results: Sequence[DiffResult]) -> None:
        """Print a compact multi-dataset summary table.

        Args:
            results: Sequence of diff results to summarise.
        """
        if not results:
            self._console.print("[dim]No results to summarise.[/dim]")
            return

        table = Table(
            title="Diff Summary",
            box=box.SIMPLE_HEAD,
            show_header=True,
            header_style="bold",
            padding=(0, 1),
        )
        table.add_column("Dataset", no_wrap=True)
        table.add_column("Change Type", no_wrap=True)
        table.add_column("Severity", no_wrap=True)
        table.add_column("Old Rows", justify="right")
        table.add_column("New Rows", justify="right")
        table.add_column("Row Δ", justify="right")
        table.add_column("Cols Removed", justify="right")
        table.add_column("Cols Added", justify="right")

        for result in results:
            severity_style = _SEVERITY_STYLE.get(result.severity, "")
            change_type_style = _CHANGE_TYPE_STYLE.get(result.change_type, "")
            table.add_row(
                result.dataset_key,
                f"[{change_type_style}]{result.change_type.upper()}[/{change_type_style}]",
                f"[{severity_style}]{result.severity.upper()}[/{severity_style}]",
                str(result.old_row_count) if result.old_row_count >= 0 else "—",
                str(result.new_row_count) if result.new_row_count >= 0 else "—",
                _fmt_delta(result.row_delta),
                str(len(result.removed_columns)) if result.removed_columns else "0",
                str(len(result.added_columns)) if result.added_columns else "0",
            )

        self._console.print(table)

    # ------------------------------------------------------------------
    # Markdown report generation
    # ------------------------------------------------------------------

    def write_markdown_report(
        self,
        results: Sequence[DiffResult],
        output_path: Optional[Path] = None,
        *,
        title: str = "Health Data Watchdog — Change Report",
        append: bool = False,
    ) -> str:
        """Render diff results as a Markdown report.

        Args:
            results: Sequence of :class:`~health_data_watchdog.differ.DiffResult`
                objects to include.
            output_path: If provided, write the Markdown to this file.  The
                parent directory is created if necessary.  Pass ``None`` to
                return the string without writing.
            title: Top-level heading for the report.
            append: If ``True`` and *output_path* exists, append rather than
                overwrite.

        Returns:
            The Markdown report as a string.
        """
        lines: List[str] = []
        now = _now_utc_str()

        lines.append(f"# {title}")
        lines.append("")
        lines.append(f"*Generated at: {now}*")
        lines.append("")
        lines.append("---")
        lines.append("")

        if not results:
            lines.append("*No changes detected.*")
            lines.append("")
        else:
            # Summary table
            lines.append("## Summary")
            lines.append("")
            lines.append(
                "| Dataset | Change Type | Severity | Old Rows | New Rows | Row Δ |"
            )
            lines.append(
                "| ------- | ----------- | -------- | -------: | -------: | ----: |"
            )
            for result in results:
                old_rows = str(result.old_row_count) if result.old_row_count >= 0 else "—"
                new_rows = str(result.new_row_count) if result.new_row_count >= 0 else "—"
                delta = f"+{result.row_delta}" if result.row_delta > 0 else str(result.row_delta)
                lines.append(
                    f"| `{result.dataset_key}` "
                    f"| {result.change_type.upper()} "
                    f"| {_markdown_severity(result.severity)} "
                    f"| {old_rows} "
                    f"| {new_rows} "
                    f"| {delta} |"
                )
            lines.append("")
            lines.append("---")
            lines.append("")

            # Detailed sections per dataset
            for result in results:
                lines.extend(self._render_markdown_result(result))

        markdown = "\n".join(lines)

        if output_path is not None:
            path = Path(output_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            mode = "a" if append else "w"
            with open(path, mode, encoding="utf-8") as fh:
                fh.write(markdown)
                fh.write("\n")

        return markdown

    def _render_markdown_result(self, result: DiffResult) -> List[str]:
        """Render a single DiffResult as Markdown lines.

        Args:
            result: The diff result to render.

        Returns:
            List of Markdown lines.
        """
        lines: List[str] = []
        severity_md = _markdown_severity(result.severity)

        lines.append(f"## `{result.dataset_key}`")
        lines.append("")
        lines.append(f"**Severity:** {severity_md}  ")
        lines.append(f"**Change type:** `{result.change_type.upper()}`  ")
        lines.append(f"**Summary:** {result.summary}")
        lines.append("")

        if result.change_type == CHANGE_TYPE_UNCHANGED:
            lines.append("*No changes detected.*")
            lines.append("")
            lines.append("---")
            lines.append("")
            return lines

        if result.change_type == CHANGE_TYPE_DELETION:
            lines.append(
                f"> ⚠ **This dataset has been DELETED or is no longer accessible.**"
            )
            lines.append(
                f"> It previously contained **{result.old_row_count}** rows "
                f"and **{len(result.old_columns)}** columns."
            )
            lines.append("")
            lines.append("---")
            lines.append("")
            return lines

        if result.change_type == CHANGE_TYPE_NEW:
            lines.append(
                f"> ✦ **New dataset detected.**"
            )
            lines.append(
                f"> Contains **{result.new_row_count}** rows "
                f"and **{len(result.new_columns)}** columns."
            )
            lines.append("")
            lines.append("---")
            lines.append("")
            return lines

        # Statistics table
        lines.append("### Statistics")
        lines.append("")
        lines.append("| Metric | Old | New | Delta |")
        lines.append("| ------ | --: | --: | ----: |")

        old_rows = str(result.old_row_count) if result.old_row_count >= 0 else "—"
        new_rows = str(result.new_row_count) if result.new_row_count >= 0 else "—"
        row_delta_str = (
            f"+{result.row_delta}" if result.row_delta > 0 else str(result.row_delta)
        )
        old_cols = str(len(result.old_columns))
        new_cols = str(len(result.new_columns))
        col_delta = len(result.new_columns) - len(result.old_columns)
        col_delta_str = f"+{col_delta}" if col_delta > 0 else str(col_delta)

        lines.append(f"| Row count | {old_rows} | {new_rows} | {row_delta_str} |")
        lines.append(f"| Column count | {old_cols} | {new_cols} | {col_delta_str} |")
        lines.append(f"| Rows added | — | {result.rows_added} | +{result.rows_added} |")
        lines.append(f"| Rows removed | {result.rows_removed} | — | -{result.rows_removed} |")
        lines.append(
            f"| Rows modified | — | {result.rows_modified} | ~{result.rows_modified} |"
        )
        lines.append(f"| Row change % | — | {result.row_change_pct:.2f}% | — |")
        lines.append("")

        # Column changes
        if result.column_changes:
            lines.append("### Column Changes")
            lines.append("")
            lines.append("| Column | Change | Old Type | New Type |")
            lines.append("| ------ | ------ | -------- | -------- |")
            for change in result.column_changes:
                lines.append(
                    f"| `{change.name}` "
                    f"| {change.change_type.replace('_', ' ').upper()} "
                    f"| {change.old_dtype or '—'} "
                    f"| {change.new_dtype or '—'} |"
                )
            lines.append("")

        # Value changes sample
        if result.value_changes:
            lines.append(
                f"### Value Changes (sample — {len(result.value_changes)} row(s))"
            )
            lines.append("")
            lines.append("| Row | Column | Old Value | New Value |")
            lines.append("| --: | ------ | --------- | --------- |")
            for row_idx, col_changes in result.value_changes.items():
                for col, (old_val, new_val) in col_changes.items():
                    old_safe = _md_escape(str(old_val))
                    new_safe = _md_escape(str(new_val))
                    lines.append(
                        f"| {row_idx} | `{col}` | `{old_safe}` | `{new_safe}` |"
                    )
            lines.append("")

        # Raw diff JSON (collapsed)
        if result.diff_json:
            lines.append("### Raw Diff Data")
            lines.append("")
            lines.append("<details>")
            lines.append("<summary>Click to expand JSON diff</summary>")
            lines.append("")
            lines.append("```json")
            lines.append(
                json.dumps(result.diff_json, indent=2, default=str)
            )
            lines.append("```")
            lines.append("")
            lines.append("</details>")
            lines.append("")

        lines.append("---")
        lines.append("")
        return lines

    def write_change_records_markdown(
        self,
        records: Sequence,
        output_path: Optional[Path] = None,
        *,
        title: str = "Health Data Watchdog — Change History",
        limit: Optional[int] = None,
        append: bool = False,
    ) -> str:
        """Render a list of ChangeRecord objects as a Markdown report.

        Args:
            records: Sequence of
                :class:`~health_data_watchdog.store.ChangeRecord` objects.
            output_path: Optional path to write the report to.
            title: Report heading.
            limit: If set, include only the first *limit* records.
            append: If ``True``, append to an existing file.

        Returns:
            The Markdown report as a string.
        """
        display = list(records)[:limit] if limit else list(records)
        lines: List[str] = []
        now = _now_utc_str()

        lines.append(f"# {title}")
        lines.append("")
        lines.append(f"*Generated at: {now}*")
        lines.append("")
        lines.append("---")
        lines.append("")

        if not display:
            lines.append("*No change records found.*")
            lines.append("")
        else:
            lines.append(
                "| ID | Dataset | Detected At | Type | Severity | Summary |"
            )
            lines.append(
                "| -: | ------- | ----------- | ---- | -------- | ------- |"
            )
            for rec in display:
                rec_id = getattr(rec, "id", "—")
                key = getattr(rec, "dataset_key", "—")
                detected_at = getattr(rec, "detected_at", "—")
                change_type = getattr(rec, "change_type", "—")
                severity = getattr(rec, "severity", "—")
                summary = _md_escape(
                    _truncate(str(getattr(rec, "summary", "—")), 100)
                )
                lines.append(
                    f"| {rec_id} "
                    f"| `{key}` "
                    f"| {detected_at} "
                    f"| {change_type.upper()} "
                    f"| {_markdown_severity(severity)} "
                    f"| {summary} |"
                )
            lines.append("")

        markdown = "\n".join(lines)

        if output_path is not None:
            path = Path(output_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            mode = "a" if append else "w"
            with open(path, mode, encoding="utf-8") as fh:
                fh.write(markdown)
                fh.write("\n")

        return markdown


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------


def _truncate(s: str, max_len: int = 60) -> str:
    """Truncate a string to *max_len* characters, appending '…' if needed.

    Args:
        s: Input string.
        max_len: Maximum length before truncation.

    Returns:
        Possibly-truncated string.
    """
    if len(s) <= max_len:
        return s
    return s[: max_len - 1] + "…"


def _md_escape(s: str) -> str:
    """Escape pipe characters and backticks that would break Markdown tables.

    Args:
        s: Input string.

    Returns:
        String safe for use inside Markdown table cells.
    """
    return s.replace("|", "\\|").replace("`", "'")


# ---------------------------------------------------------------------------
# Module-level convenience functions
# ---------------------------------------------------------------------------


def print_diff_result(
    result: DiffResult,
    *,
    no_color: bool = False,
) -> None:
    """Print a single diff result to stdout.

    Convenience wrapper around :class:`Reporter` for scripts and the CLI.

    Args:
        result: The diff result to print.
        no_color: Disable Rich colour output.
    """
    Reporter(no_color=no_color).print_diff_result(result)


def write_markdown_report(
    results: Sequence[DiffResult],
    output_path: Optional[Path] = None,
    *,
    title: str = "Health Data Watchdog — Change Report",
    append: bool = False,
) -> str:
    """Write a Markdown report for the given diff results.

    Args:
        results: Diff results to include.
        output_path: Optional file path to write to.
        title: Report title.
        append: Append to existing file if ``True``.

    Returns:
        The Markdown content as a string.
    """
    return Reporter().write_markdown_report(
        results, output_path=output_path, title=title, append=append
    )
