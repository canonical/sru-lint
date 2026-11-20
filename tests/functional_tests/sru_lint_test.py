"""
sru-lint test module.
"""

import json
import os
import subprocess
import urllib.request

import pytest

from .debdiff_generator import DebdiffGenerator

GENERATED_DEBDIFFS_DIR = os.path.join(os.path.dirname(__file__), "generated_debdiffs")


class TestSruLint:
    """Test class for sru-lint tool."""

    @classmethod
    def setup_class(cls):
        """Create output directories."""
        os.makedirs(GENERATED_DEBDIFFS_DIR, exist_ok=True)

    @staticmethod
    def run_sru_lint(*args, timeout=60):
        """Run sru-lint with the given arguments and return the result."""
        return subprocess.run(
            ["sru-lint", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    @staticmethod
    def assert_success(result):
        """Assert that sru-lint exited with return code 0."""
        assert result.returncode == 0, (
            f"sru-lint exited with code {result.returncode}: {result.stderr}"
        )

    @staticmethod
    def assert_output_contains(result, text):
        """Assert that stdout contains the given text (case-insensitive)."""
        assert text.lower() in result.stdout.lower(), (
            f"Expected '{text}' in sru-lint output, got:\n{result.stdout}"
        )

    def test_sru_lint_help(self):
        """Verify that sru-lint --help runs successfully."""
        result = self.run_sru_lint("--help")
        self.assert_success(result)
        self.assert_output_contains(result, "usage")

    def test_check_generated_debdiff(self, timeout=60):
        """Verify that sru-lint check passes on a generated debdiff."""
        debdiff_file = DebdiffGenerator.generate_to_file(
            output_dir=GENERATED_DEBDIFFS_DIR,
            filename="netplan_io_noble.debdiff",
            package="netplan.io",
            series="noble",
            lp_bug="2139598",
        )
        result = self.run_sru_lint("check", debdiff_file, timeout=120)
        self.assert_success(result)

    def test_check_debdiff_from_launchpad_url(self):
        """Verify sru-lint fetches and lints a debdiff from a Launchpad URL.

        The debdiff has an outdated version so sru-lint should fail with
        publishing-history errors only (no other unexpected errors).
        """
        url = (
            "https://bugs.launchpad.net/ubuntu/+source/opensc/+bug/2127205"
            "/+attachment/5940944/+files/resolute.debdiff"
        )

        # Verify the URL is reachable before running sru-lint
        req = urllib.request.Request(url, method="HEAD")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                assert resp.status == 200, f"Debdiff URL returned status {resp.status}"
        except Exception as exc:  # pylint: disable=broad-except
            pytest.skip(f"Debdiff URL not reachable: {exc}")

        result = self.run_sru_lint("check", "-f", "json", url, timeout=60)

        # sru-lint should exit with 1 (lint errors found)
        assert result.returncode == 1, (
            f"Expected exit code 1, got {result.returncode}.\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

        # Parse JSON findings from stdout (ignore stderr log lines)
        stdout_lines = result.stdout.strip().splitlines()
        json_start = next(i for i, line in enumerate(stdout_lines) if line.strip().startswith("["))
        json_text = "\n".join(stdout_lines[json_start:])
        findings = json.loads(json_text)

        assert findings, "Expected at least one finding from sru-lint"

        # All findings should be publishing-history, changelog-related, or patch-related
        # (The bug may or may not be targeted depending on Launchpad access,
        # and patches may diverge from upstream)
        allowed_rule_ids = {"PUBHIST001", "PUBHIST003", "CHANGELOG002", "PATCH008"}
        for finding in findings:
            assert finding["rule_id"] in allowed_rule_ids, f"Unexpected finding: {finding}"
