import unittest
from unittest.mock import MagicMock, patch

from sru_lint.common.errors import ErrorCode
from sru_lint.common.feedback import Severity, SourceLine, SourceSpan
from sru_lint.madison.search import Result
from sru_lint.madison.service import Lookup
from sru_lint.plugins.plugin_base import ProcessedFile
from sru_lint.plugins.pro_version_numbers import ProVersionNumbers


def make_source_span(path, lines_content, lines_added_indices=None, start_line=1):
    if lines_added_indices is None:
        lines_added_indices = list(range(len(lines_content)))

    lines_added = []
    lines_with_context = []
    for i, content in enumerate(lines_content):
        line_number = start_line + i
        is_added = i in lines_added_indices
        source_line = SourceLine(content=content, line_number=line_number, is_added=is_added)
        lines_with_context.append(source_line)
        if is_added:
            lines_added.append(source_line)

    return SourceSpan(
        path=path,
        start_line=start_line,
        start_col=1,
        end_line=start_line + len(lines_content) - 1,
        end_col=1,
        content=lines_added,
        content_with_context=lines_with_context,
    )


def make_processed_file(path, lines_content, lines_added_indices=None, start_line=1):
    source_span = make_source_span(path, lines_content, lines_added_indices, start_line)
    return ProcessedFile(path=path, source_span=source_span)


ESM_CHANGELOG = [
    "pkg (1.2.3-0ubuntu1~esm2) xenial; urgency=medium",
    "",
    "  * Security fix",
    "",
    " -- Author <author@example.com>  Mon, 01 Jan 2024 12:00:00 +0000",
]

REGULAR_CHANGELOG = [
    "pkg (1.2.3-0ubuntu1) jammy; urgency=medium",
    "",
    "  * Some change",
    "",
    " -- Author <author@example.com>  Mon, 01 Jan 2024 12:00:00 +0000",
]

UNRELEASED_CHANGELOG = [
    "pkg (1.2.3-0ubuntu1~esm2) UNRELEASED; urgency=medium",
    "",
    "  * WIP",
    "",
    " -- Author <author@example.com>  Mon, 01 Jan 2024 12:00:00 +0000",
]

ESM_POCKET_CHANGELOG = [
    "pkg (1.2.3-0ubuntu1~esm2) xenial-security; urgency=medium",
    "",
    "  * Security fix",
    "",
    " -- Author <author@example.com>  Mon, 01 Jan 2024 12:00:00 +0000",
]


