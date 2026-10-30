# sru-lint

Static analysis tool for Ubuntu SRU (Stable Release Update) patches — built to run in CI and generate human-friendly reports.

## Documentation

**For complete documentation, installation instructions, usage examples, and plugin development guides, visit:**

**[https://canonical-sru-lint.readthedocs-hosted.com/en/latest/](https://canonical-sru-lint.readthedocs-hosted.com/en/latest/)**

## Quick Start

```bash
# Install from snap (recommended)
snap install --edge sru-lint

# Or install with Poetry (for development)
git clone https://github.com/dargad/sru-lint.git
cd sru-lint
poetry install

# Check from a patches-unapplied git repo (the current directory)
sru-lint check .

# Check a patch file or URL
sru-lint check path/to/patch.debdiff  # if installed via snap
poetry run sru-lint check path/to/patch.debdiff  # if using Poetry
sru-lint check https://example.com/patch.diff

# Check from stdin
cat patch.debdiff | sru-lint check -  # if installed via snap
cat patch.debdiff | poetry run sru-lint check -  # if using Poetry

# List available plugins
sru-lint plugins  # if installed via snap
poetry run sru-lint plugins  # if using Poetry
```

## Authenticating to Launchpad

By default `sru-lint` talks to Launchpad anonymously, which is enough for the
public checks. ESM / Ubuntu Pro plugins query private PPAs (e.g.
`ppa:ubuntu-esm/esm-infra-security`) and need an authenticated session —
without it those checks are skipped or produce false negatives.

To authenticate once and cache the OAuth credentials in your keyring:

```bash
sru-lint login                # opens a browser to authorize via OAuth
poetry run sru-lint login     # equivalent when running from source
```

Subsequent `sru-lint check` runs reuse the cached credentials automatically.
Re-run `sru-lint login` only if the credentials expire or are revoked.

Note: if no usable keyring backend is available (headless session, snap
confinement blocking the keyring, etc.) the credentials cannot persist and
`sru-lint check` will fall back to anonymous access — `sru-lint login` will
warn you when this happens.

## Package version lookups

Search ESM and product PPA binary indexes with the `madison` subcommand:

```bash
sru-lint madison -c infra -s jammy-infra-security openssl
sru-lint madison -c landscape-latest-stable -a amd64,arm64 landscape-client
sru-lint madison -c apps -r '^python3-requests$' --format json
sru-lint madison --clear-cache
```

Defaults search all configured repositories for `amd64`. Use `-c`, `-s`, and
`-a` to narrow the search; `-a all` queries all supported architectures.
A HEAD request checks each index's ETag or Last-Modified validator; matching
indexes are reused from `${XDG_CACHE_HOME:-~/.cache}/sru-lint/madison`.
Misses require an additional GET, and indexes without validators are downloaded
on every lookup. Old versions remain until `--clear-cache`; clearing does not
cancel in-flight downloads, which may repopulate the cache. HTTP proxies may
serve a view that lags the origin. This command does not use Launchpad OAuth.
See the usage documentation for
repository categories, filtering, and exit codes.

## What it checks

- **Changelog entries** (valid distributions, LP bugs, version ordering)
- **DEP-3 patch format** compliance
- **Launchpad integration** (bug targeting, SRU templates, publication history)
- **Upload queue** conflicts
- **Version collisions** with versions already in ESM, Ubuntu Pro and product PPA repositories (`madison-collision`)
- And more via the plugin system...

## Development

Run the full local check suite (ruff lint + format, mypy, unit tests, and
functional tests) with a single command:

```bash
poetry run check-all
```

It runs every step even if an earlier one fails and prints a PASS/FAIL summary
at the end, exiting non-zero if anything failed.

## License

MIT License - see [LICENSE](LICENSE) for details.
