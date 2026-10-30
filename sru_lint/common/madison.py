"""rmadison-style version lookups in Ubuntu Pro and related repositories.

A port of ``esmadison.sh`` from canonical/se-misc.  It reads the public
``Packages.gz`` indexes of:

* the Ubuntu Pro repositories on esm.ubuntu.com (infra, apps, fips, realtime,
  ros, ...),
* the Canonical kernel team PPAs,
* the Landscape and MAAS PPAs.

The command line front end is ``sru-lint madison``.  Plugins should use
:func:`get_madison`, which returns a shared, thread-safe :class:`Madison`
whose downloaded indexes are reused for the rest of the run, e.g.::

    result = get_madison().query(["openssl"], categories=["infra"], by_source=True)
    newest = result.highest_version("openssl", release="xenial")

Downloaded indexes are cached on disk and revalidated with
``If-Modified-Since``, so repeated lookups only transfer changed indexes.
"""

from __future__ import annotations

import gzip
import os
import re
import shutil
import tempfile
import threading
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from email.utils import formatdate, parsedate_to_datetime
from enum import Enum
from pathlib import Path

from debian.debian_support import Version

from sru_lint.common.logging import get_logger

logger = get_logger("madison")

ESM_BASE_URL = "https://esm.ubuntu.com"
PPA_BASE_URL = "https://ppa.launchpadcontent.net"
USER_AGENT = "sru-lint-madison"

ALL_ARCHES = ("amd64", "i386", "arm64", "armel", "armhf", "powerpc", "ppc64el", "riscv64", "s390x")
DEFAULT_ARCHES = ("amd64",)

LANDSCAPE_VERSIONS = (
    "19.10",
    "23.03",
    "24.04",
    "26.04",
    "beta",
    "latest-stable",
    "latest-stable-client",
)
MAAS_VERSIONS = ("2.7", "2.8", "2.9", "3.0", "3.1", "3.2", "3.3", "3.4", "3.5", "3.6", "3.7", "3.8")


class MadisonError(ValueError):
    """Invalid query (unknown category or architecture, bad regex, ...)."""


@dataclass(frozen=True)
class Repository:
    """One apt repository and the suites sru-lint looks at by default."""

    category: str
    base_url: str
    suites: tuple[str, ...]
    component: str = "main"

    def index_url(self, suite: str, arch: str) -> str:
        return f"{self.base_url}/dists/{suite}/{self.component}/binary-{arch}/Packages.gz"


def _suites(releases: str, *suffixes: str) -> tuple[str, ...]:
    """``_suites("xenial bionic", "updates")`` -> ("xenial-updates", "bionic-updates")."""
    names = releases.split()
    if not suffixes:
        return tuple(names)
    return tuple(f"{r}-{s}" for r in names for s in suffixes)


def _landscape_ppa(version: str) -> str:
    if version == "19.10" or version.startswith("latest-stable"):
        return version
    return f"self-hosted-{version}"


def _build_repositories() -> dict[str, Repository]:
    # Keep in sync with esmadison.sh when Pro adds or retires suites.
    esm = ESM_BASE_URL
    repos = [
        Repository(
            "infra",
            f"{esm}/infra/ubuntu",
            _suites(
                "trusty xenial bionic focal jammy noble resolute",
                "infra-security",
                "infra-updates",
            ),
        ),
        Repository(
            "infra-legacy",
            f"{esm}/infra-legacy/ubuntu",
            _suites("trusty xenial", "infra-legacy-security", "infra-legacy-updates"),
        ),
        Repository(
            "apps",
            f"{esm}/apps/ubuntu",
            _suites("xenial bionic focal jammy noble resolute", "apps-updates", "apps-security"),
        ),
        Repository(
            "apps-legacy",
            f"{esm}/apps-legacy/ubuntu",
            _suites("xenial", "apps-legacy-security", "apps-legacy-updates"),
        ),
        Repository("fips", f"{esm}/fips/ubuntu", _suites("xenial bionic focal jammy")),
        Repository("fips-preview", f"{esm}/fips-preview/ubuntu", _suites("focal jammy")),
        Repository(
            "fips-updates",
            f"{esm}/fips-updates/ubuntu",
            _suites("xenial bionic focal jammy noble", "updates"),
        ),
        Repository("realtime", f"{esm}/realtime/ubuntu", _suites("jammy noble")),
        Repository(
            "ros", f"{esm}/ros/ubuntu", _suites("xenial bionic focal", "updates", "security")
        ),
        Repository("ros-updates", f"{esm}/ros-updates/ubuntu", _suites("xenial bionic", "updates")),
        Repository(
            "kernel-ppa",
            f"{PPA_BASE_URL}/canonical-kernel-team/ppa/ubuntu",
            _suites("focal jammy noble resolute"),
        ),
        Repository(
            "kernel-ppa2",
            f"{PPA_BASE_URL}/canonical-kernel-team/ppa2/ubuntu",
            _suites("focal jammy noble resolute"),
        ),
    ]
    for version in LANDSCAPE_VERSIONS:
        repos.append(
            Repository(
                f"landscape-{version}",
                f"{PPA_BASE_URL}/landscape/{_landscape_ppa(version)}/ubuntu",
                _suites("bionic focal jammy noble resolute"),
            )
        )
    for version in MAAS_VERSIONS:
        repos.append(
            Repository(
                f"maas-{version}",
                f"{PPA_BASE_URL}/maas/{version}/ubuntu",
                _suites("bionic focal jammy noble resolute"),
            )
        )
    return {r.category: r for r in repos}


