import json
import logging
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from enum import StrEnum
from pathlib import Path

import typer
from debian.changelog import Changelog
from git import Repo
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

from sru_lint.common.errors import ErrorEnumEncoder
from sru_lint.common.feedback import FeedbackItem
from sru_lint.common.launchpad_helper import get_launchpad_helper
from sru_lint.common.logging import get_logger, setup_logger
from sru_lint.common.patch_processor import process_patch_content
from sru_lint.common.ui.snippet import render_snippet
from sru_lint.madison.cli import madison
from sru_lint.plugin_manager import PluginManager


# Format options enum
class OutputFormat(StrEnum):
    console = "console"
    json = "json"


# Global state for CLI options
class GlobalOptions:
    verbose: int = 0
    quiet: bool = False


global_options = GlobalOptions()
console = Console()


def configure_logging():
    """Configure logging based on global options."""
    if global_options.quiet:
        log_level = logging.ERROR
    elif global_options.verbose >= 2:
        log_level = logging.DEBUG
    elif global_options.verbose >= 1:
        log_level = logging.INFO
    else:
        log_level = logging.WARNING

    setup_logger(level=log_level)


def verbose_callback(value: int):
    """Callback for verbose option."""
    global_options.verbose = value
    configure_logging()


def quiet_callback(value: bool):
    """Callback for quiet option."""
    global_options.quiet = value
    configure_logging()


def feedback_to_dict(feedback_item):
    """Convert a FeedbackItem to a dictionary for JSON serialization."""
    result = {
        "message": feedback_item.message,
        "rule_id": feedback_item.rule_id,
        "severity": feedback_item.severity.value,
        "span": {
            "path": feedback_item.span.path,
            "start_line": feedback_item.span.start_line,
            "start_col": feedback_item.span.start_col,
            "end_line": feedback_item.span.end_line,
            "end_col": feedback_item.span.end_col,
        },
    }

    if feedback_item.doc_url:
        result["doc_url"] = feedback_item.doc_url

    return result


def process_module_list(modules: list[str] | None) -> list[str]:
    """Process comma-separated module names into a flat list."""
    logger = get_logger("cli")

    if modules is None:
        return []

    expanded_modules = []
    for module_item in modules:
        # Split by comma and strip whitespace
        expanded_modules.extend([m.strip() for m in module_item.split(",")])

    # Remove empty items
    expanded_modules = [m for m in expanded_modules if m]
    logger.debug(f"Modules to run: {expanded_modules}")

    return expanded_modules


def is_url(input_str: str) -> bool:
    """Check if the input string is a URL."""
    parsed = urllib.parse.urlparse(input_str)
    return parsed.scheme in ("http", "https")


def fetch_url_content(url: str) -> str:
    """Fetch content from a URL."""
    logger = get_logger("cli")
    logger.debug(f"Fetching content from URL: {url}")

    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            content: str = response.read().decode("utf-8")
            logger.debug(f"Fetched {len(content)} characters from URL")
            return content
    except Exception as e:
        logger.error(f"Failed to fetch content from URL {url}: {e}")
        typer.echo(f"Error: Failed to fetch content from URL: {e}", err=True)
        raise typer.Exit(code=2) from None


def git_debdiff(repo_path: str):
    logger = get_logger("cli")

    repo = Repo(repo_path)
    changelog_path = "debian/changelog"

    cur_ver = Changelog(open(Path(repo_path) / changelog_path)).full_version

    logger.debug(f"Current version: {cur_ver}")

    commit_prefix = "commit "
    hashes = []
    log_output = repo.git.log(changelog_path).splitlines()
    for line in log_output:
        if line.startswith(commit_prefix):
            hashes.append(line[len(commit_prefix) :])

    last_version_commit = hashes[0]
    for hash in hashes:
        commit = repo.commit(hash)
        ch_blob = commit.tree / changelog_path
        content = ch_blob.data_stream.read().decode("utf-8")
        changelog = Changelog(content)
        logger.debug(f"Compare {hash} with version {changelog.full_version}")
        if changelog.full_version != cur_ver:
            last_version_commit = hash
            break

    diff_range = f"{last_version_commit}..HEAD"
    logger.debug(f"Using {diff_range} to generate debdiff")

    return repo.git.diff(diff_range)


