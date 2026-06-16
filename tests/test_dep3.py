import unittest

from sru_lint.common.debian.dep3 import (
    DEP3_FIELD_DEFINITIONS,
    Dep3HeaderParser,
    check_dep3_compliance,
)
from sru_lint.common.errors import ErrorCode
from sru_lint.common.feedback import Severity


class TestDep3HeaderParser(unittest.TestCase):
    def setUp(self):
        self.parser = Dep3HeaderParser(DEP3_FIELD_DEFINITIONS)

    def _parse(self, text):
        return self.parser.parse(text)

    # --- single-line fields ---

    def test_parse_single_line_description(self):
        fields = self._parse("Description: Fix widget\nAuthor: John\n---\n")
        self.assertEqual(fields["description"][0], "Fix widget")
        self.assertEqual(fields["author"][0], "John")

    def test_parse_subject_as_alias(self):
        fields = self._parse("Subject: Fix widget\nAuthor: John\n---\n")
        self.assertIn("subject", fields)
        self.assertEqual(fields["subject"][0], "Fix widget")

    # --- multi-line Description / Subject ---

    def test_description_continuation_lines_collected(self):
        patch = (
            "Description: Fix widget frobnication\n"
            " This is the long description.\n"
            " It spans multiple lines.\n"
            "Author: John Doe <j@e.com>\n"
            "---\n"
        )
        fields = self._parse(patch)
        value = fields["description"][0]
        self.assertTrue(value.startswith("Fix widget frobnication"))
        self.assertIn("long description", value)
        self.assertIn("multiple lines", value)

    def test_subject_continuation_lines_collected(self):
        patch = (
            "Subject: Fix widget frobnication\n"
            " Long description line 1\n"
            " Long description line 2\n"
            "Author: John Doe <j@e.com>\n"
            "---\n"
        )
        fields = self._parse(patch)
        value = fields["subject"][0]
        self.assertTrue(value.startswith("Fix widget frobnication"))
        self.assertIn("Long description line 1", value)
        self.assertIn("Long description line 2", value)

    def test_description_not_interrupted_by_author_header(self):
        """Author: appearing after Description's continuation lines does not truncate Description."""
        patch = (
            "Description: Fix widget\n"
            " Long description part 1\n"
            " Long description part 2\n"
            "Author: John Doe <j@e.com>\n"
            "Last-Update: 2024-01-15\n"
            "---\n"
        )
        fields = self._parse(patch)
        # Description must include ALL continuation lines
        value = fields["description"][0]
        self.assertIn("Long description part 1", value)
        self.assertIn("Long description part 2", value)
        # Author and Last-Update are parsed separately
        self.assertIn("author", fields)
        self.assertIn("last-update", fields)

    def test_description_not_interrupted_by_blank_line_then_other_headers(self):
        """Blank line separates stanzas; Description before the blank is still complete."""
        patch = "Description: Fix widget\n Long description.\n\nAuthor: John Doe <j@e.com>\n---\n"
        fields = self._parse(patch)
        value = fields["description"][0]
        self.assertIn("Long description", value)
        self.assertIn("author", fields)

    def test_free_form_text_appended_to_current_field(self):
        """A line without leading whitespace that is not a field header is appended
        to the current field value rather than silently discarded."""
        patch = (
            "Description: Fix widget frobnication\n"
            "This line has no leading space but is not a field header\n"
            "Author: John Doe <j@e.com>\n"
            "---\n"
        )
        fields = self._parse(patch)
        value = fields["description"][0]
        self.assertIn("no leading space", value)
        self.assertIn("author", fields)

    # --- line number tracking ---

    def test_line_numbers_reported_correctly(self):
        patch = (
            "Description: Fix widget\n"
            " Long description\n"
            "Author: John Doe\n"
            "Last-Update: 2024-01-15\n"
            "---\n"
        )
        fields = self._parse(patch)
        self.assertEqual(fields["description"][1], 1)
        self.assertEqual(fields["author"][1], 3)
        self.assertEqual(fields["last-update"][1], 4)