REPOSITORIES: dict[str, Repository] = _build_repositories()

# Shorthands accepted by --category that expand to several repositories.
CATEGORY_GROUPS: dict[str, tuple[str, ...]] = {
    "landscape": tuple(f"landscape-{v}" for v in LANDSCAPE_VERSIONS),
    "maas": tuple(f"maas-{v}" for v in MAAS_VERSIONS),
    "kernel": ("kernel-ppa", "kernel-ppa2"),
}


# --------------------------------------------------------------- index parsing


@dataclass(frozen=True)
class BinaryPackage:
    """The fields of one ``Packages`` stanza that madison needs."""

    package: str
    version: str
    architecture: str
    source: str
    source_version: str


_SOURCE_RE = re.compile(r"^(?P<name>\S+)(?:\s+\((?P<version>[^)]+)\))?$")
_WANTED_FIELDS = {"package", "version", "architecture", "source"}


def _make_binary(fields: dict[str, str]) -> BinaryPackage | None:
    package, version = fields.get("package"), fields.get("version")
    if not package or not version:
        return None
    source, source_version = package, version
    match = _SOURCE_RE.match(fields.get("source", ""))
    if match:
        source = match["name"]
        source_version = match["version"] or version
    return BinaryPackage(package, version, fields.get("architecture", ""), source, source_version)


def parse_packages_index(lines: Iterable[str]) -> Iterator[BinaryPackage]:
    """Parse a ``Packages`` file.

    This deliberately small parser only reads the fields madison uses.
    Full deb822 parsing is several times slower on large indexes.
    """
    fields: dict[str, str] = {}
    for raw in lines:
        line = raw.rstrip("\r\n")
        if not line.strip():
            if fields:
                binary = _make_binary(fields)
                if binary:
                    yield binary
                fields = {}
            continue
        if line[0] in " \t":  # continuation of a multi-line field
            continue
        key, sep, value = line.partition(":")
        key = key.lower()
        if sep and key in _WANTED_FIELDS:
            fields[key] = value.strip()
    if fields:
        binary = _make_binary(fields)
        if binary:
            yield binary


# ------------------------------------------------------------------- fetching


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes = b""
    last_modified: str | None = None


HttpGet = Callable[[str, dict[str, str], float], HttpResponse]


def urllib_get(url: str, headers: dict[str, str], timeout: float) -> HttpResponse:
    """Return HTTP error statuses (304, 404, ...) as responses.

    Network failures raise ``OSError``.
    """
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **headers})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return HttpResponse(response.status, response.read(), response.headers["Last-Modified"])
    except urllib.error.HTTPError as e:
        return HttpResponse(e.code)


class IndexStatus(Enum):
    OK = "ok"
    MISSING = "missing"  # the repository does not publish this suite/architecture
    FAILED = "failed"


@dataclass(frozen=True)
class IndexResult:
    url: str
    status: IndexStatus
    packages: tuple[BinaryPackage, ...] = ()
    error: str = ""
    stale: bool = False  # served from the cache because the server was unreachable