class TestProVersionNumbers(unittest.TestCase):
    def setUp(self):
        self.plugin = ProVersionNumbers()
        self.plugin.feedback = []
        self.mock_lp = MagicMock()
        self.mock_lp.get_highest_version_in_ppa.return_value = None
        self.plugin.lp_helper = self.mock_lp

    def test_symbolic_name(self):
        self.assertEqual(self.plugin.__symbolic_name__, "pro-version-numbers")

    def test_register_file_patterns(self):
        self.assertTrue(self.plugin.matches_file("debian/changelog"))
        self.assertTrue(self.plugin.matches_file("pkg/debian/changelog"))
        self.assertFalse(self.plugin.matches_file("debian/control"))

    def test_empty_added_content_is_no_op(self):
        # All lines marked as context (none added) → plugin returns early.
        pf = make_processed_file("debian/changelog", ESM_CHANGELOG, lines_added_indices=[])
        self.plugin.process_file(pf)
        self.assertEqual(self.plugin.feedback, [])
        self.mock_lp.get_highest_version_in_ppa.assert_not_called()

    @patch("sru_lint.plugins.pro_version_numbers.is_esm_only_release", return_value=False)
    def test_skips_non_esm_release(self, _is_esm):
        pf = make_processed_file("debian/changelog", REGULAR_CHANGELOG)
        self.plugin.process_file(pf)
        self.assertEqual(self.plugin.feedback, [])
        self.mock_lp.get_highest_version_in_ppa.assert_not_called()

    @patch("sru_lint.plugins.pro_version_numbers.is_esm_only_release", return_value=True)
    def test_skips_unreleased(self, _is_esm):
        pf = make_processed_file("debian/changelog", UNRELEASED_CHANGELOG)
        self.plugin.process_file(pf)
        self.assertEqual(self.plugin.feedback, [])
        # UNRELEASED short-circuits before is_esm_only_release is even consulted,
        # so the PPA helper must also not be touched.
        self.mock_lp.get_highest_version_in_ppa.assert_not_called()

    @patch("sru_lint.plugins.pro_version_numbers.is_esm_only_release", return_value=True)
    def test_package_not_in_any_ppa(self, _is_esm):
        pf = make_processed_file("debian/changelog", ESM_CHANGELOG)
        self.mock_lp.get_highest_version_in_ppa.return_value = None
        self.plugin.process_file(pf)
        self.assertEqual(self.plugin.feedback, [])

    @patch("sru_lint.plugins.pro_version_numbers.is_esm_only_release", return_value=True)
    def test_changelog_version_higher_than_all_ppas(self, _is_esm):
        pf = make_processed_file("debian/changelog", ESM_CHANGELOG)
        # Every PPA holds a strictly-lower version (~esm1 < ~esm2).
        self.mock_lp.get_highest_version_in_ppa.return_value = "1.2.3-0ubuntu1~esm1"
        self.plugin.process_file(pf)
        self.assertEqual(self.plugin.feedback, [])

    @patch("sru_lint.plugins.pro_version_numbers.is_esm_only_release", return_value=True)
    def test_changelog_version_equal_to_ppa_is_flagged(self, _is_esm):
        pf = make_processed_file("debian/changelog", ESM_CHANGELOG)
        # PPA already publishes the same version → not strictly higher → error.
        self.mock_lp.get_highest_version_in_ppa.return_value = "1.2.3-0ubuntu1~esm2"
        self.plugin.process_file(pf)
        self.assertEqual(len(self.plugin.feedback), 1)
        fb = self.plugin.feedback[0]
        self.assertEqual(fb.rule_id, ErrorCode.PRO_VERSION_NOT_HIGHER)
        self.assertEqual(fb.severity, Severity.ERROR)
        self.assertIn("1.2.3-0ubuntu1~esm2", fb.message)

    @patch("sru_lint.plugins.pro_version_numbers.is_esm_only_release", return_value=True)
    def test_changelog_version_lower_than_ppa_is_flagged(self, _is_esm):
        pf = make_processed_file("debian/changelog", ESM_CHANGELOG)
        self.mock_lp.get_highest_version_in_ppa.return_value = "1.2.3-0ubuntu1~esm5"
        self.plugin.process_file(pf)
        self.assertEqual(len(self.plugin.feedback), 1)
        self.assertEqual(self.plugin.feedback[0].rule_id, ErrorCode.PRO_VERSION_NOT_HIGHER)
        self.assertIn("1.2.3-0ubuntu1~esm5", self.plugin.feedback[0].message)

    @patch("sru_lint.plugins.pro_version_numbers.is_esm_only_release", return_value=True)
    def test_max_across_ppas_is_used(self, _is_esm):
        """Comparison uses the highest version found across all PPAs, and the
        feedback message names the PPA that holds it."""
        pf = make_processed_file("debian/changelog", ESM_CHANGELOG)

        # One PPA holds a stale low version, another holds a newer one that
        # should drive the comparison; the rest return None.
        ppa_versions = {
            "ppa:ubuntu-esm/esm-infra-security": "1.2.3-0ubuntu1~esm1",
            "ppa:ubuntu-esm/esm-apps-security": "1.2.3-0ubuntu1~esm10",
        }
        self.mock_lp.get_highest_version_in_ppa.side_effect = lambda ppa, _pkg: ppa_versions.get(
            ppa
        )

        self.plugin.process_file(pf)

        self.assertEqual(len(self.plugin.feedback), 1)
        fb = self.plugin.feedback[0]
        self.assertEqual(fb.rule_id, ErrorCode.PRO_VERSION_NOT_HIGHER)
        self.assertIn("1.2.3-0ubuntu1~esm10", fb.message)
        self.assertIn("ppa:ubuntu-esm/esm-apps-security", fb.message)

    @patch("sru_lint.plugins.pro_version_numbers.is_esm_only_release", return_value=True)
    def test_ppa_query_errors_are_non_fatal(self, _is_esm):
        """A failing PPA query is logged and skipped; the rest still drive the
        comparison."""
        pf = make_processed_file("debian/changelog", ESM_CHANGELOG)

        def side_effect(ppa, _pkg):
            if ppa == "ppa:ubuntu-esm/esm-infra-security":
                raise RuntimeError("transient API outage")
            if ppa == "ppa:ubuntu-esm/esm-apps-security":
                return "1.2.3-0ubuntu1~esm10"
            return None

        self.mock_lp.get_highest_version_in_ppa.side_effect = side_effect

        self.plugin.process_file(pf)

        self.assertEqual(len(self.plugin.feedback), 1)
        self.assertEqual(self.plugin.feedback[0].rule_id, ErrorCode.PRO_VERSION_NOT_HIGHER)

    @patch("sru_lint.plugins.pro_version_numbers.is_esm_only_release", return_value=True)
    def test_pocket_suffix_does_not_skip_check(self, _is_esm):
        """``xenial-security`` should still be recognized as ESM-only after the
        pocket suffix is stripped — i.e. the plugin proceeds to query PPAs."""
        pf = make_processed_file("debian/changelog", ESM_POCKET_CHANGELOG)
        self.mock_lp.get_highest_version_in_ppa.return_value = "1.2.3-0ubuntu1~esm1"
        self.plugin.process_file(pf)
        self.assertEqual(self.plugin.feedback, [])
        self.mock_lp.get_highest_version_in_ppa.assert_called()

    @patch("sru_lint.plugins.pro_version_numbers.is_esm_only_release", return_value=False)
    def test_madison_checks_target_release_before_esm_only(self, _is_esm):
        self.plugin.madison = MagicMock()
        self.plugin.madison.lookup_source.return_value = Lookup(
            (Result("pkg-bin", "1.2.3-0ubuntu2", "infra", "jammy-infra-security", ("arm64",)),), ()
        )
        pf = make_processed_file("debian/changelog", REGULAR_CHANGELOG)
        self.plugin.process_file(pf)
        self.plugin.madison.lookup_source.assert_called_once_with("pkg", "jammy")
        self.mock_lp.get_highest_version_in_ppa.assert_not_called()
        self.assertEqual(self.plugin.feedback[0].rule_id, ErrorCode.PRO_VERSION_NOT_HIGHER)
        self.assertIn("infra/jammy-infra-security", self.plugin.feedback[0].message)

    @patch("sru_lint.plugins.pro_version_numbers.is_esm_only_release", return_value=True)
    def test_madison_uses_debian_order_and_accepts_newer_version(self, _is_esm):
        self.plugin.madison = MagicMock()
        self.plugin.madison.lookup_source.return_value = Lookup(
            (
                Result("pkg", "1.2.3-0ubuntu1~esm1", "apps", "xenial", ("amd64",)),
                Result(
                    "pkg-bin", "1.2.3-0ubuntu1~esm10", "infra", "xenial-infra-security", ("arm64",)
                ),
            ),
            (),
        )
        self.plugin.process_file(make_processed_file("debian/changelog", ESM_CHANGELOG))
        self.assertIn("~esm10", self.plugin.feedback[0].message)
        self.plugin.feedback.clear()
        self.plugin.madison.lookup_source.return_value = Lookup((), ())
        self.plugin.process_file(make_processed_file("debian/changelog", ESM_CHANGELOG))
        self.assertEqual(self.plugin.feedback, [])
        self.plugin.madison.lookup_source.return_value = Lookup(
            (Result("pkg", "1.2.3-0ubuntu1~esm1", "apps", "xenial", ("amd64",)),), ()
        )
        self.plugin.process_file(make_processed_file("debian/changelog", ESM_CHANGELOG))
        self.assertEqual(self.plugin.feedback, [])

    @patch("sru_lint.plugins.pro_version_numbers.is_esm_only_release", return_value=True)
    def test_madison_incomplete_and_partial_results(self, _is_esm):
        self.plugin.madison = MagicMock()
        self.plugin.madison.lookup_source.return_value = Lookup(
            (Result("pkg", "1.2.3-0ubuntu1~esm2", "apps", "xenial", ("amd64",)),),
            ("network failure",),
        )
        self.plugin.process_file(make_processed_file("debian/changelog", ESM_CHANGELOG))
        self.assertEqual(
            [item.rule_id for item in self.plugin.feedback],
            [ErrorCode.PRO_VERSION_LOOKUP_FAILED, ErrorCode.PRO_VERSION_NOT_HIGHER],
        )

    @patch("sru_lint.plugins.pro_version_numbers.is_esm_only_release", return_value=True)
    def test_madison_preserves_staging_and_source_only_ppa_checks(self, _is_esm):
        staging = "ppa:ubuntu-esm/esm-infra-security-staging"
        for lookup in (Lookup((), ()), Lookup((), ("network failure",))):
            with self.subTest(failures=lookup.failures):
                self.plugin.feedback.clear()
                self.plugin.madison = MagicMock()
                self.plugin.madison.lookup_source.return_value = lookup
                self.mock_lp.get_highest_version_in_ppa.side_effect = lambda ppa, _pkg: (
                    "1.2.3-0ubuntu1~esm5" if ppa == staging else None
                )
                self.plugin.process_file(make_processed_file("debian/changelog", ESM_CHANGELOG))
                errors = [fb for fb in self.plugin.feedback if fb.severity == Severity.ERROR]
                self.assertEqual(len(errors), 1)
                self.assertEqual(errors[0].rule_id, ErrorCode.PRO_VERSION_NOT_HIGHER)
                self.assertIn(staging, errors[0].message)
                self.assertIn("~esm5", errors[0].message)

    @patch("sru_lint.plugins.pro_version_numbers.is_esm_only_release", return_value=True)
    def test_madison_and_ppa_use_combined_maximum_without_duplicate_errors(self, _is_esm):
        self.plugin.madison = MagicMock()
        self.plugin.madison.lookup_source.return_value = Lookup(
            (Result("pkg", "1.2.3-0ubuntu1~esm5", "apps", "xenial-apps-security", ("amd64",)),),
            (),
        )
        for ppa_version, origin in (
            ("1.2.3-0ubuntu1~esm3", "apps/xenial-apps-security"),
            ("1.2.3-0ubuntu1~esm10", "ppa:ubuntu-esm/"),
        ):
            with self.subTest(ppa_version=ppa_version):
                self.plugin.feedback.clear()
                self.mock_lp.get_highest_version_in_ppa.return_value = ppa_version
                self.plugin.process_file(make_processed_file("debian/changelog", ESM_CHANGELOG))
                self.assertEqual(len(self.plugin.feedback), 1)
                self.assertIn(origin, self.plugin.feedback[0].message)

    @patch("sru_lint.plugins.pro_version_numbers.is_esm_only_release", return_value=True)
    def test_madison_uses_base_release_for_pocket_changelog(self, _is_esm):
        self.plugin.madison = MagicMock()
        self.plugin.madison.lookup_source.return_value = Lookup((), ())
        self.plugin.process_file(make_processed_file("debian/changelog", ESM_POCKET_CHANGELOG))
        self.plugin.madison.lookup_source.assert_called_once_with("pkg", "xenial")
        self.mock_lp.get_highest_version_in_ppa.assert_called()

    def test_madison_skips_unreleased(self):
        self.plugin.madison = MagicMock()
        self.plugin.process_file(make_processed_file("debian/changelog", UNRELEASED_CHANGELOG))
        self.plugin.madison.lookup_source.assert_not_called()

    def test_base_release_helper(self):
        f = ProVersionNumbers._base_release
        self.assertEqual(f("xenial"), "xenial")
        self.assertEqual(f("xenial-security"), "xenial")
        self.assertEqual(f("xenial-proposed"), "xenial")
        self.assertEqual(f("xenial-updates"), "xenial")
        self.assertEqual(f("xenial-backports"), "xenial")
        self.assertEqual(f("xenial xenial-security"), "xenial")
        self.assertIsNone(f(""))
        self.assertIsNone(f("UNRELEASED"))


if __name__ == "__main__":
    unittest.main()
