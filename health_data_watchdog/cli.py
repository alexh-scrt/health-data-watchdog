"""Click-based CLI entry point for Health Data Watchdog.

This module wires all application modules together into a cohesive
command-line interface with the following top-level commands:

- ``fetch``     — Download datasets and persist snapshots.
- ``diff``      — Compare the two most recent snapshots for each dataset.
- ``watch``     — Run fetch+diff on a configurable schedule.
- ``report``    — Display stored change history from the database.
- ``configure`` — Manage the configuration file.
- ``datasets``  — List the built-in dataset registry.

The global ``--config`` option and ``HEALTH_WATCHDOG_CONFIG`` environment
variable control which TOML file is loaded.
"""

from __future__ import annotations

import logging
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import click
import schedule
from rich.console import Console
from rich.table import Table
from rich import box

from health_data_watchdog import __version__
from health_data_watchdog.alerting import AlertManager
from health_data_watchdog.config import (
    Config,
    ConfigValidationError,
    load_config,
    write_default_config,
)
from health_data_watchdog.datasets import (
    get_all_datasets,
    get_builtin_datasets,
    get_dataset_by_key,
    list_datasets_summary,
)
from health_data_watchdog.differ import DatasetDiffer, load_dataframe
from health_data_watchdog.fetcher import DatasetFetcher
from health_data_watchdog.reporter import Reporter
from health_data_watchdog.store import SnapshotStore

# ---------------------------------------------------------------------------
# Module-level Rich console (stderr so stdout stays clean for piping)
# ---------------------------------------------------------------------------

_console = Console(stderr=False)
_err_console = Console(stderr=True)


# ---------------------------------------------------------------------------
# Logging setup helper
# ---------------------------------------------------------------------------


