# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Common commands

Dependency management is Poetry (Python ^3.12). Prefix runtime commands with `poetry run` unless you have entered `poetry shell`.

```bash
poetry install                                       # install deps (incl. dev group)

# Run the tool
poetry run sru-lint check path/to/patch.debdiff      # file
poetry run sru-lint check .                          # path to a (patches-unapplied) git repo
poetry run sru-lint check https://example.com/p.diff # URL
cat patch.debdiff | poetry run sru-lint check -      # stdin
poetry run sru-lint check -m changelog-entry .       # restrict to one plugin (or comma-separated)
poetry run sru-lint -vv check .                      # -v INFO, -vv DEBUG; -q for errors only
poetry run sru-lint check -f json .                  # machine-readable output
poetry run sru-lint plugins                          # list discovered plugins

# Tests
poetry run python -m unittest discover -s tests                                              # all unit tests
poetry run python -m unittest tests.test_changelog_entry                                     # single module
poetry run python -m unittest tests.test_changelog_entry.TestChangelogEntry.test_xyz         # single test
poetry run coverage run -m unittest discover -s tests && poetry run coverage report          # with coverage
poetry run pytest tests/functional_tests                                                     # functional tests (invoke installed `sru-lint`; run `poetry install` first)

# Lint / format / type check (also wired via .pre-commit-config.yaml)
poetry run ruff check .
poetry run ruff format .
poetry run mypy sru_lint/
```

`tox` exists but its `[testenv]` references an undefined `{[testenv]tests}` variable, so `tox` will fail out of the box — prefer the `unittest`/`pytest` invocations above.

## Architecture

The data flow for a `check` run is a single pipeline, all wired in `sru_lint/cli.py`:

1. **Input resolution** (`read_input_content`): file path, URL, `-` (stdin), or a directory. A directory is treated as a git repo and `git_debdiff()` synthesizes a debdiff by walking `git log debian/changelog` to find the previous version's commit and diffing `<that>..HEAD`.
2. **Patch parsing** (`sru_lint/common/patch_processor.py`): raw patch text → `unidiff.PatchSet` → list of `ProcessedFile`. Each `ProcessedFile` carries a `SourceSpan` whose `content` is added lines only and `content_with_context` is added + context lines (each line keeping its target line number from the diff).
3. **Plugin discovery** (`sru_lint/plugin_manager.py`): `PluginManager.load_plugins()` walks `sru_lint.plugins` recursively with `pkgutil.iter_modules`, imports every submodule (including `plugins/nested/`), and instantiates every concrete `Plugin` subclass it finds. There is no manual registration list.
4. **Filtering and execution** (`run_plugins`): plugins are run **concurrently** in a `ThreadPoolExecutor`. Each plugin's `Plugin.process()` filters `processed_files` against patterns registered via `add_file_pattern()` (matched with `fnmatch`, also tried as `*/pattern` so `debian/changelog` matches both top-level and nested paths), then calls `process_file()` per matching file.
5. **Feedback** (`sru_lint/common/feedback.py`, `sru_lint/common/errors.py`): plugins append `FeedbackItem` (message, `SourceSpan`, `ErrorCode` rule_id, `Severity`, optional `doc_url`). Console output uses `rich` to render code snippets (`sru_lint/common/ui/snippet.py`); JSON output uses `ErrorEnumEncoder`.
6. **Exit code**: 1 if any `Severity.ERROR` feedback was produced, 2 for input errors (file not found, URL fetch failure, unparseable patch), 0 otherwise.

### The `Plugin` contract (`sru_lint/plugins/plugin_base.py`)

To add a plugin, drop a class inheriting from `Plugin` anywhere under `sru_lint/plugins/`:

- `__symbolic_name__` is **auto-derived** from the class name (PascalCase → kebab-case via the regex in `_generate_symbolic_name`). Set it explicitly only to override.
- Override `register_file_patterns()` to call `self.add_file_pattern("debian/changelog")` etc. — patterns determine which `ProcessedFile`s reach your `process_file()`.
- Implement `process_file(processed_file)` (abstract). Append findings via `self.add_feedback(...)` or use the helpers `create_feedback(...)` / `create_line_feedback(...)` which build the `SourceSpan` and log at the right severity.
- Override `post_process()` for end-of-run work; it runs from `__exit__` (the CLI uses `with plugin:` per plugin).
- `self.lp_helper` is preconfigured (see below). `self.logger` is a child logger under `plugins.<symbolic-name>`.

### Launchpad integration (`sru_lint/common/launchpad_helper.py`)

`get_launchpad_helper()` returns a `LaunchpadHelper` whose `Launchpad` connection is **thread-local**: `httplib2` (used by `launchpadlib`) is not thread-safe and plugins run in parallel, so each worker thread gets its own anonymous Launchpad login. There is also a shared `_distributions_cache` guarded by `_distributions_lock`. Don't introduce a module-level `Launchpad` instance — share state only via the helper.

### Error codes

All rule IDs live in the `ErrorCode` enum in `sru_lint/common/errors.py` (e.g. `CHANGELOG001`, `PATCH002`). Use these instead of string literals when constructing feedback so JSON output and downstream consumers stay stable.

## Conventions

- Python 3.12, `line-length = 100`, double quotes, ruff selects `E,W,F,I,B,C4,UP` (see `pyproject.toml`).
- Pre-commit runs ruff (lint + format) and mypy (`--ignore-missing-imports`, excludes `docs/tests/tmp`).
- Tests use `unittest` (stdlib) for plugin/unit tests; the `tests/functional_tests/` suite uses `pytest` and shells out to the installed `sru-lint` binary, so it requires `poetry install` first.
- Distribution is via snap (`snap/snapcraft.yaml`, `core24`, `strict` confinement with `home` + `network` plugs).