def read_input_content(input_source: str) -> str:
    """Read patch content from input source (file path, URL, or stdin)."""
    logger = get_logger("cli")

    logger.debug(f"Input source: {input_source}")

    if input_source == "-":
        # Read from stdin
        logger.debug("Reading patch from stdin")
        patch_content = sys.stdin.read()
        logger.debug(f"Read {len(patch_content)} characters from stdin")
    elif is_url(input_source):
        # Fetch from URL
        logger.debug(f"Fetching patch from URL: {input_source}")
        patch_content = fetch_url_content(input_source)
    elif Path(input_source).is_dir():
        logger.debug("Deriving debdiff from git")
        patch_content = git_debdiff(input_source)
    else:
        # Read from file path
        logger.debug(f"Reading patch from file: {input_source}")
        try:
            with open(input_source, encoding="utf-8") as file:
                patch_content = file.read()
            logger.debug(f"Read {len(patch_content)} characters from file")
        except FileNotFoundError:
            logger.error(f"File not found: {input_source}")
            typer.echo(f"Error: File not found: {input_source}", err=True)
            raise typer.Exit(code=2) from None
        except Exception as e:
            logger.error(f"Error reading file {input_source}: {e}")
            typer.echo(f"Error reading file: {e}", err=True)
            raise typer.Exit(code=2) from None

    logger.debug(f"Patch content: {patch_content}")

    return patch_content


def process_input_to_files(patch_content: str):
    """Convert patch content to ProcessedFile objects."""
    logger = get_logger("cli")

    processed_files = process_patch_content(patch_content)
    if not processed_files:
        logger.error("No files found in patch or failed to parse patch")
        raise typer.Exit(code=2)

    logger.info(f"Converted patch to {len(processed_files)} processed files")
    return processed_files


def load_and_filter_plugins(modules: list[str], output_format: OutputFormat):
    """Load plugins and filter them based on specified modules."""
    logger = get_logger("cli")

    # Load all plugins
    pm = PluginManager()
    plugins = pm.load_plugins()
    logger.debug(f"Loaded {len(plugins)} plugins")

    # Filter plugins based on modules
    if "all" not in modules:
        filtered_plugins = [p for p in plugins if p.__symbolic_name__ in modules]
        if not filtered_plugins:
            logger.warning(f"No plugins found matching the specified modules: {', '.join(modules)}")

            if output_format == OutputFormat.console:
                typer.echo("Available modules:")
                for plugin in plugins:
                    typer.echo(f"- {plugin.__symbolic_name__}")
            else:
                # For JSON format, output empty array when no modules found
                typer.echo(json.dumps([]))
            return []

        plugins = filtered_plugins
        logger.info(f"Filtered to {len(plugins)} plugins: {[p.__symbolic_name__ for p in plugins]}")

    logger.info(f"Running {len(plugins)} plugins")
    return plugins


def _run_single_plugin(plugin, processed_files) -> tuple[str, list[FeedbackItem], float]:
    """Run a single plugin and return its name, feedback, and elapsed time."""
    logger = get_logger("cli")
    logger.debug(f"Running plugin: {plugin.__symbolic_name__}")

    start_time = time.time()

    with plugin:
        plugin.process(processed_files)

    plugin_feedback = list(plugin.feedback)  # Make a copy of feedback
    elapsed = time.time() - start_time

    logger.debug(
        f"Plugin {plugin.__symbolic_name__} generated {len(plugin_feedback)} feedback items in {elapsed:.2f}s"
    )

    return plugin.__symbolic_name__, plugin_feedback, elapsed


