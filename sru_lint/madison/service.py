"""Run-scoped, shared source-package lookups in release-specific ESM indexes."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from threading import Lock

from sru_lint.madison.cache import IndexCache
from sru_lint.madison.repositories import ARCHITECTURES, CATEGORIES, index_url
from sru_lint.madison.search import Result, aggregate, search_source_index


@dataclass(frozen=True)
class Lookup:
    results: tuple[Result, ...]
    failures: tuple[str, ...]


class MadisonService:
    """Share complete lookups, including failures, between concurrent plugins.

    Create one service per lint run. No result or failure survives into the next
    run; IndexCache independently validates downloaded indexes on each lookup.
    """

    def __init__(self):
        self._cache = IndexCache()
        self._lock = Lock()
        self._lookups: dict[tuple[str, str], Lookup] = {}

    def lookup_source(self, package: str, release: str) -> Lookup:
        # Plugins run concurrently; serialize cache misses so even simultaneous
        # requests for the same package perform only one search.
        key = (package, release)
        with self._lock:
            if key not in self._lookups:
                self._lookups[key] = self._search(package, release)
            return self._lookups[key]

    def _search(self, package: str, release: str) -> Lookup:
        jobs = [
            (category, suite, arch, index_url(category, suite, arch))
            for category, suites in CATEGORIES.items()
            if category in {"infra", "infra-legacy", "apps", "apps-legacy"}
            for suite in suites
            if suite == release or suite.startswith(f"{release}-")
            for arch in ARCHITECTURES
        ]

        def fetch(job):
            category, suite, arch, url = job
            with self._cache.open_index(url) as index:
                if index is None:
                    return []  # 404: repository/architecture not published
                return [
                    (name, version, category, suite, arch)
                    for name, version in search_source_index(index, package)
                ]

        rows = []
        failures = []
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {executor.submit(fetch, job): job for job in jobs}
            for future in as_completed(futures):
                try:
                    rows.extend(future.result())
                except Exception as error:
                    failures.append(f"{futures[future][3]}: {error}")
        return Lookup(tuple(aggregate(rows)), tuple(sorted(failures)))