def _configure_logging(log_level: str) -> None:
    """Configure the root logger with *log_level*.

    Args:
        log_level: One of ``DEBUG``, ``INFO``, ``WARNING``, ``ERROR``,
            ``CRITICAL``.
    """
    numeric = getattr(logging, log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=numeric,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


# ---------------------------------------------------------------------------
# Config loading helper (shared across commands)
# ---------------------------------------------------------------------------


def _load_config_or_exit(config_path: Optional[str]) -> Config:
    """Load and return the configuration, exiting with a friendly error on failure.

    Args:
        config_path: Path string from the ``--config`` option, or ``None``.

    Returns:
        Validated :class:`~health_data_watchdog.config.Config` instance.
    """
    path = Path(config_path) if config_path else None
    try:
        return load_config(path)
    except ConfigValidationError as exc:
        _err_console.print(f"[bold red]Configuration error:[/bold red] {exc}")
        sys.exit(1)
    except Exception as exc:
        _err_console.print(f"[bold red]Failed to load config:[/bold red] {exc}")
        sys.exit(1)


def _open_store(cfg: Config) -> SnapshotStore:
    """Open (or create) the SQLite store from the config.

    Args:
        cfg: Application configuration.

    Returns:
        An open :class:`~health_data_watchdog.store.SnapshotStore`.
    """
    db_path = cfg.general.db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return SnapshotStore(db_path)


# ---------------------------------------------------------------------------
# Root group
# ---------------------------------------------------------------------------


@click.group()
@click.version_option(version=__version__, prog_name="health-watchdog")
@click.option(
    "--config",
    "config_path",
    envvar="HEALTH_WATCHDOG_CONFIG",
    default=None,
    metavar="PATH",
    help="Path to the TOML configuration file.  Defaults to "
         "~/.config/health_watchdog/config.toml or $HEALTH_WATCHDOG_CONFIG.",
    show_default=False,
)
@click.pass_context
def main(ctx: click.Context, config_path: Optional[str]) -> None:
    """Health Data Watchdog — Monitor public health datasets for changes.

    Periodically fetches CDC and WHO surveillance datasets, diffs them
    against cached snapshots, and alerts you to any changes.
    """
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path


# ---------------------------------------------------------------------------
# fetch command
# ---------------------------------------------------------------------------


@main.command("fetch")
@click.option(
    "--dataset",
    "dataset_key",
    default=None,
    metavar="KEY",
    help="Fetch only the dataset with this key.  Omit to fetch all enabled datasets.",
)
@click.option(
    "--include-disabled",
    is_flag=True,
    default=False,
    help="Also fetch datasets that are marked disabled in the registry.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Print what would be fetched without actually downloading anything.",
)
@click.pass_context
def fetch_cmd(
    ctx: click.Context,
    dataset_key: Optional[str],
    include_disabled: bool,
    dry_run: bool,
) -> None:
    """Download datasets and save snapshots to disk.

    Fetches one or all datasets, computes SHA-256 content hashes,
    writes raw snapshots to the configured data directory, and records
    metadata in the local SQLite database.

    Examples:

    \b
        health-watchdog fetch
        health-watchdog fetch --dataset cdc_covid_cases
        health-watchdog fetch --dry-run
    """
    cfg = _load_config_or_exit(ctx.obj.get("config_path"))
    _configure_logging(cfg.general.log_level)

    # Build the full dataset list (built-ins + custom from config)
    all_datasets = get_all_datasets(cfg.datasets if cfg.datasets else None)

    if dataset_key:
        targets = [ds for ds in all_datasets if ds.key == dataset_key]
        if not targets:
            _err_console.print(
                f"[bold red]Unknown dataset key:[/bold red] '{dataset_key}'\n"
                f"Run [bold]health-watchdog datasets[/bold] to list available datasets."
            )
            sys.exit(1)
    else:
        targets = [
            ds for ds in all_datasets
            if include_disabled or ds.enabled
        ]

    if not targets:
        _console.print("[yellow]No datasets matched the given criteria.[/yellow]")
        return

    if dry_run:
        _console.print("[bold cyan]Dry run — datasets that would be fetched:[/bold cyan]")
        for ds in targets:
            _console.print(
                f"  [bold]{ds.key}[/bold]  "
                f"[dim]{ds.source} / {ds.format}[/dim]\n"
                f"    {ds.url}"
            )
        return

    store = _open_store(cfg)
    fetcher = DatasetFetcher(
        data_dir=cfg.general.data_dir,
        store=store,
    )

    _console.print(
        f"[bold]Fetching {len(targets)} dataset(s)...[/bold]"
    )

    results = fetcher.fetch_all(targets, skip_disabled=False)  # already filtered

    # Summarise results in a table
    table = Table(
        title="Fetch Results",
        box=box.SIMPLE_HEAD,
        show_header=True,
        header_style="bold",
        padding=(0, 1),
    )
    table.add_column("Dataset", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("HTTP", justify="right", no_wrap=True)
    table.add_column("Bytes", justify="right")
    table.add_column("Duplicate", no_wrap=True)
    table.add_column("Hash (12 chars)", no_wrap=True)

    success_count = 0
    fail_count = 0
    for r in results:
        if r.success:
            success_count += 1
            status_str = "[green]OK[/green]"
            hash_str = (r.content_hash[:12] + "...") if r.content_hash else "—"
            dup_str = "[yellow]yes[/yellow]" if r.is_duplicate else "[dim]no[/dim]"
        else:
            fail_count += 1
            status_str = "[red]FAILED[/red]"
            hash_str = "—"
            dup_str = "—"

        table.add_row(
            r.dataset_key,
            status_str,
            str(r.http_status) if r.http_status else "—",
            f"{r.byte_size:,}" if r.byte_size else "—",
            dup_str,
            hash_str,
        )

    _console.print(table)
    _console.print(
        f"[bold]Done.[/bold]  "
        f"[green]{success_count} succeeded[/green], "
        f"[red]{fail_count} failed[/red]."
    )

    fetcher.close()
    store.close()

    if fail_count > 0:
        sys.exit(1)


# ---------------------------------------------------------------------------
# diff command
# ---------------------------------------------------------------------------


@main.command("diff")
@click.option(
    "--dataset",
    "dataset_key",
    default=None,
    metavar="KEY",
    help="Diff only this dataset.  Omit to diff all datasets that have snapshots.",
)
@click.option(
    "--output",
    "output_path",
    default=None,
    metavar="FILE",
    type=click.Path(dir_okay=False, writable=True),
    help="Write a Markdown diff report to FILE.",
)
@click.option(
    "--alert",
    is_flag=True,
    default=False,
    help="Send alerts via configured Slack/email channels for detected changes.",
)
@click.pass_context
def diff_cmd(
    ctx: click.Context,
    dataset_key: Optional[str],
    output_path: Optional[str],
    alert: bool,
) -> None:
    """Diff the two most recent snapshots for each dataset.

    Compares the latest snapshot against the previous one for structural
    changes: added/removed rows, column additions/deletions, dtype shifts,
    and value-level modifications.

    Examples:

    \b
        health-watchdog diff
        health-watchdog diff --dataset cdc_covid_cases
        health-watchdog diff --output report.md
        health-watchdog diff --alert
    """
    cfg = _load_config_or_exit(ctx.obj.get("config_path"))
    _configure_logging(cfg.general.log_level)

    store = _open_store(cfg)
    reporter = Reporter(console=_console)
    differ = DatasetDiffer(
        critical_row_change_pct=cfg.thresholds.critical_row_change_pct,
        warning_column_drop_count=cfg.thresholds.warning_column_drop_count,
    )

    # Determine which dataset keys to diff
    if dataset_key:
        keys_to_diff = [dataset_key]
    else:
        keys_to_diff = store.get_dataset_keys()

    if not keys_to_diff:
        _console.print(
            "[yellow]No snapshots found in the database.  "
            "Run [bold]health-watchdog fetch[/bold] first.[/yellow]"
        )
        store.close()
        return

    # Build a mapping from dataset key to format string
    all_datasets = get_all_datasets(cfg.datasets if cfg.datasets else None)
    fmt_map = {ds.key: ds.format for ds in all_datasets}

    diff_results = []

    for key in keys_to_diff:
        snapshots = store.get_snapshots_for_dataset(
            key,
            include_duplicates=False,
            limit=2,
        )

        fmt = fmt_map.get(key, "csv")

        if len(snapshots) == 0:
            _err_console.print(
                f"[yellow]No non-duplicate snapshots for '{key}', skipping.[/yellow]"
            )
            continue
        elif len(snapshots) == 1:
            # Only one snapshot — treat as new
            snap = snapshots[0]
            try:
                new_df = load_dataframe(Path(snap.file_path), fmt)
            except Exception as exc:
                _err_console.print(
                    f"[red]Failed to load snapshot for '{key}': {exc}[/red]"
                )
                continue
            result = differ.diff(key, None, new_df)
        else:
            # snapshots[0] is newer, snapshots[1] is older
            new_snap, old_snap = snapshots[0], snapshots[1]
            try:
                old_df = load_dataframe(Path(old_snap.file_path), fmt)
                new_df = load_dataframe(Path(new_snap.file_path), fmt)
            except Exception as exc:
                _err_console.print(
                    f"[red]Failed to load snapshot for '{key}': {exc}[/red]"
                )
                continue
            result = differ.diff(key, old_df, new_df)

        diff_results.append(result)

        # Persist the change record if there are actual changes
        if result.has_changes():
            latest_snap = store.get_latest_snapshot(key)
            if latest_snap and latest_snap.id is not None:
                from health_data_watchdog.store import ChangeRecord
                store_dict = result.to_store_dict()
                change_rec = ChangeRecord(
                    snapshot_id=latest_snap.id,
                    dataset_key=key,
                    detected_at=datetime.now(timezone.utc).isoformat(),
                    change_type=store_dict["change_type"],
                    severity=store_dict["severity"],
                    row_delta=store_dict["row_delta"],
                    col_delta=store_dict["col_delta"],
                    summary=store_dict["summary"],
                    diff_json=store_dict["diff_json"],
                )
                try:
                    store.insert_change_record(change_rec)
                except Exception as exc:
                    _err_console.print(
                        f"[yellow]Warning: could not persist change record for '{key}': {exc}[/yellow]"
                    )

    if not diff_results:
        _console.print("[dim]No diffs to display.[/dim]")
        store.close()
        return

    # Print summary table
    reporter.print_summary_table(diff_results)

    # Print individual results
    reporter.print_diff_results(diff_results)

    # Write Markdown report if requested
    if output_path:
        out = Path(output_path)
        md = reporter.write_markdown_report(diff_results, output_path=out)
        _console.print(
            f"[green]Markdown report written to:[/green] [bold]{out}[/bold]"
        )

    # Send alerts if requested
    if alert:
        alert_manager = AlertManager(
            config=cfg,
            min_severity="warning",
        )
        alert_results = alert_manager.send_alerts(diff_results)
        if alert_results:
            for ar in alert_results:
                if ar.success:
                    _console.print(
                        f"[green]Alert sent via {ar.channel}[/green]: {ar.message}"
                    )
                else:
                    _err_console.print(
                        f"[red]Alert failed via {ar.channel}[/red]: {ar.message}"
                    )
        else:
            _console.print(
                "[dim]No alerts sent (no changes met the severity threshold or no channels enabled).[/dim]"
            )

    store.close()


# ---------------------------------------------------------------------------
# watch command
# ---------------------------------------------------------------------------


@main.command("watch")
@click.option(
    "--interval",
    "interval_hours",
    default=None,
    type=float,
    metavar="HOURS",
    help="Override the schedule interval in hours (default: from config).",
)
@click.option(
    "--alert",
    is_flag=True,
    default=False,
    help="Send alerts for detected changes on every scheduled run.",
)
@click.option(
    "--once",
    is_flag=True,
    default=False,
    help="Run a single fetch+diff cycle immediately then exit (useful for cron).",
)
@click.pass_context
def watch_cmd(
    ctx: click.Context,
    interval_hours: Optional[float],
    alert: bool,
    once: bool,
) -> None:
    """Start the periodic watcher for all enabled datasets.

    Runs fetch + diff on the configured schedule (default: every 6 hours).
    Press Ctrl+C to stop the watcher.

    Examples:

    \b
        health-watchdog watch
        health-watchdog watch --interval 1
        health-watchdog watch --once
        health-watchdog watch --alert
    """
    cfg = _load_config_or_exit(ctx.obj.get("config_path"))
    _configure_logging(cfg.general.log_level)

    effective_interval = interval_hours if interval_hours is not None else cfg.schedule.interval_hours

    all_datasets = get_all_datasets(cfg.datasets if cfg.datasets else None)
    enabled_datasets = [ds for ds in all_datasets if ds.enabled]

    if not enabled_datasets:
        _err_console.print(
            "[yellow]No enabled datasets found.  "
            "Check your configuration or dataset registry.[/yellow]"
        )
        return

    reporter = Reporter(console=_console)
    differ = DatasetDiffer(
        critical_row_change_pct=cfg.thresholds.critical_row_change_pct,
        warning_column_drop_count=cfg.thresholds.warning_column_drop_count,
    )
    fmt_map = {ds.key: ds.format for ds in all_datasets}

    def _run_cycle() -> None:
        """Perform a single fetch + diff cycle."""
        now = datetime.now(timezone.utc).isoformat()
        _console.print(f"\n[bold cyan]═══ Watchdog cycle started at {now} ═══[/bold cyan]")

        store = _open_store(cfg)
        fetcher = DatasetFetcher(
            data_dir=cfg.general.data_dir,
            store=store,
        )

        # Fetch all enabled datasets
        fetch_results = fetcher.fetch_all(enabled_datasets, skip_disabled=False)
        fetcher.close()

        success_count = sum(1 for r in fetch_results if r.success)
        fail_count = len(fetch_results) - success_count
        _console.print(
            f"  Fetch complete: [green]{success_count} OK[/green], "
            f"[red]{fail_count} failed[/red]."
        )

        # Diff each dataset that was successfully fetched
        diff_results = []
        for fr in fetch_results:
            if not fr.success:
                continue
            if fr.is_duplicate:
                _console.print(f"  [dim]'{fr.dataset_key}' unchanged (duplicate hash), skipping diff.[/dim]")
                continue

            key = fr.dataset_key
            fmt = fmt_map.get(key, "csv")
            snapshots = store.get_snapshots_for_dataset(key, include_duplicates=False, limit=2)

            if len(snapshots) == 0:
                continue
            elif len(snapshots) == 1:
                try:
                    new_df = load_dataframe(Path(snapshots[0].file_path), fmt)
                except Exception as exc:
                    _err_console.print(f"  [red]Load error for '{key}': {exc}[/red]")
                    continue
                result = differ.diff(key, None, new_df)
            else:
                new_snap, old_snap = snapshots[0], snapshots[1]
                try:
                    old_df = load_dataframe(Path(old_snap.file_path), fmt)
                    new_df = load_dataframe(Path(new_snap.file_path), fmt)
                except Exception as exc:
                    _err_console.print(f"  [red]Load error for '{key}': {exc}[/red]")
                    continue
                result = differ.diff(key, old_df, new_df)

            diff_results.append(result)

            # Persist change records
            if result.has_changes():
                latest_snap = store.get_latest_snapshot(key)
                if latest_snap and latest_snap.id is not None:
                    from health_data_watchdog.store import ChangeRecord
                    sd = result.to_store_dict()
                    try:
                        store.insert_change_record(
                            ChangeRecord(
                                snapshot_id=latest_snap.id,
                                dataset_key=key,
                                detected_at=datetime.now(timezone.utc).isoformat(),
                                change_type=sd["change_type"],
                                severity=sd["severity"],
                                row_delta=sd["row_delta"],
                                col_delta=sd["col_delta"],
                                summary=sd["summary"],
                                diff_json=sd["diff_json"],
                            )
                        )
                    except Exception:
                        pass

        if diff_results:
            reporter.print_summary_table(diff_results)
            changing = [r for r in diff_results if r.has_changes()]
            if changing:
                reporter.print_diff_results(changing)

        # Alerts
        if alert and diff_results:
            alert_manager = AlertManager(config=cfg, min_severity="warning")
            for ar in alert_manager.send_summary_alerts(diff_results):
                status = "[green]sent[/green]" if ar.success else "[red]failed[/red]"
                _console.print(f"  Alert {status} via {ar.channel}: {ar.message}")

        store.close()
        _console.print(
            f"  [dim]Cycle complete.  Next run in {effective_interval}h.[/dim]"
        )

    if once:
        _run_cycle()
        return

    _console.print(
        f"[bold green]Starting watcher[/bold green] — "
        f"interval: [bold]{effective_interval}h[/bold], "
        f"{len(enabled_datasets)} dataset(s) enabled.  "
        f"Press [bold]Ctrl+C[/bold] to stop."
    )

    # Run once immediately, then schedule
    _run_cycle()

    schedule.every(effective_interval).hours.do(_run_cycle)

    # Handle SIGINT/SIGTERM gracefully
    stop_flag = {"stop": False}

    def _handle_signal(sig: int, frame: object) -> None:  # type: ignore[type-arg]
        _console.print("\n[yellow]Stopping watcher…[/yellow]")
        stop_flag["stop"] = True

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    while not stop_flag["stop"]:
        schedule.run_pending()
        time.sleep(30)  # check every 30 seconds

    _console.print("[bold]Watcher stopped.[/bold]")


# ---------------------------------------------------------------------------
# report command
# ---------------------------------------------------------------------------


@main.command("report")
@click.option(
    "--dataset",
    "dataset_key",
    default=None,
    metavar="KEY",
    help="Limit the report to this dataset key.",
)
@click.option(
    "--limit",
    default=50,
    show_default=True,
    metavar="N",
    help="Maximum number of change records to display.",
)
@click.option(
    "--severity",
    "severity_filter",
    default=None,
    type=click.Choice(["info", "warning", "critical"], case_sensitive=False),
    help="Filter records by severity level.",
)
@click.option(
    "--output",
    "output_path",
    default=None,
    metavar="FILE",
    type=click.Path(dir_okay=False, writable=True),
    help="Write the change history report to a Markdown FILE.",
)
@click.option(
    "--snapshots",
    is_flag=True,
    default=False,
    help="Show snapshot history instead of change records.",
)
@click.pass_context
def report_cmd(
    ctx: click.Context,
    dataset_key: Optional[str],
    limit: int,
    severity_filter: Optional[str],
    output_path: Optional[str],
    snapshots: bool,
) -> None:
    """Display the stored change history from the local database.

    By default shows change records.  Use --snapshots to view raw fetch
    history instead.

    Examples:

    \b
        health-watchdog report
        health-watchdog report --limit 20
        health-watchdog report --dataset cdc_covid_cases
        health-watchdog report --severity critical
        health-watchdog report --output history.md
        health-watchdog report --snapshots
    """
    cfg = _load_config_or_exit(ctx.obj.get("config_path"))
    _configure_logging(cfg.general.log_level)

    store = _open_store(cfg)
    reporter = Reporter(console=_console)

    if snapshots:
        _print_snapshot_history(store, reporter, dataset_key, limit)
        store.close()
        return

    # Fetch change records
    if dataset_key:
        records = store.get_change_records_for_dataset(
            dataset_key,
            limit=limit,
            severity=severity_filter,
        )
    else:
        records = store.get_all_change_records(
            limit=limit,
            severity=severity_filter,
        )

    title = "Change History"
    if dataset_key:
        title += f" — {dataset_key}"
    if severity_filter:
        title += f" (severity: {severity_filter})"

    reporter.print_change_records(records, title=title, limit=limit)

    _console.print(
        f"\n[dim]Showing {len(records)} change record(s) "
        f"(total in DB: {store.count_change_records(dataset_key)})[/dim]"
    )

    if output_path:
        out = Path(output_path)
        reporter.write_change_records_markdown(
            records,
            output_path=out,
            title=title,
            limit=limit,
        )
        _console.print(
            f"[green]Report written to:[/green] [bold]{out}[/bold]"
        )

    store.close()


def _print_snapshot_history(
    store: SnapshotStore,
    reporter: Reporter,
    dataset_key: Optional[str],
    limit: int,
) -> None:
    """Print a table of snapshot records.

    Args:
        store: Open store instance.
        reporter: Reporter for console output.
        dataset_key: Optional key to filter by.
        limit: Maximum records to show.
    """
    if dataset_key:
        snaps = store.get_snapshots_for_dataset(dataset_key, limit=limit)
    else:
        snaps = store.get_all_snapshots(limit=limit)

    if not snaps:
        _console.print("[dim]No snapshots found.[/dim]")
        return

    table = Table(
        title="Snapshot History",
        box=box.SIMPLE_HEAD,
        show_header=True,
        header_style="bold",
        padding=(0, 1),
    )
    table.add_column("ID", justify="right", no_wrap=True)
    table.add_column("Dataset", no_wrap=True)
    table.add_column("Fetched At", no_wrap=True)
    table.add_column("HTTP", justify="right", no_wrap=True)
    table.add_column("Bytes", justify="right")
    table.add_column("Dup", no_wrap=True)
    table.add_column("Hash (12)", no_wrap=True)

    for snap in snaps:
        dup_str = "[yellow]yes[/yellow]" if snap.is_duplicate else "[dim]no[/dim]"
        table.add_row(
            str(snap.id),
            snap.dataset_key,
            snap.fetched_at,
            str(snap.http_status),
            f"{snap.byte_size:,}",
            dup_str,
            snap.content_hash[:12] + "...",
        )

    _console.print(table)
    _console.print(f"\n[dim]Showing {len(snaps)} snapshot(s).[/dim]")


# ---------------------------------------------------------------------------
# configure command
# ---------------------------------------------------------------------------


@main.command("configure")
@click.option(
    "--init",
    is_flag=True,
    default=False,
    help="Write a default configuration file (does not overwrite existing).",
)
@click.option(
    "--show",
    is_flag=True,
    default=False,
    help="Print the current effective configuration.",
)
@click.option(
    "--path",
    is_flag=True,
    default=False,
    help="Print the resolved path of the configuration file.",
)
@click.pass_context
def configure_cmd(
    ctx: click.Context,
    init: bool,
    show: bool,
    path: bool,
) -> None:
    """Manage the Health Data Watchdog configuration file.

    Examples:

    \b
        health-watchdog configure --init
        health-watchdog configure --show
        health-watchdog configure --path
    """
    config_path_str: Optional[str] = ctx.obj.get("config_path")
    target_path = Path(config_path_str) if config_path_str else None

    if init:
        written = write_default_config(target_path)
        if written.exists():
            _console.print(
                f"[green]Configuration file ready:[/green] [bold]{written}[/bold]"
            )
        else:
            _console.print(
                f"[yellow]Configuration file already exists:[/yellow] [bold]{written}[/bold]"
            )
        return

    if path:
        import os
        from health_data_watchdog.config import DEFAULT_CONFIG_PATH, CONFIG_ENV_VAR
        if config_path_str:
            resolved = Path(config_path_str).expanduser().resolve()
        else:
            env_val = os.environ.get(CONFIG_ENV_VAR)
            resolved = (
                Path(env_val).expanduser().resolve()
                if env_val
                else DEFAULT_CONFIG_PATH.expanduser().resolve()
            )
        _console.print(str(resolved))
        return

    if show:
        cfg = _load_config_or_exit(config_path_str)
        _print_config(cfg)
        return

    # Default: show help
    _console.print(ctx.get_help())


def _print_config(cfg: Config) -> None:
    """Pretty-print the effective configuration to the console.

    Args:
        cfg: Configuration object to display.
    """
    _console.print("\n[bold]Effective Configuration[/bold]\n")

    table = Table(box=box.SIMPLE_HEAD, show_header=True, header_style="bold", padding=(0, 1))
    table.add_column("Section", no_wrap=True, style="bold cyan")
    table.add_column("Key", no_wrap=True)
    table.add_column("Value")

    rows = [
        ("general", "data_dir", str(cfg.general.data_dir)),
        ("general", "db_path", str(cfg.general.db_path)),
        ("general", "log_level", cfg.general.log_level),
        ("schedule", "interval_hours", str(cfg.schedule.interval_hours)),
        ("thresholds", "critical_row_change_pct", str(cfg.thresholds.critical_row_change_pct)),
        ("thresholds", "warning_column_drop_count", str(cfg.thresholds.warning_column_drop_count)),
        ("slack", "enabled", str(cfg.slack.enabled)),
        ("slack", "webhook_url", "***" if cfg.slack.webhook_url else "(not set)"),
        ("email", "enabled", str(cfg.email.enabled)),
        ("email", "smtp_host", cfg.email.smtp_host),
        ("email", "smtp_port", str(cfg.email.smtp_port)),
        ("email", "from_address", cfg.email.from_address),
        (
            "email",
            "to_addresses",
            ", ".join(cfg.email.to_addresses) if cfg.email.to_addresses else "(none)",
        ),
        ("email", "use_tls", str(cfg.email.use_tls)),
    ]

    for section, key, value in rows:
        table.add_row(section, key, value)

    _console.print(table)

    if cfg.datasets:
        _console.print(f"\n[bold]Custom Datasets ({len(cfg.datasets)}):[/bold]")
        for ds in cfg.datasets:
            enabled_str = "[green]enabled[/green]" if ds.enabled else "[dim]disabled[/dim]"
            _console.print(
                f"  [bold]{ds.key}[/bold]  {enabled_str}  "
                f"[dim]{ds.source} / {ds.format}[/dim]\n"
                f"    {ds.url}"
            )
    else:
        _console.print("\n[dim]No custom datasets configured.[/dim]")


# ---------------------------------------------------------------------------
# datasets command
# ---------------------------------------------------------------------------


@main.command("datasets")
@click.option(
    "--all",
    "show_all",
    is_flag=True,
    default=False,
    help="Show all datasets including disabled ones.",
)
@click.option(
    "--key",
    "lookup_key",
    default=None,
    metavar="KEY",
    help="Show detailed info for a single dataset.",
)
@click.pass_context
def datasets_cmd(
    ctx: click.Context,
    show_all: bool,
    lookup_key: Optional[str],
) -> None:
    """List all registered datasets (built-in and custom).

    Examples:

    \b
        health-watchdog datasets
        health-watchdog datasets --all
        health-watchdog datasets --key cdc_covid_cases
    """
    cfg = _load_config_or_exit(ctx.obj.get("config_path"))

    all_datasets = get_all_datasets(cfg.datasets if cfg.datasets else None)

    if lookup_key:
        entry = next((ds for ds in all_datasets if ds.key == lookup_key), None)
        if entry is None:
            _err_console.print(
                f"[red]Dataset key not found:[/red] '{lookup_key}'"
            )
            sys.exit(1)
        _console.print(f"\n[bold]Dataset:[/bold] {entry.key}")
        _console.print(f"  Source:      {entry.source}")
        _console.print(f"  Format:      {entry.format}")
        _console.print(f"  Enabled:     {'yes' if entry.enabled else 'no'}")
        _console.print(f"  URL:         {entry.url}")
        _console.print(f"  Description: {entry.description}")
        if entry.extra_headers:
            _console.print(f"  Extra headers: {entry.extra_headers}")
        _console.print()
        return

    display = all_datasets if show_all else [ds for ds in all_datasets if ds.enabled]

    if not display:
        _console.print("[yellow]No datasets to display.[/yellow]")
        return

    table = Table(
        title=f"Dataset Registry ({len(display)} shown)",
        box=box.SIMPLE_HEAD,
        show_header=True,
        header_style="bold",
        padding=(0, 1),
    )
    table.add_column("Key", no_wrap=True, style="bold")
    table.add_column("Source", no_wrap=True)
    table.add_column("Format", no_wrap=True)
    table.add_column("Enabled", no_wrap=True)
    table.add_column("Description")

    for ds in display:
        enabled_str = (
            "[green]yes[/green]" if ds.enabled else "[dim]no[/dim]"
        )
        table.add_row(
            ds.key,
            ds.source,
            ds.format,
            enabled_str,
            ds.description[:70] + ("…" if len(ds.description) > 70 else ""),
        )

    _console.print(table)
    if not show_all:
        disabled_count = len(all_datasets) - len(display)
        if disabled_count:
            _console.print(
                f"[dim]{disabled_count} disabled dataset(s) hidden. "
                f"Use --all to show them.[/dim]"
            )