def default_cache_dir() -> Path:
    # Inside the snap, SNAP_USER_COMMON survives refreshes; the per-revision HOME does not.
    if snap_common := os.environ.get("SNAP_USER_COMMON"):
        root = Path(snap_common) / ".cache"
    elif xdg := os.environ.get("XDG_CACHE_HOME"):
        root = Path(xdg)
    else:
        root = Path.home() / ".cache"
    return root / "sru-lint" / "madison"


class PackageIndexFetcher:
    """Downloads ``Packages.gz`` files into a revalidated on-disk cache."""

    def __init__(
        self,
        cache_dir: Path | None = None,
        http_get: HttpGet = urllib_get,
        timeout: float = 30.0,
    ):
        self.cache_dir = cache_dir or default_cache_dir()
        self.http_get = http_get
        self.timeout = timeout

    def cache_path(self, url: str) -> Path:
        name = re.sub(r"^https?://", "", url).replace("/", "_")
        return self.cache_dir / name

    def clear_cache(self) -> int:
        """Remove every cached index; returns how many files were removed."""
        if not self.cache_dir.is_dir():
            return 0
        count = sum(1 for p in self.cache_dir.iterdir() if p.is_file())
        shutil.rmtree(self.cache_dir)
        return count

    def fetch(self, url: str) -> IndexResult:
        path = self.cache_path(url)
        result = self._fetch(url, path, revalidate=path.is_file())
        if result is None:  # cached copy was corrupt; it has been removed
            result = self._fetch(url, path, revalidate=False)
        return result or IndexResult(url, IndexStatus.FAILED, error="corrupt index")

    def _fetch(self, url: str, path: Path, revalidate: bool) -> IndexResult | None:
        headers = {}
        if revalidate:
            headers["If-Modified-Since"] = formatdate(path.stat().st_mtime, usegmt=True)
        try:
            response = self.http_get(url, headers, self.timeout)
        except OSError as e:
            reason = getattr(e, "reason", e)
            if revalidate:
                logger.warning(f"Cannot reach {url} ({reason}); using the cached copy")
                return self._load(url, path, stale=True)
            return IndexResult(url, IndexStatus.FAILED, error=f"cannot download: {reason}")

        if response.status == 304 and revalidate:
            return self._load(url, path)
        if response.status in (404, 410):
            path.unlink(missing_ok=True)
            return IndexResult(url, IndexStatus.MISSING)
        if response.status != 200:
            return IndexResult(url, IndexStatus.FAILED, error=f"HTTP {response.status}")
        try:
            text = gzip.decompress(response.body).decode("utf-8", errors="replace")
        except (OSError, EOFError) as e:
            return IndexResult(url, IndexStatus.FAILED, error=f"corrupt download: {e}")
        self._store(path, response.body, response.last_modified)
        return IndexResult(url, IndexStatus.OK, tuple(parse_packages_index(text.splitlines())))

    def _load(self, url: str, path: Path, stale: bool = False) -> IndexResult | None:
        try:
            text = gzip.decompress(path.read_bytes()).decode("utf-8", errors="replace")
        except (OSError, EOFError):
            logger.debug(f"Discarding corrupt cache file {path}")
            path.unlink(missing_ok=True)
            return None
        packages = tuple(parse_packages_index(text.splitlines()))
        return IndexResult(url, IndexStatus.OK, packages, stale=stale)

    def _store(self, path: Path, data: bytes, last_modified: str | None) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            if last_modified:
                try:
                    stamp = parsedate_to_datetime(last_modified).timestamp()
                    os.utime(tmp, (stamp, stamp))
                except (TypeError, ValueError):
                    pass
            os.replace(tmp, path)
        except OSError as e:  # an unwritable cache must not break lookups
            logger.warning(f"Cannot cache {path.name}: {e}")


# -------------------------------------------------------------------- queries


@dataclass(frozen=True)
class MadisonEntry:
    """A binary package version found in one repository suite and architecture."""

    package: str
    version: str
    category: str
    suite: str
    architecture: str  # index architecture (binary-<arch>), even for Architecture: all
    source: str
    source_version: str

    @property
    def release(self) -> str:
        """Ubuntu series of the suite (``jammy-infra-security`` -> ``jammy``)."""
        return self.suite.split("-", 1)[0]