def run_plugins(plugins, processed_files, output_format: OutputFormat) -> list[FeedbackItem]:
    """Run all plugins concurrently on the processed files and collect feedback."""
    if not plugins:
        return []

    feedback = []

    # Don't show progress in JSON mode or if quiet
    if output_format == OutputFormat.json or global_options.quiet:
        with ThreadPoolExecutor() as executor:
            futures = {
                executor.submit(_run_single_plugin, plugin, processed_files): plugin
                for plugin in plugins
            }

            for future in as_completed(futures):
                plugin_name, plugin_feedback, elapsed = future.result()
                feedback.extend(plugin_feedback)

    else:
        # Show progress with rich progress bar
        total_plugins = len(plugins)
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            TimeElapsedColumn(),
            console=console,
            transient=True,  # Remove progress bar when done
        ) as progress:
            task = progress.add_task(
                f"Running plugins: 0 of {total_plugins} completed", total=total_plugins
            )

            with ThreadPoolExecutor() as executor:
                futures = {
                    executor.submit(_run_single_plugin, plugin, processed_files): plugin
                    for plugin in plugins
                }

                completed_count = 0
                for future in as_completed(futures):
                    plugin_name, plugin_feedback, elapsed = future.result()
                    feedback.extend(plugin_feedback)
                    completed_count += 1

                    # Update progress to show completion count
                    progress.update(
                        task,
                        completed=completed_count,
                        description=f"Running plugins: {completed_count} of {total_plugins} completed",
                    )

    return feedback


def analyze_feedback(feedback: list[FeedbackItem]) -> tuple[int, int, int]:
    """Analyze feedback and count items by severity."""
    logger = get_logger("cli")

    error_count = sum(1 for item in feedback if item.severity.value == "error")
    warning_count = sum(1 for item in feedback if item.severity.value == "warning")
    info_count = sum(1 for item in feedback if item.severity.value == "info")

    logger.info(
        f"Collected {len(feedback)} feedback items: {error_count} errors, {warning_count} warnings, {info_count} info"
    )

    return error_count, warning_count, info_count


def output_json_feedback(feedback: list[FeedbackItem]):
    """Output feedback in JSON format."""
    feedback_dicts = [feedback_to_dict(item) for item in feedback]
    typer.echo(json.dumps(feedback_dicts, indent=2, cls=ErrorEnumEncoder))


def output_console_feedback(feedback: list[FeedbackItem]):
    """Output feedback in console format with snippets."""
    if not global_options.quiet:
        if feedback:
            typer.echo("\nFeedback:")
            for item in feedback:
                # Format output based on severity
                severity_color = {
                    "error": typer.colors.RED,
                    "warning": typer.colors.YELLOW,
                    "info": typer.colors.BLUE,
                }.get(item.severity.value, None)

                typer.secho(
                    f"- {item.message} (Severity: {item.severity.value}): {item.span.path}",
                    fg=severity_color,
                )

                if not item.span.is_empty():
                    render_snippet(
                        code="\n".join([line.content for line in item.span.lines_added]),
                        title=f"File: {item.span.path}",
                        highlight_lines=[item.span.start_line] if item.span.start_line >= 0 else [],
                        severity=item.severity,
                        annotations={
                            item.span.start_line: [
                                (
                                    item.message,
                                    item.span.start_col if item.span.start_col >= 0 else 0,
                                    item.span.end_col if item.span.end_col >= 0 else 0,
                                )
                            ]
                        },
                    )
                if item.doc_url:
                    typer.secho(f"  More info: {item.doc_url}", fg=typer.colors.CYAN)
        else:
            typer.secho("✅ No issues found", fg=typer.colors.GREEN)


def output_feedback(feedback: list[FeedbackItem], output_format: OutputFormat):
    """Output feedback in the specified format."""
    if output_format == OutputFormat.json:
        output_json_feedback(feedback)
    else:
        output_console_feedback(feedback)


