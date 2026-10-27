"""Offline tests for package index lookups."""

import gzip
import io
import json
import tempfile
import threading
import unittest
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from sru_lint.cli import app
from sru_lint.madison.cache import IndexCache
from sru_lint.madison.repositories import CATEGORIES, index_url
from sru_lint.madison.search import aggregate, search_index

INDEX = b"Package: hello\nDescription: example\nArchitecture: all\nVersion: 1.0\n\nPackage: hello\nVersion: 2.0\n\nPackage: hello-tools\nVersion: 3\n\nPackage: incomplete\n\n"


class Response(io.BytesIO):
    def __init__(self, body=b"", headers=None):
        super().__init__(body)
        self.headers = headers if headers is not None else {"ETag": "tag", "Last-Modified": "date"}


def http_error(status):
    return urllib.error.HTTPError("url", status, "error", {}, None)


class MadisonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.index = self.directory / "fixture.gz"
        self.index.write_bytes(gzip.compress(INDEX))

    def test_repository_urls(self):
        cases = {
            "infra": "esm.ubuntu.com/infra/ubuntu",
            "apps-legacy": "esm.ubuntu.com/apps-legacy/ubuntu",
            "kernel-ppa2": "ppa.launchpadcontent.net/canonical-kernel-team/ppa2/ubuntu",
            "landscape-19.10": "ppa.launchpadcontent.net/landscape/19.10/ubuntu",
            "landscape-beta": "ppa.launchpadcontent.net/landscape/self-hosted-beta/ubuntu",
            "landscape-latest-stable-client": "ppa.launchpadcontent.net/landscape/latest-stable-client/ubuntu",
            "maas-3.8": "ppa.launchpadcontent.net/maas/3.8/ubuntu",
        }
        for category, base in cases.items():
            self.assertEqual(
                index_url(category, "jammy", "amd64"),
                f"https://{base}/dists/jammy/main/binary-amd64/Packages.gz",
            )
        for category, suites in CATEGORIES.items():
            for suite in suites:
                self.assertTrue(index_url(category, suite, "arm64").endswith("Packages.gz"))
        for args in [
            ("unknown", "jammy", "amd64"),
            ("infra", "../escape", "amd64"),
            ("infra", "jammy", "unknown"),
        ]:
            with self.assertRaises(ValueError):
                index_url(*args)

    def test_structured_search_and_aggregation(self):
        for queries, regex, expected in [
            (["hello"], False, {("hello", "1.0"), ("hello", "2.0")}),
            (["hello"], True, {("hello", "1.0"), ("hello", "2.0"), ("hello-tools", "3")}),
            (["missing"], False, set()),
        ]:
            with self.index.open("rb") as index:
                self.assertEqual(search_index(index, queries, regex), expected)
        results = aggregate(
            [("hello", "1", "infra", "jammy", arch) for arch in ["arm64", "amd64", "amd64"]]
            + [("hello", "1", "infra", "focal", "amd64")]
        )
        self.assertEqual(len(results), 2)
        self.assertEqual(results[1].architectures, ("amd64", "arm64"))

    @patch("sru_lint.madison.cli.IndexCache")
    def test_all_architectures_partial_results_and_help(self, cache):
        @contextmanager
        def open_index(url):
            if "binary-amd64" in url:
                with self.index.open("rb") as index:
                    yield index
            else:
                yield None

        cache.return_value.open_index.side_effect = open_index
        runner = CliRunner()
        args = ["madison", "-c", "infra", "-s", "jammy", "-a", "all", "hello", "--format", "json"]
        result = runner.invoke(app, args)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(cache.return_value.open_index.call_count, 9)
        self.assertEqual(json.loads(result.stdout)[0]["architectures"], ["amd64"])

        @contextmanager
        def partial(url):
            if "binary-amd64" in url:
                with self.index.open("rb") as index:
                    yield index
            else:
                raise OSError("offline")

        cache.return_value.open_index.side_effect = partial
        result = runner.invoke(app, args)
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertEqual(len(json.loads(result.stdout)), 2)
        for args in (["madison", "--help"], ["help", "madison"]):
            result = runner.invoke(app, args)
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertIn("--category", result.stdout)

    @patch("sru_lint.madison.cli.IndexCache")
    def test_cli_json_and_filters(self, cache):
        cache.return_value.open_index.side_effect = lambda url: self.index.open("rb")
        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "madison",
                "-c",
                "infra",
                "-s",
                "jammy-infra-security",
                "-a",
                "amd64,arm64",
                "-p",
                "hello,missing",
                "--format",
                "json",
            ],
        )
        self.assertEqual(result.exit_code, 0, result.output)
        records = json.loads(result.stdout)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["architectures"], ["amd64", "arm64"])
        result = runner.invoke(
            app, ["madison", "-c", "infra", "-s", "jammy", "-r", "^hello", "-f", "tools"]
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIn("hello-tools", result.stdout)
        self.assertNotIn("1.0", result.stdout)

    @patch("sru_lint.madison.cli.IndexCache")
    def test_cli_validation_clear_and_failures(self, cache):
        runner = CliRunner()
        for args in [
            [],
            ["-c", "bad", "hello"],
            ["-a", "bad", "hello"],
            ["-r", "["],
            ["--format", "bad", "hello"],
        ]:
            result = runner.invoke(app, ["madison", *args])
            self.assertEqual(result.exit_code, 2, result.output)
        cache.assert_not_called()
        self.assertEqual(runner.invoke(app, ["madison", "-C"]).exit_code, 0)
        cache.return_value.clear.assert_called_once()
        args = ["madison", "-c", "infra", "-s", "jammy", "hello", "--format", "json"]

        @contextmanager
        def unavailable(url):
            yield None

        cache.return_value.open_index.side_effect = unavailable
        result = runner.invoke(app, args)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(json.loads(result.stdout), [])
        self.assertIn("404", result.stderr)
        cache.return_value.open_index.side_effect = OSError("offline")
        result = runner.invoke(app, args)
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(json.loads(result.stdout), [])
        self.assertIn("incomplete", result.stderr)

    @patch("sru_lint.madison.cli.IndexCache")
    def test_failure_details_only_in_verbose_mode(self, cache):
        @contextmanager
        def partial(url):
            if "binary-amd64" in url:
                with self.index.open("rb") as index:
                    yield index
            else:
                raise urllib.error.HTTPError(url, 403, "Forbidden", {}, None)

        cache.return_value.open_index.side_effect = partial
        runner = CliRunner()
        args = [
            "madison",
            "-c",
            "maas-3.8",
            "-s",
            "jammy",
            "-a",
            "amd64,arm64",
            "hello",
            "--format",
            "json",
        ]
        failed_url = index_url("maas-3.8", "jammy", "arm64")
        for options, detailed in [
            ([], False),
            (["-v"], True),
            (["-vv"], True),
            (["-v", "-q"], False),
            (["-q"], False),
        ]:
            with self.subTest(options=options):
                result = runner.invoke(app, [*options, *args])
                self.assertEqual(result.exit_code, 1, result.output)
                self.assertEqual(len(json.loads(result.stdout)), 2)
                self.assertIn("Search incomplete: 1 index(es) failed.", result.stderr)
                self.assertEqual(
                    f"Failed {failed_url}: HTTP Error 403: Forbidden" in result.stderr, detailed
                )
                if not detailed:
                    self.assertNotIn(failed_url, result.stderr)


class CacheTests(unittest.TestCase):
    url = "https://example.test/Packages.gz"

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.cache = IndexCache(Path(temp.name))

    def read_index(self):
        with self.cache.open_index(self.url) as index:
            return None if index is None else gzip.decompress(index.read())

    def entries(self):
        return list(self.cache.directory.iterdir())

    @patch("sru_lint.madison.cache.urllib.request.urlopen")
    def test_validator_selection_and_changes(self, urlopen):
        bodies = {'W/"one"': b"one", '"two"': b"two"}
        current = ['W/"one"']

        def request(req, timeout):
            headers = {"ETag": current[0], "Last-Modified": "same date"}
            return Response(
                gzip.compress(bodies[current[0]]) if req.get_method() == "GET" else b"", headers
            )

        urlopen.side_effect = request
        self.assertEqual(self.read_index(), b"one")
        self.assertEqual(self.read_index(), b"one")
        self.assertEqual(
            [c.args[0].get_method() for c in urlopen.call_args_list], ["HEAD", "GET", "HEAD"]
        )
        current[0] = '"two"'
        self.assertEqual(self.read_index(), b"two")
        self.assertEqual(len(self.entries()), 2)
        self.assertTrue(all(path.suffix == ".gz" for path in self.entries()))
        self.assertNotEqual(
            self.cache._target(self.url, ("ETag", "same")),
            self.cache._target(self.url, ("Last-Modified", "same")),
        )
        self.assertNotEqual(
            self.cache._target(self.url, ("ETag", "same")),
            self.cache._target(self.url + "2", ("ETag", "same")),
        )

    @patch("sru_lint.madison.cache.urllib.request.urlopen")
    def test_last_modified_and_missing_validators(self, urlopen):
        for headers, get_count, entry_count in [({"Last-Modified": "date"}, 1, 1), ({}, 2, 0)]:
            with self.subTest(headers=headers):
                self.cache.clear()
                urlopen.reset_mock()
                urlopen.side_effect = lambda req, timeout, headers=headers: Response(
                    gzip.compress(INDEX), headers
                )
                self.assertEqual(self.read_index(), INDEX)
                self.assertEqual(self.read_index(), INDEX)
                self.assertEqual(
                    sum(c.args[0].get_method() == "GET" for c in urlopen.call_args_list), get_count
                )
                self.assertEqual(len(self.entries()), entry_count)

    @patch("sru_lint.madison.cache.urllib.request.urlopen")
    def test_head_unsupported_and_get_validator(self, urlopen):
        for status in (405, 501):
            with self.subTest(status=status):
                self.cache.clear()
                urlopen.side_effect = [
                    http_error(status),
                    Response(gzip.compress(INDEX), {"ETag": "get"}),
                ]
                self.assertEqual(self.read_index(), INDEX)
                self.assertEqual(self.entries(), [self.cache._target(self.url, ("ETag", "get"))])

    @patch("sru_lint.madison.cache.urllib.request.urlopen")
    def test_changed_between_head_and_get(self, urlopen):
        urlopen.side_effect = [
            Response(headers={"ETag": "old"}),
            Response(gzip.compress(b"new"), {"ETag": "new"}),
            Response(headers={"ETag": "new"}),
        ]
        self.assertEqual(self.read_index(), b"new")
        self.assertEqual(self.read_index(), b"new")
        self.assertEqual(self.entries(), [self.cache._target(self.url, ("ETag", "new"))])
        self.cache.clear()
        urlopen.side_effect = [
            Response(headers={"ETag": "old"}),
            Response(gzip.compress(b"new"), {}),
        ]
        self.assertEqual(self.read_index(), b"new")
        self.assertEqual(self.entries(), [])

    @patch("sru_lint.madison.cache.urllib.request.urlopen")
    def test_concurrent_publication(self, urlopen):
        for same in (False, True):
            with self.subTest(same=same):
                self.cache.clear()
                barrier = threading.Barrier(2, timeout=5)
                local = threading.local()

                def request(req, timeout, barrier=barrier, same=same, local=local):
                    if req.get_method() == "HEAD":
                        barrier.wait()
                        return Response(headers={})
                    value = b"same" if same else local.value
                    barrier.wait()
                    return Response(gzip.compress(value), {"ETag": value.decode()})

                def worker(value, local=local):
                    local.value = value
                    return self.read_index()

                urlopen.side_effect = request
                with ThreadPoolExecutor(max_workers=2) as pool:
                    results = list(pool.map(worker, (b"one", b"two")))
                self.assertEqual(results, [b"same", b"same"] if same else [b"one", b"two"])
                self.assertEqual(len(self.entries()), 1 if same else 2)
                for value in (b"same",) if same else (b"one", b"two"):
                    self.assertEqual(
                        gzip.decompress(
                            self.cache._target(self.url, ("ETag", value.decode())).read_bytes()
                        ),
                        value,
                    )

    @patch("sru_lint.madison.cache.urllib.request.urlopen")
    def test_different_urls_download_in_parallel(self, urlopen):
        barrier = threading.Barrier(2, timeout=5)

        def request(req, timeout):
            if req.get_method() == "GET":
                barrier.wait()
            return Response(gzip.compress(req.full_url.encode()))

        def worker(url):
            with self.cache.open_index(url) as index:
                return gzip.decompress(index.read())

        urlopen.side_effect = request
        urls = [self.url, self.url + "2"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(list(pool.map(worker, urls)), [url.encode() for url in urls])

    @patch("sru_lint.madison.cache.urllib.request.urlopen")
    def test_clear_before_open_and_after_open(self, urlopen):
        urlopen.side_effect = lambda req, timeout: Response(gzip.compress(INDEX))
        self.assertEqual(self.read_index(), INDEX)
        original = Path.open
        cleared = False

        def clear_before_open(path, *args, **kwargs):
            nonlocal cleared
            if path.suffix == ".gz" and not cleared:
                cleared = True
                self.cache.clear()
            return original(path, *args, **kwargs)

        with patch.object(Path, "open", clear_before_open):
            with self.cache.open_index(self.url) as index:
                self.cache.clear()  # Fresh publication has occurred, but the descriptor is open.
                self.assertEqual(gzip.decompress(index.read()), INDEX)
                self.assertFalse(index.closed)
            self.assertTrue(index.closed)
        self.assertTrue(cleared)
        self.assertEqual(self.entries(), [])
        self.assertEqual(self.read_index(), INDEX)
        with self.cache.open_index(self.url) as index:
            self.cache.clear()
            self.assertEqual(gzip.decompress(index.read()), INDEX)
        self.assertTrue(index.closed)

    @patch("sru_lint.madison.cache.urllib.request.urlopen")
    def test_invalid_gzip_and_cleanup(self, urlopen):
        for body in (b"", b"bad"):
            with self.subTest(body=body):
                urlopen.side_effect = [Response(headers={}), Response(body, {"ETag": "bad"})]
                with self.assertRaises(gzip.BadGzipFile):
                    self.read_index()
                self.assertEqual(self.entries(), [])
        truncated = Response(gzip.compress(INDEX)[:-4], {"ETag": "truncated"})
        urlopen.side_effect = [Response(headers={}), truncated]
        with self.assertRaises(EOFError):
            self.read_index()
        self.assertTrue(truncated.closed)
        self.assertEqual(self.entries(), [])

        class BrokenResponse(Response):
            def read(self, size=-1):
                raise OSError("download failed")

        broken = BrokenResponse()
        urlopen.side_effect = [Response(headers={}), broken]
        with self.assertRaisesRegex(OSError, "download failed"):
            self.read_index()
        self.assertTrue(broken.closed)
        self.assertEqual(self.entries(), [])
        urlopen.side_effect = lambda req, timeout: Response(gzip.compress(INDEX), {})
        with self.assertRaisesRegex(ValueError, "search"):
            with self.cache.open_index(self.url) as index:
                self.cache.clear()  # Must leave the active temporary file alone.
                self.assertEqual(len(self.entries()), 1)
                raise ValueError("search")
        self.assertTrue(index.closed)
        self.assertEqual(self.entries(), [])

    @patch("sru_lint.madison.cache.urllib.request.urlopen")
    def test_http_errors_and_no_stale_fallback(self, urlopen):
        urlopen.side_effect = lambda req, timeout: Response(gzip.compress(INDEX))
        self.assertEqual(self.read_index(), INDEX)
        for phase in ("HEAD", "GET"):
            for status in (404, 401, 500):
                with self.subTest(phase=phase, status=status):

                    def request(req, timeout, phase=phase, status=status):
                        if req.get_method() == phase:
                            raise http_error(status)
                        return Response(headers={"ETag": "changed"})

                    urlopen.side_effect = request
                    if status == 404:
                        self.assertIsNone(self.read_index())
                    else:
                        with self.assertRaises(urllib.error.HTTPError):
                            self.read_index()
        urlopen.side_effect = OSError("offline")
        with self.assertRaises(OSError):
            self.read_index()
        self.assertEqual(len(self.entries()), 1)
