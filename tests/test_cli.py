# for CI environments that may not support colors
import os
import unittest
from unittest.mock import MagicMock, mock_open, patch

from typer.testing import CliRunner

from sru_lint.cli import app
from sru_lint.common.feedback import FeedbackItem, Severity, SourceSpan
from sru_lint.plugins.plugin_base import ProcessedFile

os.environ["TERM"] = "dumb"
os.environ["NO_COLOR"] = "1"
os.environ["FORCE_COLOR"] = "0"


class TestCLI(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()

    def _create_mock_source_span(
        self, path="test.py", start_line=1, start_col=1, end_line=1, end_col=1
    ):
        """Helper to create a properly mocked SourceSpan"""
        mock_span = MagicMock(spec=SourceSpan)
        mock_span.path = path
        mock_span.start_line = start_line
        mock_span.start_col = start_col
        mock_span.end_line = end_line
        mock_span.end_col = end_col
        # Mock lines_added as empty list to avoid issues with snippet rendering
        mock_span.lines_added = []
        return mock_span

    @patch("sru_lint.cli.MadisonService")
    @patch("sru_lint.cli.process_patch_content")
    @patch("sru_lint.cli.PluginManager")
    def test_madison_flag_shares_service(self, manager, process_patch, service):
        plugins = [MagicMock(), MagicMock()]
        for i, plugin in enumerate(plugins):
            plugin.__symbolic_name__ = f"plugin{i}"
            plugin.feedback = []
        manager.return_value.load_plugins.return_value = plugins
        process_patch.return_value = [ProcessedFile(path="test", source_span=MagicMock())]
        result = self.runner.invoke(
            app, ["check", "--use-madison", "-f", "json", "-"], input="patch"
        )
        self.assertEqual(result.exit_code, 0, result.output)
        service.assert_called_once_with()
        for plugin in plugins:
            self.assertIs(plugin.madison, service.return_value)

    @patch("sru_lint.cli.render_snippet")
    @patch("sru_lint.cli.process_patch_content")
    @patch("sru_lint.cli.PluginManager")
    def test_check_command_default_modules(
        self, mock_plugin_manager, mock_process_patch, mock_render_snippet
    ):
        """Test check command with default 'all' modules"""
        mock_pm = MagicMock()
        mock_plugin_manager.return_value = mock_pm

        mock_plugin1 = MagicMock()
        mock_plugin1.__symbolic_name__ = "plugin1"
        mock_plugin1.process.return_value = []
        mock_plugin2 = MagicMock()
        mock_plugin2.__symbolic_name__ = "plugin2"
        mock_plugin2.process.return_value = []
        mock_pm.load_plugins.return_value = [mock_plugin1, mock_plugin2]

        # Mock processed files
        mock_processed_files = [
            ProcessedFile(path="test.py", source_span=MagicMock()),
            ProcessedFile(path="test2.py", source_span=MagicMock()),
        ]
        mock_process_patch.return_value = mock_processed_files

        # Use stdin input directly
        result = self.runner.invoke(app, ["check", "-"], input="patch content")

        self.assertEqual(result.exit_code, 0)
        mock_process_patch.assert_called_once_with("patch content")
        mock_plugin1.process.assert_called_once_with(mock_processed_files)
        mock_plugin2.process.assert_called_once_with(mock_processed_files)

    @patch("sru_lint.cli.render_snippet")
    @patch("sru_lint.cli.process_patch_content")
    @patch("sru_lint.cli.PluginManager")
    def test_check_command_specific_modules(
        self, mock_plugin_manager, mock_process_patch, mock_render_snippet
    ):
        """Test check command with specific modules"""
        mock_pm = MagicMock()
        mock_plugin_manager.return_value = mock_pm

        mock_plugin1 = MagicMock()
        mock_plugin1.__symbolic_name__ = "plugin1"
        mock_plugin1.process.return_value = []
        mock_plugin2 = MagicMock()
        mock_plugin2.__symbolic_name__ = "plugin2"
        mock_plugin2.process.return_value = []
        mock_pm.load_plugins.return_value = [mock_plugin1, mock_plugin2]

        mock_processed_files = [ProcessedFile(path="test.py", source_span=MagicMock())]
        mock_process_patch.return_value = mock_processed_files

        result = self.runner.invoke(
            app, ["check", "-", "--modules", "plugin1"], input="patch content"
        )

        self.assertEqual(result.exit_code, 0)
        mock_plugin1.process.assert_called_once_with(mock_processed_files)
        mock_plugin2.process.assert_not_called()

    @patch("sru_lint.cli.render_snippet")
    @patch("sru_lint.cli.process_patch_content")
    @patch("sru_lint.cli.PluginManager")
    def test_check_command_comma_separated_modules(
        self, mock_plugin_manager, mock_process_patch, mock_render_snippet
    ):
        """Test check command with comma-separated modules"""
        mock_pm = MagicMock()
        mock_plugin_manager.return_value = mock_pm

        mock_plugin1 = MagicMock()
        mock_plugin1.__symbolic_name__ = "plugin1"
        mock_plugin1.process.return_value = []
        mock_plugin2 = MagicMock()
        mock_plugin2.__symbolic_name__ = "plugin2"
        mock_plugin2.process.return_value = []
        mock_plugin3 = MagicMock()
        mock_plugin3.__symbolic_name__ = "plugin3"
        mock_plugin3.process.return_value = []
        mock_pm.load_plugins.return_value = [mock_plugin1, mock_plugin2, mock_plugin3]

        mock_processed_files = [ProcessedFile(path="test.py", source_span=MagicMock())]
        mock_process_patch.return_value = mock_processed_files

        result = self.runner.invoke(
            app, ["check", "-", "--modules", "plugin1,plugin2"], input="patch content"
        )

        self.assertEqual(result.exit_code, 0)
        mock_plugin1.process.assert_called_once_with(mock_processed_files)
        mock_plugin2.process.assert_called_once_with(mock_processed_files)
        mock_plugin3.process.assert_not_called()

    @patch("sru_lint.cli.render_snippet")
    @patch("sru_lint.cli.process_patch_content")
    @patch("sru_lint.cli.PluginManager")
    def test_check_command_nonexistent_modules(
        self, mock_plugin_manager, mock_process_patch, mock_render_snippet
    ):
        """Test check command with nonexistent modules"""
        mock_pm = MagicMock()
        mock_plugin_manager.return_value = mock_pm

        mock_plugin1 = MagicMock()
        mock_plugin1.__symbolic_name__ = "plugin1"
        mock_pm.load_plugins.return_value = [mock_plugin1]

        mock_processed_files = [ProcessedFile(path="test.py", source_span=MagicMock())]
        mock_process_patch.return_value = mock_processed_files

        result = self.runner.invoke(
            app, ["check", "-", "--modules", "nonexistent"], input="patch content"
        )

        self.assertEqual(result.exit_code, 0)
        self.assertIn("Available modules:", result.stdout)
        self.assertIn("plugin1", result.stdout)

    @patch("sru_lint.cli.render_snippet")
    @patch("sru_lint.cli.process_patch_content")
    @patch("sru_lint.cli.PluginManager")
    def test_check_command_with_errors(
        self, mock_plugin_manager, mock_process_patch, mock_render_snippet
    ):
        """Test check command with plugins that return errors"""
        mock_pm = MagicMock()
        mock_plugin_manager.return_value = mock_pm

        # Create a mock feedback item with error severity
        error_feedback = FeedbackItem(
            message="Test error",
            span=self._create_mock_source_span("test.py", 1, 1, 1, 1),
            rule_id="TEST001",
            severity=Severity.ERROR,
        )

        mock_plugin1 = MagicMock()
        mock_plugin1.__symbolic_name__ = "plugin1"
        mock_plugin1.process.return_value = [error_feedback]
        mock_plugin1.feedback = [error_feedback]
        mock_pm.load_plugins.return_value = [mock_plugin1]

        mock_processed_files = [ProcessedFile(path="test.py", source_span=MagicMock())]
        mock_process_patch.return_value = mock_processed_files

        result = self.runner.invoke(app, ["check", "-"], input="patch content")

        self.assertEqual(result.exit_code, 1)  # Should exit with error code due to errors
        self.assertIn("Test error", result.stdout)

    @patch("sru_lint.cli.render_snippet")
    @patch("sru_lint.cli.process_patch_content")
    @patch("sru_lint.cli.PluginManager")
    def test_check_command_with_warnings_only(
        self, mock_plugin_manager, mock_process_patch, mock_render_snippet
    ):
        """Test check command with plugins that return only warnings"""
        mock_pm = MagicMock()
        mock_plugin_manager.return_value = mock_pm

        # Create a mock feedback item with warning severity
        warning_feedback = FeedbackItem(
            message="Test warning",
            span=self._create_mock_source_span("test.py", 1, 1, 1, 1),
            rule_id="TEST002",
            severity=Severity.WARNING,
        )

        mock_plugin1 = MagicMock()
        mock_plugin1.__symbolic_name__ = "plugin1"
        mock_plugin1.process.return_value = [warning_feedback]
        mock_plugin1.feedback = [warning_feedback]
        mock_pm.load_plugins.return_value = [mock_plugin1]

        mock_processed_files = [ProcessedFile(path="test.py", source_span=MagicMock())]
        mock_process_patch.return_value = mock_processed_files

        result = self.runner.invoke(app, ["check", "-"], input="patch content")

        self.assertEqual(
            result.exit_code, 0
        )  # Should exit successfully (warnings don't cause exit code 1)
        self.assertIn("Test warning", result.stdout)

    @patch("sru_lint.cli.render_snippet")
    @patch("sru_lint.cli.process_patch_content")
    @patch("sru_lint.cli.PluginManager")
    def test_check_command_no_files_in_patch(
        self, mock_plugin_manager, mock_process_patch, mock_render_snippet
    ):
        """Test check command when patch contains no files"""
        mock_pm = MagicMock()
        mock_plugin_manager.return_value = mock_pm
        mock_pm.load_plugins.return_value = []

        # Mock empty processed files
        mock_process_patch.return_value = []

        result = self.runner.invoke(app, ["check", "-"], input="empty patch")

        self.assertEqual(result.exit_code, 2)  # Should exit with error code for no files

    @patch("sru_lint.cli.render_snippet")
    @patch("sru_lint.cli.process_patch_content")
    @patch("sru_lint.cli.PluginManager")
    def test_check_command_patch_parse_failure(
        self, mock_plugin_manager, mock_process_patch, mock_render_snippet
    ):
        """Test check command when patch parsing fails"""
        mock_pm = MagicMock()
        mock_plugin_manager.return_value = mock_pm
        mock_pm.load_plugins.return_value = []

        # Mock patch processing failure
        mock_process_patch.return_value = []

        result = self.runner.invoke(app, ["check", "-"], input="invalid patch")

        self.assertEqual(result.exit_code, 2)

    @patch("sru_lint.cli.PluginManager")
    def test_plugins_command_with_plugins(self, mock_plugin_manager):
        """Test plugins command when plugins are available"""
        mock_pm = MagicMock()
        mock_plugin_manager.return_value = mock_pm

        mock_plugin1 = MagicMock()
        mock_plugin1.__class__.__name__ = "TestPlugin1"
        mock_plugin1.__symbolic_name__ = "test-plugin1"
        mock_plugin1.__class__.__doc__ = "Test plugin description"
        mock_plugin1.__class__.__module__ = "test.module1"
        mock_plugin2 = MagicMock()
        mock_plugin2.__class__.__name__ = "TestPlugin2"
        mock_plugin2.__symbolic_name__ = "test-plugin2"
        mock_plugin2.__class__.__doc__ = None
        mock_plugin2.__class__.__module__ = "test.module2"

        mock_pm.load_plugins.return_value = [mock_plugin1, mock_plugin2]

        result = self.runner.invoke(app, ["plugins"])

        self.assertEqual(result.exit_code, 0)
        self.assertIn("Available plugins:", result.stdout)
        # Check for aligned output - both plugin names should be padded to the same width
        # The longer name 'test-plugin2' (12 chars) determines the alignment
        self.assertIn("test-plugin1 : Test plugin description", result.stdout)
        self.assertIn("test-plugin2 : No description available", result.stdout)

    @patch("sru_lint.cli.PluginManager")
    def test_plugins_command_no_plugins(self, mock_plugin_manager):
        """Test plugins command when no plugins are available"""
        mock_pm = MagicMock()
        mock_plugin_manager.return_value = mock_pm
        mock_pm.load_plugins.return_value = []

        result = self.runner.invoke(app, ["plugins"])

        self.assertEqual(result.exit_code, 0)
        self.assertIn("Available plugins:", result.stdout)
        self.assertIn("No plugins found.", result.stdout)

    def test_inspect_command(self):
        """Test inspect command"""
        result = self.runner.invoke(app, ["inspect"])

        self.assertEqual(result.exit_code, 0)
        self.assertIn("Inspecting code...", result.stdout)

    def test_help_command_no_args(self):
        """Test help command without arguments"""
        result = self.runner.invoke(app, ["help"])

        self.assertEqual(result.exit_code, 0)
        # Should show the general help with usage info
        self.assertIn("sru-lint - Static analysis tool for Ubuntu SRU patches", result.stdout)
        self.assertIn(
            "Usage: root [OPTIONS] COMMAND [ARGS]...", result.stdout
        )  # Updated to match actual output
        self.assertIn("Commands", result.stdout)  # Updated to match actual output (without colon)
        self.assertIn("check", result.stdout)
        self.assertIn("plugins", result.stdout)
        self.assertIn("inspect", result.stdout)
        self.assertIn("help", result.stdout)  # Added since help command is also listed

    def test_help_command_with_valid_command(self):
        """Test help command with valid subcommand"""
        result = self.runner.invoke(app, ["help", "check"])

        self.assertEqual(result.exit_code, 0)
        self.assertIn("check", result.stdout)
        self.assertIn("Run the linter on the specified patch", result.stdout)

    def test_help_command_with_invalid_command(self):
        """Test help command with invalid subcommand"""
        result = self.runner.invoke(app, ["help", "invalid"])

        self.assertEqual(result.exit_code, 2)
        self.assertIn("Unknown command: invalid", result.stderr)

    def test_help_with_help_option(self):
        """Test --help option"""
        result = self.runner.invoke(app, ["--help"])

        self.assertEqual(result.exit_code, 0)
        self.assertIn("sru-lint - Static analysis tool for Ubuntu SRU patches", result.stdout)

    def test_help_for_check_command(self):
        """Test help for check command"""
        result = self.runner.invoke(app, ["check", "--help"])

        self.assertEqual(result.exit_code, 0)
        self.assertIn("Run the linter on the specified patch", result.stdout)

    def test_help_for_plugins_command(self):
        """Test help for plugins command"""
        result = self.runner.invoke(app, ["plugins", "--help"])

        self.assertEqual(result.exit_code, 0)
        self.assertIn("List all available plugins", result.stdout)

    def test_help_for_help_command(self):
        """Test help for help command itself"""
        result = self.runner.invoke(app, ["help", "help"])

        self.assertEqual(result.exit_code, 0)
        self.assertIn("Show the same help text as `--help`", result.stdout)

    def test_invalid_command(self):
        """Test invalid command"""
        result = self.runner.invoke(app, ["invalid"])

        self.assertEqual(result.exit_code, 2)
        self.assertIn("No such command", result.stderr)

    @patch("sru_lint.cli.render_snippet")
    @patch("sru_lint.cli.process_patch_content")
    @patch("sru_lint.cli.PluginManager")
    def test_check_multiple_modules_options(
        self, mock_plugin_manager, mock_process_patch, mock_render_snippet
    ):
        """Test check command with multiple --modules options"""
        mock_pm = MagicMock()
        mock_plugin_manager.return_value = mock_pm

        mock_plugin1 = MagicMock()
        mock_plugin1.__symbolic_name__ = "plugin1"
        mock_plugin1.process.return_value = []
        mock_plugin2 = MagicMock()
        mock_plugin2.__symbolic_name__ = "plugin2"
        mock_plugin2.process.return_value = []
        mock_plugin3 = MagicMock()
        mock_plugin3.__symbolic_name__ = "plugin3"
        mock_plugin3.process.return_value = []
        mock_pm.load_plugins.return_value = [mock_plugin1, mock_plugin2, mock_plugin3]

        mock_processed_files = [ProcessedFile(path="test.py", source_span=MagicMock())]
        mock_process_patch.return_value = mock_processed_files

        result = self.runner.invoke(
            app, ["check", "-", "-m", "plugin1", "-m", "plugin2"], input="patch content"
        )

        self.assertEqual(result.exit_code, 0)
        mock_plugin1.process.assert_called_once_with(mock_processed_files)
        mock_plugin2.process.assert_called_once_with(mock_processed_files)
        mock_plugin3.process.assert_not_called()

    @patch("sru_lint.cli.render_snippet")
    @patch("sru_lint.cli.process_patch_content")
    @patch("sru_lint.cli.PluginManager")
    def test_check_mixed_comma_and_multiple_options(
        self, mock_plugin_manager, mock_process_patch, mock_render_snippet
    ):
        """Test check command mixing comma-separated and multiple options"""
        mock_pm = MagicMock()
        mock_plugin_manager.return_value = mock_pm

        mock_plugin1 = MagicMock()
        mock_plugin1.__symbolic_name__ = "plugin1"
        mock_plugin1.process.return_value = []
        mock_plugin2 = MagicMock()
        mock_plugin2.__symbolic_name__ = "plugin2"
        mock_plugin2.process.return_value = []
        mock_plugin3 = MagicMock()
        mock_plugin3.__symbolic_name__ = "plugin3"
        mock_plugin3.process.return_value = []
        mock_pm.load_plugins.return_value = [mock_plugin1, mock_plugin2, mock_plugin3]

        mock_processed_files = [ProcessedFile(path="test.py", source_span=MagicMock())]
        mock_process_patch.return_value = mock_processed_files

        result = self.runner.invoke(
            app, ["check", "-", "-m", "plugin1,plugin2", "-m", "plugin3"], input="patch content"
        )

        self.assertEqual(result.exit_code, 0)
        mock_plugin1.process.assert_called_once_with(mock_processed_files)
        mock_plugin2.process.assert_called_once_with(mock_processed_files)
        mock_plugin3.process.assert_called_once_with(mock_processed_files)

    def test_verbose_flag(self):
        """Test verbose flag functionality"""
        with patch("sru_lint.cli.setup_logger") as mock_setup_logger:
            self.runner.invoke(app, ["-v", "plugins"])
            # Check that logging was configured with INFO level
            mock_setup_logger.assert_called()

    def test_quiet_flag(self):
        """Test quiet flag functionality"""
        with patch("sru_lint.cli.setup_logger") as mock_setup_logger:
            self.runner.invoke(app, ["-q", "plugins"])
            # Check that logging was configured
            mock_setup_logger.assert_called()

    @patch("sru_lint.cli.render_snippet")
    @patch("sru_lint.cli.process_patch_content")
    @patch("sru_lint.cli.PluginManager")
    def test_quiet_suppresses_success_message(
        self, mock_plugin_manager, mock_process_patch, mock_render_snippet
    ):
        """Test that quiet mode suppresses success message"""
        mock_pm = MagicMock()
        mock_plugin_manager.return_value = mock_pm

        mock_plugin1 = MagicMock()
        mock_plugin1.__symbolic_name__ = "plugin1"
        mock_plugin1.process.return_value = []  # No feedback
        mock_pm.load_plugins.return_value = [mock_plugin1]

        mock_processed_files = [ProcessedFile(path="test.py", source_span=MagicMock())]
        mock_process_patch.return_value = mock_processed_files

        result = self.runner.invoke(app, ["-q", "check", "-"], input="patch content")

        self.assertEqual(result.exit_code, 0)
        # In quiet mode, success message should not appear
        self.assertNotIn("No issues found", result.stdout)

    @patch("sru_lint.cli.render_snippet")
    @patch("sru_lint.cli.process_patch_content")
    @patch("sru_lint.cli.PluginManager")
    def test_check_command_with_file_input(
        self, mock_plugin_manager, mock_process_patch, mock_render_snippet
    ):
        """Test check command with file input instead of stdin"""
        mock_pm = MagicMock()
        mock_plugin_manager.return_value = mock_pm

        mock_plugin1 = MagicMock()
        mock_plugin1.__symbolic_name__ = "plugin1"
        mock_plugin1.process.return_value = []
        mock_pm.load_plugins.return_value = [mock_plugin1]

        mock_processed_files = [ProcessedFile(path="test.py", source_span=MagicMock())]
        mock_process_patch.return_value = mock_processed_files

        # Test with a file argument
        with patch("builtins.open", mock_open(read_data="file patch content")):
            result = self.runner.invoke(app, ["check", "test.patch"])

        self.assertEqual(result.exit_code, 0)
        mock_process_patch.assert_called_once_with("file patch content")

    def test_global_options_combinations(self):
        """Test various combinations of global options"""
        # Test multiple verbose flags
        result = self.runner.invoke(app, ["-vv", "plugins"])
        self.assertEqual(result.exit_code, 0)

        # Test verbose and quiet together (quiet should take precedence)
        result = self.runner.invoke(app, ["-v", "-q", "plugins"])
        self.assertEqual(result.exit_code, 0)

    @patch("sru_lint.cli.render_snippet")
    @patch("sru_lint.cli.process_patch_content")
    @patch("sru_lint.cli.PluginManager")
    def test_check_command_feedback_display_colors(
        self, mock_plugin_manager, mock_process_patch, mock_render_snippet
    ):
        """Test that feedback is displayed with appropriate colors"""
        mock_pm = MagicMock()
        mock_plugin_manager.return_value = mock_pm

        # Create feedback items with different severities
        error_feedback = FeedbackItem(
            message="Error message",
            span=self._create_mock_source_span("test.py", 1, 1, 1, 1),
            rule_id="ERR001",
            severity=Severity.ERROR,
        )
        warning_feedback = FeedbackItem(
            message="Warning message",
            span=self._create_mock_source_span("test.py", 2, 1, 2, 1),
            rule_id="WARN001",
            severity=Severity.WARNING,
        )
        info_feedback = FeedbackItem(
            message="Info message",
            span=self._create_mock_source_span("test.py", 3, 1, 3, 1),
            rule_id="INFO001",
            severity=Severity.INFO,
        )

        mock_plugin1 = MagicMock()
        mock_plugin1.__symbolic_name__ = "plugin1"
        mock_plugin1.process.return_value = [error_feedback, warning_feedback, info_feedback]
        mock_plugin1.feedback = [error_feedback, warning_feedback, info_feedback]
        mock_pm.load_plugins.return_value = [mock_plugin1]

        mock_processed_files = [ProcessedFile(path="test.py", source_span=MagicMock())]
        mock_process_patch.return_value = mock_processed_files

        result = self.runner.invoke(app, ["check", "-"], input="patch content")

        self.assertEqual(result.exit_code, 1)  # Should exit with error due to error feedback
        self.assertIn("Error message", result.stdout)
        self.assertIn("Warning message", result.stdout)
        self.assertIn("Info message", result.stdout)
        self.assertIn("(Severity: error)", result.stdout)
        self.assertIn("(Severity: warning)", result.stdout)
        self.assertIn("(Severity: info)", result.stdout)


if __name__ == "__main__":
    unittest.main()
