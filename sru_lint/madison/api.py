"""Programmatic madison search shared by the CLI and plugins."""

from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from itertools import product

from sru_lint.madison.cache import IndexCache
from sru_lint.madison.repositories import (
    CATEGORIES,
    index_url,
    suite_matches_release,
)
from sru_lint.madison.search import Result, aggregate, search_index

Job = tuple[str, str, str, str]
"""``(category, suite, architecture, url)``"""

DEFAULT_ARCHITECTURES = ("amd64",)


@dataclass
class SearchOutcome:
    results: list[Result] = field(default_factory=list)
    unavailable: int = 0
    failures: int = 0


def build_jobs(
    categories: Iterable[str],
    suites: Iterable[str] | None,
    architectures: Iterable[str],
    release: str | None = None,
) -> list[Job]:
    """Build the index jobs for a search, rejecting unknown or unsafe components.

    Explicit ``suites`` apply to every category. When ``release`` is given, only
    suites equal to the release or one of its pockets are kept, and categories
    with no such suite produce no jobs.
    """
    suites = list(suites or [])
    architectures = list(architectures)
    jobs: list[Job] = []
    for cat in categories:
        if cat not in CATEGORIES:
            raise ValueError(f"Unknown category: {cat}")
        selected = suites or CATEGORIES[cat]
        if release is not None:
            selected = tuple(s for s in selected if suite_matches_release(s, release))
        for selected_suite, arch in product(selected, architectures):
            jobs.append((cat, selected_suite, arch, index_url(cat, selected_suite, arch)))
    return jobs


def run_jobs(
    jobs: list[Job],
    queries: list[str],
    *,
    regex: bool = False,
    cache: IndexCache,
    max_workers: int = 8,
    on_job_complete: Callable[[Job, Exception | None], None] | None = None,
) -> SearchOutcome:
    """Search every job's index for ``queries`` and aggregate the matches.

    A missing index (HTTP 404) is counted as unavailable. Any other error is
    counted as a failure and the remaining indexes are still searched.
    ``on_job_complete`` is called on the calling thread as each job finishes.
    """

    def fetch_search(job: Job):
        cat, selected_suite, arch, url = job
        with cache.open_index(url) as index:
            if index is None:
                return None
            return [
                (name, version, cat, selected_suite, arch)
                for name, version in search_index(index, queries, regex)
            ]

    rows: list[tuple[str, str, str, str, str]] = []
    outcome = SearchOutcome()
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(fetch_search, job): job for job in jobs}
        for future in as_completed(futures):
            job = futures[future]
            try:
                matches = future.result()
            except Exception as error:
                outcome.failures += 1
                if on_job_complete:
                    on_job_complete(job, error)
                continue
            if matches is None:
                outcome.unavailable += 1
            else:
                rows.extend(matches)
            if on_job_complete:
                on_job_complete(job, None)
    outcome.results = aggregate(rows)
    return outcome


def search_repositories(
    queries: list[str],
    *,
    categories: Iterable[str] | None = None,
    suites: Iterable[str] | None = None,
    architectures: Iterable[str] | None = None,
    release: str | None = None,
    regex: bool = False,
    cache: IndexCache | None = None,
    max_workers: int = 8,
) -> SearchOutcome:
    """Build jobs and search them. Defaults to every category on amd64."""
    jobs = build_jobs(
        categories if categories is not None else list(CATEGORIES),
        suites,
        architectures if architectures is not None else DEFAULT_ARCHITECTURES,
        release,
    )
    return run_jobs(
        jobs,
        queries,
        regex=regex,
        cache=cache or IndexCache(),
        max_workers=max_workers,
    )