def output_summary(
    error_count: int,
    warning_count: int,
    info_count: int,
    output_format: OutputFormat,
):
    """Print a colored one-line summary of feedback counts.

    Skipped in JSON mode (the array of items is itself the machine-readable
    summary) and in quiet mode. When there is no feedback at all,
    ``output_console_feedback`` already prints the "no issues found"
    banner, so nothing more is needed here.
    """
    if output_format == OutputFormat.json or global_options.quiet:
        return
    if error_count == 0 and warning_count == 0 and info_count == 0:
        return

    parts: list[str] = []
    if error_count:
        parts.append(
            typer.style(
                f"{error_count} error{'s' if error_count != 1 else ''}",
                fg=typer.colors.RED,
                bold=True,
            )
        )
    if warning_count:
        parts.append(
            typer.style(
                f"{warning_count} warning{'s' if warning_count != 1 else ''}",
                fg=typer.colors.YELLOW,
                bold=True,
            )
        )
    if info_count:
        parts.append(typer.style(f"{info_count} info", fg=typer.colors.BLUE, bold=True))

    typer.echo(f"\nSummary: {', '.join(parts)}")


def show_processing_summary(processed_files, plugins, output_format: OutputFormat):
    """Show a summary of what will be processed."""
    if output_format == OutputFormat.json or global_options.quiet:
        return

    file_count = len(processed_files)
    plugin_count = len(plugins)

    console.print(f"[blue]Processing {file_count} file(s) with {plugin_count} plugin(s)...[/blue]")

    if global_options.verbose >= 1:
        console.print("[dim]Files:[/dim]")
        for f in processed_files:
            console.print(f"  [dim]• {f.path}[/dim]")

        console.print("[dim]Plugins:[/dim]")
        for p in plugins:
            console.print(f"  [dim]• {p.__symbolic_name__}[/dim]")
        console.print()


app = typer.Typer(
    help="sru-lint - Static analysis tool for Ubuntu SRU patches",
    add_completion=False,
    callback=lambda: None,  # Dummy callback to allow global options
)


app.command()(madison)


# Add global options to the main app
@app.callback()
def main(
    verbose: int = typer.Option(
        0,
        "--verbose",
        "-v",
        count=True,
        help="Increase verbosity (-v for INFO, -vv for DEBUG)",
        callback=verbose_callback,
        is_eager=True,
    ),
    quiet: bool = typer.Option(
        False,
        "--quiet",
        "-q",
        help="Suppress all output except errors",
        callback=quiet_callback,
        is_eager=True,
    ),
):
    """Global options for sru-lint."""
    pass


@app.command()
def check(
    input_source: str = typer.Argument(
        "-", metavar="INPUT", help="File path, URL, or '-' for stdin"
    ),
    modules: list[str] | None = typer.Option(
        ["all"],
        "--modules",
        "-m",
        help="Only run the specified module(s). Default is 'all'. Can be specified as comma-separated list or multiple times",
    ),
    format: OutputFormat = typer.Option(
        OutputFormat.console,
        "--format",
        "-f",
        help="Output format: 'console' for human-readable output with snippets, 'json' for machine-readable JSON array",
    ),
):
    """
    Run the linter on the specified patch from a file, URL, or stdin.
    """
    logger = get_logger("cli")
    logger.debug(f"Output format: {format}")

    # Process input parameters
    expanded_modules = process_module_list(modules)

    # Read and process input
    patch_content = read_input_content(input_source)
    processed_files = process_input_to_files(patch_content)

    # Load and filter plugins
    plugins = load_and_filter_plugins(expanded_modules, format)
    if not plugins:
        return  # Early exit if no plugins found

    # Show processing summary
    show_processing_summary(processed_files, plugins, format)

    # Run plugins and collect feedback
    feedback = run_plugins(plugins, processed_files, format)

    # Analyze feedback
    error_count, warning_count, info_count = analyze_feedback(feedback)

    # Output results
    output_feedback(feedback, format)
    output_summary(error_count, warning_count, info_count, format)

    # Exit with error code if there are any errors
    if error_count > 0:
        logger.error(f"Found {error_count} error(s)")
        raise typer.Exit(code=1)


