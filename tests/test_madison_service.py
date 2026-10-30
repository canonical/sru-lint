import gzip
import io
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from unittest.mock import patch

from sru_lint.madison.search import Result, search_source_index
from sru_lint.madison.service import Lookup, MadisonService


class TestMadisonService(unittest.TestCase):
    def test_source_matching_and_source_version(self):
        index = gzip.compress(b"""Package: libfoo1
Source: foo (1.0-1)
Version: 1.0-1+b1

Package: foo-tools
Source: foo
Version: 2.0

Package: foo
Source: other
Version: 99

Package: foo
Version: 3.0

Package: unrelated
Version: 100

""")
        self.assertEqual(
            search_source_index(io.BytesIO(index), "foo"),
            {("libfoo1", "1.0-1"), ("foo-tools", "2.0"), ("foo", "3.0")},
        )

    def test_lookup_shared_between_threads_and_scoped_to_service(self):
        service = MadisonService()
        result = Lookup((Result("foo", "1", "infra", "xenial", ("amd64",)),), ())
        barrier = threading.Barrier(2)

        def lookup():
            barrier.wait()
            return service.lookup_source("foo", "xenial")

        def search(_package, _release):
            time.sleep(0.05)
            return result

        with patch.object(service, "_search", side_effect=search) as mock_search:
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(lookup) for _ in range(2)]
                self.assertTrue(all(f.result() == result for f in futures))
            service.lookup_source("bar", "xenial")
            service.lookup_source("foo", "jammy")
            self.assertEqual(mock_search.call_count, 3)
            mock_search.assert_any_call("foo", "jammy")
        self.assertEqual(MadisonService()._lookups, {})

    def test_search_partial_failures_and_unavailable(self):
        service = MadisonService()

        @contextmanager
        def open_index(url):
            if "amd64" in url:
                yield io.BytesIO(gzip.compress(b"Package: foo\nVersion: 1\n\n"))
            elif "i386" in url:
                yield None
            else:
                raise OSError("network failure")

        service._cache.open_index = open_index
        with (
            patch("sru_lint.madison.service.CATEGORIES", {"infra": ("xenial",)}),
            patch("sru_lint.madison.service.ARCHITECTURES", ("amd64", "i386", "arm64")),
        ):
            lookup = service.lookup_source("foo", "xenial")
            self.assertIs(service.lookup_source("foo", "xenial"), lookup)
        self.assertEqual(len(lookup.results), 1)
        self.assertEqual(lookup.results[0].version, "1")
        self.assertEqual(len(lookup.failures), 1)
        self.assertIn("network failure", lookup.failures[0])

    def test_search_excludes_other_releases_and_product_repositories(self):
        service = MadisonService()
        urls = []

        @contextmanager
        def open_index(url):
            urls.append(url)
            # Unrelated repositories would cause a false version error if queried.
            version = b"1" if "/xenial-" in url and "/fips/" not in url else b"99"
            yield io.BytesIO(gzip.compress(b"Package: foo\nVersion: " + version + b"\n\n"))

        catalogue = {
            "infra": ("xenial-infra-security", "jammy-infra-security"),
            "infra-legacy": ("xenial-infra-legacy-updates",),
            "apps": ("xenial-apps-updates", "xenialish-apps-updates"),
            "apps-legacy": ("xenial-apps-legacy-security",),
            "fips": ("xenial",),
            "maas-3.0": ("xenial",),
        }
        service._cache.open_index = open_index
        with (
            patch("sru_lint.madison.service.CATEGORIES", catalogue),
            patch("sru_lint.madison.service.ARCHITECTURES", ("amd64",)),
        ):
            lookup = service.lookup_source("foo", "xenial")
        self.assertEqual(len(urls), 4)
        self.assertEqual({result.version for result in lookup.results}, {"1"})
        self.assertEqual(
            {result.category for result in lookup.results},
            {"infra", "infra-legacy", "apps", "apps-legacy"},
        )
        self.assertEqual(lookup.failures, ())
