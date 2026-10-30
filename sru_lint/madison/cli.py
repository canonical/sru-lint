"""Typer command adapter for madison."""

import json
import re
from dataclasses import asdict

import typer

from sru_lint.madison.api import build_jobs, run_jobs
from sru_lint.madison.cache import IndexCache
from sru_lint.madison.repositories import ARCHITECTURES, CATEGORIES


def expand(values: list[str] | None) -> list[str]:
    return list(
        dict.fromkeys(
            part.strip() for value in values or [] for part in value.split(",") if part.strip()
        )
    )


def madison(
    packages: list[str] | None = typer.Argument(
        None, help="Package names (or patterns with --regex)."
    ),
    package_options: list[str] | None = typer.Option(None, "--packages", "-p"),
    architecture: list[str] | None = typer.Option(
        None,
        "--architecture",
        "-a",
        help="Architectures, comma-separated or repeated; default amd64; accepts all.",
    ),
    category: list[str] | None = typer.Option(
        None,
        "--category",
        "-c",
        help="Repository categories, comma-separated or repeated; default all.",
    ),
    suite: list[str] | None = typer.Option(
        None, "--suite", "-s", help="Suites; default depends on category."
    ),
    regex: bool = typer.Option(False, "--regex", "-r"),
    filter_pattern: str | None = typer.Option(
        None, "--filter", "-f", help="Python regex applied to canonical result lines."
    ),
    clear_cache: bool = typer.Option(False, "--clear-cache", "-C"),
    output_format: str = typer.Option("console", "--format", help="console or json"),
):
    """Look up binary package versions in ESM and product PPAs."""
    from sru_lint.cli import global_options

    queries = expand((packages or []) + (package_options or []))
    categories = expand(category) or list(CATEGORIES)
    architectures = expand(architecture) or ["amd64"]
    if "all" in architectures:
        architectures = list(ARCHITECTURES)
    suites = expand(suite)
    try:
        if output_format not in ("console", "json"):
            raise ValueError("--format must be console or json")
        if not queries and not clear_cache:
            raise ValueError("Provide at least one package or --clear-cache")
        if regex:
            for query in queries:
                re.compile(query)
        pattern = re.compile(filter_pattern) if filter_pattern is not None else None
        jobs = build_jobs(categories, suites, architectures)
    except (ValueError, re.error) as error:
        raise typer.BadParameter(str(error)) from error
    cache = IndexCache()
    try:
        if clear_cache:
            cache.clear()
    except OSError as error:
        typer.echo(f"Cannot clear cache: {error}", err=True)
        raise typer.Exit(1) from error
    if not queries:
        return

    def report(job, error):
        if global_options.verbose and not global_options.quiet:
            if error is None:
                typer.echo(f"Checked {job[3]}", err=True)
            else:
                typer.echo(f"Failed {job[3]}: {error}", err=True)

    outcome = run_jobs(jobs, queries, regex=regex, cache=cache, on_job_complete=report)
    unavailable = outcome.unavailable
    failures = outcome.failures
    results = [
        result for result in outcome.results if pattern is None or pattern.search(result.line())
    ]
    if output_format == "json":
        typer.echo(json.dumps([asdict(result) for result in results], indent=2))
    elif not global_options.quiet:
        if results:
            fields = [result.line().split(" | ") for result in results]
            widths = [max(len(row[i]) for row in fields) for i in range(5)]
            for row in fields:
                typer.echo(
                    " | ".join(value.ljust(widths[i]) for i, value in enumerate(row)).rstrip()
                )
        else:
            typer.echo("No matching packages found.")
    if unavailable and not global_options.quiet:
        typer.echo(
            f"{unavailable} repository/architecture combinations unavailable (404).", err=True
        )
    if failures:
        typer.echo(f"Search incomplete: {failures} index(es) failed.", err=True)
        raise typer.Exit(1)