@app.command()
def plugins():
    """
    List all available plugins.
    """
    logger = get_logger("cli")

    typer.echo("Available plugins:")

    pm = PluginManager()
    plugins = pm.load_plugins()
    logger.debug(f"Loaded {len(plugins)} plugins")

    if not plugins:
        typer.echo("No plugins found.")
        return

    # Calculate the maximum length of plugin names for alignment
    max_name_length = max(len(plugin.__symbolic_name__) for plugin in plugins)

    for plugin in plugins:
        # Get the class name
        plugin_name = plugin.__symbolic_name__
        # Get the docstring (description)
        plugin_description = plugin.__class__.__doc__ or "No description available"
        # Clean up the description (remove leading/trailing whitespace and newlines)
        plugin_description = " ".join(plugin_description.split())
        # Print formatted output with aligned descriptions
        typer.echo(f"- {plugin_name:<{max_name_length}} : {plugin_description}")
        logger.debug(
            f"Plugin {plugin_name}: {plugin.__class__.__module__}.{plugin.__class__.__name__}"
        )


@app.command()
def login():
    """
    Authenticate with Launchpad via OAuth.

    Opens a browser to authorize sru-lint and caches the credentials for
    future runs. Re-run this command only if the cached credentials expire
    or are revoked.
    """
    logger = get_logger("cli")
    logger.info("Starting Launchpad login")

    helper = get_launchpad_helper()
    try:
        lp = helper.login()
    except Exception as e:
        logger.error(f"Launchpad login failed: {e}")
        typer.echo(f"Error: Launchpad login failed: {e}", err=True)
        raise typer.Exit(code=2) from None

    me = lp.me
    typer.secho(
        f"✓ Logged in to Launchpad as {me.name} ({me.display_name})",
        fg=typer.colors.GREEN,
    )

    if not helper.credentials_persisted:
        typer.secho(
            "\n⚠ Credentials were NOT persisted to a keyring.\n"
            "  `sru-lint check` cannot reuse this login and will fall back "
            "to anonymous access, so private bugs will remain invisible.\n"
            "  Likely causes:\n"
            "    • No usable keyring backend (no GNOME Keyring / KWallet running)\n"
            "    • Headless session with no keyring daemon\n"
            "    • Snap confinement blocking access to the keyring",
            fg=typer.colors.YELLOW,
            err=True,
        )
        raise typer.Exit(code=1)


@app.command()
def inspect():
    """
    Inspect the patch and generate a HTML report.
    """
    logger = get_logger("cli")
    logger.info("Starting patch inspection")
    typer.echo("Inspecting code...")
    # TODO: Implement inspection logic


@app.command("help")
def help_cmd(
    ctx: typer.Context,
    command: list[str] | None = typer.Argument(
        None,
        help="Show help for this app or a subcommand path, e.g. `help greet` or `help tools sub`.",
    ),
):
    """Show the same help text as `--help`."""
    # `ctx` here is the context of the `help` command. Its parent is the app context.
    if ctx.parent is None:
        typer.secho("No parent context available", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    if not command:
        # Root help (same as `myprog --help`)
        typer.echo(ctx.parent.get_help())
        raise typer.Exit()

    # Resolve a nested command path (e.g. ["tools", "build"])
    cmd = ctx.parent.command  # start at the app (click.MultiCommand)
    target = None
    info_parts: list[str] = []

    for name in command:
        info_parts.append(name)
        # Use getattr for click Group API compatibility
        get_cmd = getattr(cmd, "get_command", None)
        if get_cmd is None:
            break
        target = get_cmd(ctx.parent, name)
        if target is None:
            typer.secho(f"Unknown command: {' '.join(info_parts)}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=2)
        cmd = target  # descend

    # Show help for the resolved command
    if target is None:
        typer.secho("No command specified", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)

    with typer.Context(target, info_name=" ".join(info_parts), parent=ctx.parent) as subctx:
        typer.echo(target.get_help(subctx))
    raise typer.Exit()


if __name__ == "__main__":
    app()
