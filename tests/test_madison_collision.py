import gzip
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

from sru_lint.common.errors import ErrorCode
from sru_lint.common.feedback import Severity
from sru_lint.common.parse import base_release
from sru_lint.madison.api import SearchOutcome, build_jobs, search_repositories
from sru_lint.madison.repositories import CATEGORIES, suite_matches_release
from sru_lint.madison.search import Result
from sru_lint.plugins.madison_collision import MadisonCollision
from tests.test_pro_version_numbers import make_processed_file

SEARCH = "sru_lint.plugins.madison_collision.search_repositories"

INDEX = b"Package: pkg\nVersion: 1.2.3-0ubuntu1\n\nPackage: pkg\nVersion: 1.2.2-0ubuntu1\n\n"


def changelog(version="1.2.3-0ubuntu1", distribution="jammy", package="pkg"):
    return [
        f"{package} ({version}) {distribution}; urgency=medium",
        "",
        "  * Some change",
        "",
        " -- Author <author@example.com>  Mon, 01 Jan 2024 12:00:00 +0000",
    ]


def result(version="1.2.3-0ubuntu1", category="infra", suite="jammy-infra-security", package="pkg"):
    return Result(package, version, category, suite, ("amd64",))


class TestMadisonCollisionPlugin(unittest.TestCase):
    def setUp(self):
        self.plugin = MadisonCollision()
        self.plugin.feedback = []
        self.plugin.cache = MagicMock()

    def run_plugin(self, lines, outcome=None, lines_added_indices=None, **search_kwargs):
        pf = make_processed_file("debian/changelog", lines, lines_added_indices)
        with patch(SEARCH, return_value=outcome or SearchOutcome(), **search_kwargs) as search:
            self.plugin.process_file(pf)
        return search

    def test_symbolic_name_and_patterns(self):
        self.assertEqual(self.plugin.__symbolic_name__, "madison-collision")
        self.assertTrue(self.plugin.matches_file("debian/changelog"))
        self.assertTrue(self.plugin.matches_file("pkg/debian/changelog"))
        self.assertFalse(self.plugin.matches_file("debian/control"))

    def test_construction_does_not_build_cache(self):
        with patch("sru_lint.plugins.madison_collision.IndexCache") as cache:
            MadisonCollision()
        cache.assert_not_called()

    def test_exact_collision_is_error(self):
        outcome = SearchOutcome(
            results=[
                result(),
                result(category="apps", suite="jammy-apps-updates"),
                result(version="1.2.2-0ubuntu1"),
            ]
        )
        search = self.run_plugin(changelog(), outcome)

        self.assertEqual(len(self.plugin.feedback), 1)
        item = self.plugin.feedback[0]
        self.assertEqual(item.rule_id, ErrorCode.MADISON_VERSION_COLLISION)
        self.assertEqual(item.severity, Severity.ERROR)
        self.assertIn("'1.2.3-0ubuntu1' of 'pkg'", item.message)
        self.assertIn("apps/jammy-apps-updates, infra/jammy-infra-security", item.message)
        self.assertNotIn("1.2.2", item.message)
        self.assertEqual(item.span.start_line, 1)
        search.assert_called_once()
        self.assertEqual(search.call_args.args, (["pkg"],))
        self.assertEqual(search.call_args.kwargs["release"], "jammy")
        self.assertEqual(search.call_args.kwargs["architectures"], ("amd64",))
        self.assertIs(search.call_args.kwargs["cache"], self.plugin.cache)

    def test_no_collision_for_other_version_or_package(self):
        outcome = SearchOutcome(results=[result(version="1.2.2-0ubuntu1"), result(package="other")])
        self.run_plugin(changelog(), outcome)
        self.assertEqual(self.plugin.feedback, [])

    def test_empty_results(self):
        self.run_plugin(changelog())
        self.assertEqual(self.plugin.feedback, [])

    def test_unreleased_skipped(self):
        search = self.run_plugin(changelog(distribution="UNRELEASED"))
        search.assert_not_called()
        self.assertEqual(self.plugin.feedback, [])

    def test_uca_skipped(self):
        search = self.run_plugin(changelog("1:16.0.0-0ubuntu1~cloud1", "noble-epoxy"))
        search.assert_not_called()
        self.assertEqual(self.plugin.feedback, [])

    def test_pocket_suffix_is_stripped_for_release(self):
        for distribution in ("jammy-security", "jammy-proposed", "jammy jammy-proposed"):
            with self.subTest(distribution=distribution):
                search = self.run_plugin(changelog(distribution=distribution))
                self.assertEqual(search.call_args.kwargs["release"], "jammy")

    def test_only_first_entry_is_checked(self):
        lines = changelog("1.2.3-0ubuntu2", "jammy") + [""] + changelog("1.2.3-0ubuntu1", "focal")
        outcome = SearchOutcome(results=[result(version="1.2.3-0ubuntu1")])
        search = self.run_plugin(lines, outcome)
        self.assertEqual(search.call_args.kwargs["release"], "jammy")
        self.assertEqual(self.plugin.feedback, [])

    def test_no_added_lines(self):
        search = self.run_plugin(changelog(), lines_added_indices=[])
        search.assert_not_called()
        self.assertEqual(self.plugin.feedback, [])

    def test_unparseable_changelog_is_silent(self):
        search = self.run_plugin(["not a changelog"])
        search.assert_not_called()
        self.assertEqual(self.plugin.feedback, [])

    def test_failed_indexes_warn(self):
        self.run_plugin(changelog(), SearchOutcome(failures=2))
        self.assertEqual(len(self.plugin.feedback), 1)
        item = self.plugin.feedback[0]
        self.assertEqual(item.rule_id, ErrorCode.MADISON_CHECK_ERROR)
        self.assertEqual(item.severity, Severity.WARNING)
        self.assertIn("2 index(es) failed", item.message)

    def test_collision_and_failures_both_reported(self):
        self.run_plugin(changelog(), SearchOutcome(results=[result()], failures=1))
        self.assertEqual(
            [f.rule_id for f in self.plugin.feedback],
            [ErrorCode.MADISON_VERSION_COLLISION, ErrorCode.MADISON_CHECK_ERROR],
        )

    def test_unavailable_indexes_are_silent(self):
        self.run_plugin(changelog(), SearchOutcome(unavailable=5))
        self.assertEqual(self.plugin.feedback, [])

    def test_search_exception_warns(self):
        pf = make_processed_file("debian/changelog", changelog())
        with patch(SEARCH, side_effect=OSError("offline")):
            self.plugin.process_file(pf)
        self.assertEqual(len(self.plugin.feedback), 1)
        item = self.plugin.feedback[0]
        self.assertEqual(item.rule_id, ErrorCode.MADISON_CHECK_ERROR)
        self.assertEqual(item.severity, Severity.WARNING)
        self.assertIn("offline", item.message)

    def test_cache_is_created_lazily(self):
        self.plugin.cache = None
        pf = make_processed_file("debian/changelog", changelog())
        with (
            patch("sru_lint.plugins.madison_collision.IndexCache") as cache,
            patch(SEARCH, return_value=SearchOutcome()),
        ):
            self.plugin.process_file(pf)
            self.plugin.process_file(pf)
        cache.assert_called_once()

    def test_end_to_end_with_index_fixture(self):
        with tempfile.TemporaryDirectory() as directory:
            index = Path(directory) / "Packages.gz"
            index.write_bytes(gzip.compress(INDEX))
            opened = []

            @contextmanager
            def open_index(url):
                opened.append(url)
                with index.open("rb") as handle:
                    yield handle

            self.plugin.cache.open_index.side_effect = open_index
            pf = make_processed_file("debian/changelog", changelog())
            self.plugin.process_file(pf)

        self.assertTrue(opened)
        self.assertTrue(all("/dists/jammy" in url for url in opened), opened)
        self.assertTrue(all("binary-amd64" in url for url in opened), opened)
        self.assertEqual(len(self.plugin.feedback), 1)
        self.assertEqual(self.plugin.feedback[0].rule_id, ErrorCode.MADISON_VERSION_COLLISION)