@dataclass(frozen=True)
class MadisonRow:
    """One output line: identical name/version/category/suite merged across arches."""

    name: str
    version: str
    category: str
    suite: str
    architectures: tuple[str, ...]

    def format_cells(self) -> tuple[str, str, str, str, str]:
        return (self.name, self.version, self.category, self.suite, ", ".join(self.architectures))


def _arch_order(arch: str) -> int:
    return ALL_ARCHES.index(arch) if arch in ALL_ARCHES else len(ALL_ARCHES)


@dataclass
class MadisonResult:
    entries: list[MadisonEntry]
    indexes: list[IndexResult]
    by_source: bool = False
    categories: tuple[str, ...] = field(default_factory=tuple)

    @property
    def failed(self) -> list[IndexResult]:
        return [i for i in self.indexes if i.status is IndexStatus.FAILED]

    @property
    def complete(self) -> bool:
        """False if some index could not be read, so results may be missing."""
        return not self.failed

    def _name_version(self, entry: MadisonEntry) -> tuple[str, str]:
        if self.by_source:
            return entry.source, entry.source_version
        return entry.package, entry.version

    def rows(self) -> list[MadisonRow]:
        grouped: dict[tuple[str, str, str, str], set[str]] = {}
        for e in self.entries:
            name, version = self._name_version(e)
            grouped.setdefault((name, version, e.category, e.suite), set()).add(e.architecture)
        rows = [
            MadisonRow(*key, architectures=tuple(sorted(arches, key=_arch_order)))
            for key, arches in grouped.items()
        ]
        rows.sort(key=lambda r: (r.name, Version(r.version), r.category, r.suite))
        return rows

    def versions(self, name: str, release: str | None = None) -> list[str]:
        """Distinct versions of ``name``, sorted by Debian version order.

        ``name`` is a binary package name, or a source package name when the
        query used ``by_source``.  Pass ``release`` to restrict the result
        to one Ubuntu series.
        """
        found = {
            self._name_version(e)[1]
            for e in self.entries
            if self._name_version(e)[0] == name and (release is None or e.release == release)
        }
        return sorted(found, key=Version)

    def highest_version(self, name: str, release: str | None = None) -> str | None:
        versions = self.versions(name, release)
        return versions[-1] if versions else None

    def where(self, name: str, version: str) -> list[MadisonEntry]:
        """Entries publishing exactly ``version`` of ``name``."""
        return [e for e in self.entries if self._name_version(e) == (name, version)]


def resolve_categories(tokens: Iterable[str] | None) -> tuple[str, ...]:
    """Expand group names and validate categories.  ``None`` or empty means all."""
    tokens = [t for t in (tokens or ()) if t]
    if not tokens:
        return tuple(REPOSITORIES)
    resolved: list[str] = []
    for token in tokens:
        names = CATEGORY_GROUPS.get(token, (token,))
        for name in names:
            if name not in REPOSITORIES:
                raise MadisonError(
                    f"Unknown category '{token}'. Known categories: "
                    f"{', '.join([*REPOSITORIES, *CATEGORY_GROUPS])}"
                )
            if name not in resolved:
                resolved.append(name)
    return tuple(resolved)


def resolve_arches(tokens: Iterable[str] | None) -> tuple[str, ...]:
    """Validate architectures.  ``all`` selects every architecture; empty means amd64."""
    tokens = [t for t in (tokens or ()) if t]
    if not tokens:
        return DEFAULT_ARCHES
    if "all" in tokens:
        return ALL_ARCHES
    unknown = [t for t in tokens if t not in ALL_ARCHES]
    if unknown:
        raise MadisonError(
            f"Unknown architecture(s): {', '.join(unknown)}. Known: {', '.join(ALL_ARCHES)}, all"
        )
    return tuple(dict.fromkeys(tokens))


def _matcher(names: Sequence[str], regex: bool) -> Callable[[str], bool]:
    if not regex:
        wanted = set(names)
        return wanted.__contains__
    try:
        patterns = [re.compile(n) for n in names]
    except re.error as e:
        raise MadisonError(f"Invalid regular expression: {e}") from None
    return lambda name: any(p.search(name) for p in patterns)