class TestCheckDep3Compliance(unittest.TestCase):
    def _check(self, patch_text):
        return check_dep3_compliance(patch_text, "debian/patches/test.patch")

    # --- fully compliant patches ---

    def test_compliant_single_line_description(self):
        patch = (
            "Description: Fix widget frobnication speeds\n"
            "Author: John Doe <j@e.com>\n"
            "Last-Update: 2024-01-15\n"
            "---\n"
        )
        compliant, issues = self._check(patch)
        self.assertTrue(compliant)
        self.assertEqual(issues, [])

    def test_compliant_multi_line_description_then_author(self):
        """Multi-line Description followed immediately by Author is valid."""
        patch = (
            "Description: Fix widget frobnication speeds\n"
            " Detailed description spanning\n"
            " multiple continuation lines.\n"
            "Author: John Doe <j@e.com>\n"
            "Last-Update: 2024-01-15\n"
            "---\n"
        )
        compliant, issues = self._check(patch)
        self.assertTrue(compliant)
        self.assertEqual(issues, [])

    def test_compliant_subject_alias(self):
        patch = (
            "Subject: Fix widget frobnication speeds\n"
            " Long description.\n"
            "Author: John Doe <j@e.com>\n"
            "---\n"
        )
        compliant, issues = self._check(patch)
        self.assertTrue(compliant)
        self.assertEqual(issues, [])

    def test_compliant_with_origin_instead_of_author(self):
        patch = (
            "Description: Fix widget frobnication speeds\nOrigin: upstream, commit abc123\n---\n"
        )
        compliant, issues = self._check(patch)
        self.assertTrue(compliant)
        self.assertEqual(issues, [])

    def test_compliant_from_alias_for_author(self):
        patch = "Description: Fix widget frobnication speeds\nFrom: John Doe <j@e.com>\n---\n"
        compliant, issues = self._check(patch)
        self.assertTrue(compliant)
        self.assertEqual(issues, [])

    # --- missing required fields ---

    def test_missing_description_and_subject(self):
        patch = "Author: John Doe <j@e.com>\n---\n"
        compliant, issues = self._check(patch)
        self.assertFalse(compliant)
        rule_ids = [f.rule_id for f in issues]
        self.assertIn(ErrorCode.PATCH_DEP3_MISSING_DESCRIPTION, rule_ids)

    def test_missing_author_and_origin(self):
        patch = "Description: Fix widget\n---\n"
        compliant, issues = self._check(patch)
        self.assertFalse(compliant)
        rule_ids = [f.rule_id for f in issues]
        self.assertIn(ErrorCode.PATCH_DEP3_MISSING_ORIGIN_AUTHOR, rule_ids)

    # --- empty short description (first-line check) ---

    def test_empty_description_first_line_with_continuation_is_invalid(self):
        """Description: with no short description on its first line must be flagged,
        even if continuation lines contain text."""
        patch = (
            "Description:\n"
            " Only a long description, no short one\n"
            "Author: John Doe <j@e.com>\n"
            "---\n"
        )
        compliant, issues = self._check(patch)
        self.assertFalse(compliant)
        rule_ids = [f.rule_id for f in issues]
        self.assertIn(ErrorCode.PATCH_DEP3_EMPTY_DESCRIPTION, rule_ids)

    def test_empty_subject_first_line_with_continuation_is_invalid(self):
        """Subject: with no short description on its first line must be flagged,
        even if continuation lines contain text."""
        patch = (
            "Subject:\n Only a long description, no short one\nAuthor: John Doe <j@e.com>\n---\n"
        )
        compliant, issues = self._check(patch)
        self.assertFalse(compliant)
        rule_ids = [f.rule_id for f in issues]
        self.assertIn(ErrorCode.PATCH_DEP3_EMPTY_DESCRIPTION, rule_ids)

    def test_empty_description_entirely_is_invalid(self):
        patch = "Description:\nAuthor: John Doe <j@e.com>\n---\n"
        compliant, issues = self._check(patch)
        self.assertFalse(compliant)
        rule_ids = [f.rule_id for f in issues]
        self.assertIn(ErrorCode.PATCH_DEP3_EMPTY_DESCRIPTION, rule_ids)

    def test_non_empty_first_line_with_long_description_is_valid(self):
        """Short description on first line is enough; continuation lines are optional."""
        patch = (
            "Description: Fix widget\n"
            " Long description line 1\n"
            " Long description line 2\n"
            "Author: John Doe <j@e.com>\n"
            "---\n"
        )
        compliant, issues = self._check(patch)
        self.assertTrue(compliant)
        self.assertEqual(issues, [])

    def test_multiline_author_is_invalid(self):
        patch = "Description: Fix widget\nAuthor: John Doe\n More author details\n---\n"
        compliant, issues = self._check(patch)
        self.assertFalse(compliant)
        rule_ids = [f.rule_id for f in issues]
        self.assertIn(ErrorCode.PATCH_DEP3_FORMAT, rule_ids)

    def test_multiline_origin_is_invalid(self):
        patch = "Description: Fix widget\nOrigin: upstream\n Additional origin details\n---\n"
        compliant, issues = self._check(patch)
        self.assertFalse(compliant)
        rule_ids = [f.rule_id for f in issues]
        self.assertIn(ErrorCode.PATCH_DEP3_FORMAT, rule_ids)

    def test_multiline_last_update_is_invalid(self):
        patch = (
            "Description: Fix widget\n"
            "Author: John Doe <j@e.com>\n"
            "Last-Update: 2024-01-15\n"
            " Extra date details\n"
            "---\n"
        )
        compliant, issues = self._check(patch)
        self.assertFalse(compliant)
        rule_ids = [f.rule_id for f in issues]
        self.assertIn(ErrorCode.PATCH_DEP3_FORMAT, rule_ids)

    def test_multiline_forwarded_is_invalid(self):
        patch = (
            "Description: Fix widget\n"
            "Author: John Doe <j@e.com>\n"
            "Forwarded: https://bugs.example.com/123\n"
            " More forwarding details\n"
            "---\n"
        )
        compliant, issues = self._check(patch)
        self.assertFalse(compliant)
        rule_ids = [f.rule_id for f in issues]
        self.assertIn(ErrorCode.PATCH_DEP3_FORMAT, rule_ids)

    def test_multiline_description_is_valid(self):
        patch = (
            "Description: Fix widget\n"
            " More details line 1\n"
            " More details line 2\n"
            "Author: John Doe <j@e.com>\n"
            "---\n"
        )
        compliant, issues = self._check(patch)
        self.assertTrue(compliant)
        self.assertEqual(issues, [])

    def test_multiline_subject_is_valid(self):
        patch = (
            "Subject: Fix widget\n"
            " More details line 1\n"
            " More details line 2\n"
            "Author: John Doe <j@e.com>\n"
            "---\n"
        )
        compliant, issues = self._check(patch)
        self.assertTrue(compliant)
        self.assertEqual(issues, [])

    # --- optional field validators ---

    def test_invalid_last_update_date(self):
        patch = (
            "Description: Fix widget\nAuthor: John Doe <j@e.com>\nLast-Update: not-a-date\n---\n"
        )
        compliant, issues = self._check(patch)
        self.assertFalse(compliant)
        rule_ids = [f.rule_id for f in issues]
        self.assertIn(ErrorCode.PATCH_DEP3_INVALID_DATE, rule_ids)

    def test_valid_last_update_date(self):
        patch = (
            "Description: Fix widget\nAuthor: John Doe <j@e.com>\nLast-Update: 2024-01-15\n---\n"
        )
        compliant, issues = self._check(patch)
        self.assertTrue(compliant)
        self.assertEqual(issues, [])

    def test_invalid_forwarded_value(self):
        patch = (
            "Description: Fix widget\n"
            "Author: John Doe <j@e.com>\n"
            "Forwarded: not-a-url-or-keyword\n"
            "---\n"
        )
        compliant, issues = self._check(patch)
        self.assertFalse(compliant)
        rule_ids = [f.rule_id for f in issues]
        self.assertIn(ErrorCode.PATCH_DEP3_INVALID_FORWARDED, rule_ids)
        severities = [f.severity for f in issues]
        self.assertIn(Severity.WARNING, severities)

    def test_forwarded_no_is_valid(self):
        patch = "Description: Fix widget\nAuthor: John Doe <j@e.com>\nForwarded: no\n---\n"
        compliant, issues = self._check(patch)
        self.assertTrue(compliant)

    def test_forwarded_not_needed_is_valid(self):
        patch = "Description: Fix widget\nAuthor: John Doe <j@e.com>\nForwarded: not-needed\n---\n"
        compliant, issues = self._check(patch)
        self.assertTrue(compliant)

    def test_forwarded_url_is_valid(self):
        patch = (
            "Description: Fix widget\n"
            "Author: John Doe <j@e.com>\n"
            "Forwarded: https://bugs.example.com/123\n"
            "---\n"
        )
        compliant, issues = self._check(patch)
        self.assertTrue(compliant)

    # --- severity ---

    def test_missing_description_is_error_severity(self):
        patch = "Author: John Doe <j@e.com>\n---\n"
        _, issues = self._check(patch)
        missing = [f for f in issues if f.rule_id == ErrorCode.PATCH_DEP3_MISSING_DESCRIPTION]
        self.assertTrue(missing)
        self.assertEqual(missing[0].severity, Severity.ERROR)

    def test_invalid_date_is_warning_severity(self):
        patch = "Description: Fix widget\nAuthor: John Doe <j@e.com>\nLast-Update: bad\n---\n"
        _, issues = self._check(patch)
        date_issues = [f for f in issues if f.rule_id == ErrorCode.PATCH_DEP3_INVALID_DATE]
        self.assertTrue(date_issues)
        self.assertEqual(date_issues[0].severity, Severity.WARNING)


if __name__ == "__main__":
    unittest.main()
