import unittest
from unittest.mock import MagicMock, patch

from sru_lint.common.errors import ErrorCode
from sru_lint.common.feedback import Severity
from sru_lint.common.madison import MadisonEntry, MadisonResult
from sru_lint.plugins.pro_version_numbers import ProVersionNumbers
from sru_lint.plugins.publishing_history import PublishingHistory
from tests.test_pro_version_numbers import ESM_CHANGELOG, make_processed_file, make_source_span


def entry(version, suite="xenial-infra-security", category="infra"):
    return MadisonEntry("pkg", version, category, suite, "amd64", "pkg", version)


def madison_with(*entries):
    madison = MagicMock()
    madison.source_versions.return_value = MadisonResult(list(entries), [], by_source=True)
    return madison


class TestCheckMadisonVersion(unittest.TestCase):
    def setUp(self):
        self.plugin = PublishingHistory()
        self.plugin.feedback = []
        self.plugin.lp_helper = MagicMock()
        self.span = make_source_span("debian/changelog", ESM_CHANGELOG)

    def run_check(self, *entries, release="xenial"):
        with patch("sru_lint.plugins.plugin_base.get_madison", return_value=madison_with(*entries)):
            self.plugin.check_madison_version(self.span, "pkg", "1.2.3-0ubuntu1~esm2", release)

    def test_disabled_by_default(self):
        self.assertFalse(self.plugin.use_madison)
        with patch("sru_lint.plugins.plugin_base.get_madison") as get:
            self.plugin.check_madison_version(self.span, "pkg", "1.2.3-0ubuntu1~esm2", "xenial")
        get.assert_not_called()

    def test_collision(self):
        self.plugin.use_madison = True
        self.run_check(entry("1.2.3-0ubuntu1~esm2"))
        self.assertEqual(len(self.plugin.feedback), 1)
        item = self.plugin.feedback[0]
        self.assertEqual(item.rule_id, ErrorCode.MADISON_VERSION_COLLISION)
        self.assertEqual(item.severity, Severity.WARNING)
        self.assertIn("infra/xenial-infra-security", item.message)

    def test_higher_version(self):
        self.plugin.use_madison = True
        self.run_check(entry("1.2.3-0ubuntu1~esm3"), release="xenial-security")
        self.assertEqual(len(self.plugin.feedback), 1)
        self.assertEqual(self.plugin.feedback[0].rule_id, ErrorCode.MADISON_HIGHER_VERSION_EXISTS)
        self.assertIn("1.2.3-0ubuntu1~esm3", self.plugin.feedback[0].message)

    def test_lower_version_and_other_release_ignored(self):
        self.plugin.use_madison = True
        self.run_check(
            entry("1.2.3-0ubuntu1~esm1"),
            entry("1.2.3-0ubuntu1~esm9", suite="bionic-infra-security"),
        )
        self.assertEqual(self.plugin.feedback, [])

    def test_lookup_failure_is_not_fatal(self):
        self.plugin.use_madison = True
        madison = MagicMock()
        madison.source_versions.side_effect = OSError("boom")
        with patch("sru_lint.plugins.plugin_base.get_madison", return_value=madison):
            self.plugin.check_madison_version(self.span, "pkg", "1.0", "xenial")
        self.assertEqual(self.plugin.feedback, [])


class TestProPluginMadison(unittest.TestCase):
    @patch("sru_lint.plugins.pro_version_numbers.is_esm_only_release", return_value=True)
    def test_pro_plugin_uses_madison_when_enabled(self, _is_esm):
        plugin = ProVersionNumbers()
        plugin.feedback = []
        plugin.lp_helper = MagicMock()
        plugin.lp_helper.get_highest_version_in_ppa.return_value = None
        plugin.use_madison = True
        pf = make_processed_file("debian/changelog", ESM_CHANGELOG)
        with patch(
            "sru_lint.plugins.plugin_base.get_madison",
            return_value=madison_with(entry("1.2.3-0ubuntu1~esm2")),
        ):
            plugin.process_file(pf)
        self.assertEqual(
            [f.rule_id for f in plugin.feedback], [ErrorCode.MADISON_VERSION_COLLISION]
        )


if __name__ == "__main__":
    unittest.main()
