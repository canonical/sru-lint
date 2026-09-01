import unittest
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from sru_lint.common.launchpad_helper import LaunchpadHelper, ProPublication
from sru_lint.promadison import app


class TestPromadison(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()

    @patch("sru_lint.promadison.LaunchpadHelper")
    def test_lists_publications_with_stream_and_pocket(self, mock_helper):
        mock_helper.return_value.get_pro_publications.return_value = [
            ProPublication("1.0~esm2", "xenial", "apps", "security"),
            ProPublication("1.0~esm1", "xenial", "infra", "security"),
        ]

        result = self.runner.invoke(app, ["example"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(
            result.output.splitlines(),
            [
                "example | 1.0~esm2 | xenial | apps | security",
                "example | 1.0~esm1 | xenial | infra | security",
            ],
        )
        mock_helper.return_value.get_pro_publications.assert_called_once_with("example")

    def test_derives_stream_and_pocket_from_esm_archive_name(self):
        publication = MagicMock()
        publication.source_package_version = "1.0~esm2"
        publication.distro_series.name = "xenial"
        publication.pocket = "Release"
        helper = LaunchpadHelper()
        helper.PRO_PPAS = [
            "ppa:ubuntu-esm/esm-infra-legacy-updates",
            "ppa:ubuntu-esm/esm-infra-security-staging",
        ]

        with patch.object(helper, "_get_published_sources_in_ppa", return_value=[publication]):
            publications = list(helper.get_pro_publications("example"))

        self.assertEqual(
            publications,
            [
                ProPublication("1.0~esm2", "xenial", "infra-legacy", "updates"),
                ProPublication("1.0~esm2", "xenial", "infra", "security-staging"),
            ],
        )

    @patch("sru_lint.promadison.LaunchpadHelper")
    def test_reports_no_results_with_no_output(self, mock_helper):
        mock_helper.return_value.get_pro_publications.return_value = []
        result = self.runner.invoke(app, ["missing"])

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(result.output, "")


if __name__ == "__main__":
    unittest.main()