class TestBaseRelease(unittest.TestCase):
    def test_base_release(self):
        self.assertEqual(base_release("jammy"), "jammy")
        self.assertEqual(base_release("jammy-security"), "jammy")
        self.assertEqual(base_release("jammy-proposed"), "jammy")
        self.assertEqual(base_release("jammy-updates"), "jammy")
        self.assertEqual(base_release("jammy-backports"), "jammy")
        self.assertEqual(base_release("jammy jammy-proposed"), "jammy")
        self.assertEqual(base_release("jammy, jammy-proposed"), "jammy")
        self.assertIsNone(base_release(""))
        self.assertIsNone(base_release("UNRELEASED"))


class TestReleaseNarrowing(unittest.TestCase):
    def test_suite_matches_release(self):
        self.assertTrue(suite_matches_release("jammy", "jammy"))
        self.assertTrue(suite_matches_release("jammy-infra-security", "jammy"))
        self.assertTrue(suite_matches_release("jammy-updates", "jammy"))
        self.assertFalse(suite_matches_release("jammy2", "jammy"))
        self.assertFalse(suite_matches_release("xenial-updates", "jammy"))
        self.assertFalse(suite_matches_release("noble", "jammy"))

    def test_every_catalogue_suite_starts_with_a_release(self):
        releases = {
            "trusty",
            "xenial",
            "bionic",
            "focal",
            "jammy",
            "noble",
            "resolute",
        }
        for category, suites in CATEGORIES.items():
            for suite in suites:
                with self.subTest(category=category, suite=suite):
                    self.assertTrue(any(suite_matches_release(suite, r) for r in releases))

    def test_build_jobs_without_release_is_unchanged(self):
        jobs = build_jobs(["infra"], ["jammy"], ["amd64", "arm64"])
        self.assertEqual(
            [j[:3] for j in jobs], [("infra", "jammy", "amd64"), ("infra", "jammy", "arm64")]
        )
        jobs = build_jobs(["infra-legacy"], None, ["amd64"])
        self.assertEqual(len(jobs), len(CATEGORIES["infra-legacy"]))

    def test_build_jobs_rejects_unknown_category(self):
        with self.assertRaises(ValueError):
            build_jobs(["nope"], None, ["amd64"], "jammy")

    def test_build_jobs_narrows_to_release(self):
        jobs = build_jobs(list(CATEGORIES), None, ["amd64"], "jammy")
        self.assertTrue(jobs)
        self.assertTrue(all(job[1] == "jammy" or job[1].startswith("jammy-") for job in jobs))
        pairs = {(job[0], job[1]) for job in jobs}
        self.assertIn(("infra", "jammy-infra-security"), pairs)
        self.assertIn(("infra", "jammy-infra-updates"), pairs)
        self.assertIn(("fips", "jammy"), pairs)
        self.assertIn(("maas-3.8", "jammy"), pairs)
        self.assertFalse(
            {"ros", "ros-updates", "infra-legacy", "apps-legacy"} & {j[0] for j in jobs}
        )

    def test_build_jobs_unknown_release_yields_nothing(self):
        self.assertEqual(build_jobs(list(CATEGORIES), None, ["amd64"], "questing"), [])

    def test_search_repositories_only_opens_release_indexes(self):
        urls = []

        @contextmanager
        def open_index(url):
            urls.append(url)
            yield None

        cache = MagicMock()
        cache.open_index.side_effect = open_index
        outcome = search_repositories(["pkg"], release="focal", cache=cache)
        self.assertEqual(outcome.results, [])
        self.assertEqual(outcome.unavailable, len(urls))
        self.assertTrue(urls)
        self.assertTrue(all("/dists/focal" in url and "binary-amd64" in url for url in urls))
        self.assertTrue(len(urls) < 60, len(urls))


if __name__ == "__main__":
    unittest.main()