class Madison:
    """Queries package versions across :data:`REPOSITORIES`.

    It is thread-safe, and each index is downloaded at most once per instance.
    """

    def __init__(
        self,
        fetcher: PackageIndexFetcher | None = None,
        max_workers: int = 8,
    ):
        self.fetcher = fetcher or PackageIndexFetcher()
        self.max_workers = max_workers
        self._indexes: dict[str, IndexResult] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()
        self._source_results: dict[str, MadisonResult] = {}
        self._source_lock = threading.Lock()

    def source_versions(self, package: str) -> MadisonResult:
        """Query a source package once, across all repositories and architectures.

        Incomplete results are also retained so concurrent checks use the same
        snapshot rather than retrying unavailable repositories.
        """
        with self._source_lock:
            if package not in self._source_results:
                result = self.query([package], by_source=True, arches=["all"])
                if not result.complete:
                    logger.warning(
                        f"Madison lookup for {package} is incomplete: "
                        f"{len(result.failed)} index(es) failed; version conflicts may be missed"
                    )
                self._source_results[package] = result
            return self._source_results[package]

    def index(self, url: str) -> IndexResult:
        with self._guard:
            if url in self._indexes:
                return self._indexes[url]
            lock = self._locks.setdefault(url, threading.Lock())
        with lock:
            with self._guard:
                if url in self._indexes:
                    return self._indexes[url]
            logger.info(f"Checking: {url}")
            result = self.fetcher.fetch(url)
            if result.status is IndexStatus.FAILED:
                logger.warning(f"{url}: {result.error}")
            elif result.status is IndexStatus.MISSING:
                logger.debug(f"{url}: not published")
            with self._guard:
                # Failures are not remembered, so a later query retries them.
                if result.status is not IndexStatus.FAILED:
                    self._indexes[url] = result
            return result

    def query(
        self,
        names: Sequence[str],
        *,
        categories: Iterable[str] | None = None,
        suites: Iterable[str] | None = None,
        arches: Iterable[str] | None = None,
        regex: bool = False,
        by_source: bool = False,
        progress: Callable[[int, int], None] | None = None,
    ) -> MadisonResult:
        """Look up ``names`` (binary names, or source names with ``by_source``).

        ``suites`` replaces every category's default suites, like
        esmadison's ``-s``.  ``progress(done, total)`` is called after each
        index is processed.
        """
        if not names:
            raise MadisonError("No package names given")
        selected = resolve_categories(categories)
        arch_list = resolve_arches(arches)
        suite_override = tuple(s for s in (suites or ()) if s)
        matches = _matcher(names, regex)

        targets = [
            (REPOSITORIES[c], suite, arch)
            for c in selected
            for suite in (suite_override or REPOSITORIES[c].suites)
            for arch in arch_list
        ]
        total = len(targets)
        done = 0
        done_lock = threading.Lock()

        def run(target: tuple[Repository, str, str]) -> tuple[IndexResult, list[MadisonEntry]]:
            nonlocal done
            repo, suite, arch = target
            result = self.index(repo.index_url(suite, arch))
            found = [
                MadisonEntry(
                    b.package, b.version, repo.category, suite, arch, b.source, b.source_version
                )
                for b in result.packages
                if matches(b.source if by_source else b.package)
            ]
            if progress:
                with done_lock:
                    done += 1
                    progress(done, total)
            return result, found

        entries: list[MadisonEntry] = []
        indexes: list[IndexResult] = []
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            for result, found in pool.map(run, targets):
                indexes.append(result)
                entries.extend(found)
        return MadisonResult(entries, indexes, by_source=by_source, categories=selected)


_shared: Madison | None = None
_shared_lock = threading.Lock()


def get_madison() -> Madison:
    """The process-wide :class:`Madison` shared by plugins."""
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = Madison()
        return _shared


def format_rows(rows: Sequence[MadisonRow], line_filter: str | None = None) -> list[str]:
    """Render rows as aligned ``a | b | c`` lines.

    ``line_filter`` is a regex that keeps only the lines it matches, like
    esmadison's ``-f``.
    """
    cells = [r.format_cells() for r in rows]
    if line_filter:
        try:
            pattern = re.compile(line_filter)
        except re.error as e:
            raise MadisonError(f"Invalid filter expression: {e}") from None
        cells = [c for c in cells if pattern.search(" | ".join(c))]
    if not cells:
        return []
    widths = [max(len(c[i]) for c in cells) for i in range(4)]
    return [" | ".join([*(c[i].ljust(widths[i]) for i in range(4)), c[4]]).rstrip() for c in cells]
