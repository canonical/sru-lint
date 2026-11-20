from sru_lint.common.debian.dep3 import check_dep3_compliance
from sru_lint.common.debian.upstream_fetcher import fetch_patch_from_upstream
from sru_lint.common.debian.upstream_patch import UpstreamReference, compare_patch_content
from sru_lint.common.errors import ErrorCode
from sru_lint.common.feedback import FeedbackItem, Severity, SourceSpan
from sru_lint.common.logging import get_logger
from sru_lint.plugins.plugin_base import Plugin


class PatchFormat(Plugin):
    """Checks the compliance of the patches in debian/patches to the DEP-3."""

    def __init__(self):
        super().__init__()
        self.logger = get_logger("plugins.patch-format")

    def register_file_patterns(self):
        """Register that we want to check debian/patches files."""
        self.add_file_pattern("debian/patches/*")

    def process_file(self, processed_file):
        """Process a single patch file for DEP-3 compliance."""
        self.logger.info(f"Processing patch file: {processed_file.path}")

        # Skip the series file
        if processed_file.path.endswith("/series"):
            self.logger.debug(f"Skipping series file: {processed_file.path}")
            return

        # Get the added content from the patch file
        added_lines = processed_file.source_span.content
        if not added_lines:
            self.logger.debug(f"No added lines in {processed_file.path}")
            return

        # Combine the added lines into patch content
        patch_content = "\n".join([line.content for line in added_lines])

        self.logger.info(f"Checking DEP-3 compliance for patch: {processed_file.path}")
        self.check_dep3_format(processed_file, patch_content)

        # Check upstream comparison if DEP-3 Origin tag is present
        self.check_upstream_patch(processed_file, patch_content)

    def check_dep3_format(self, processed_file, patch_content):
        """Check DEP-3 compliance for a patch file."""
        self.logger.debug(f"Checking DEP-3 format of patch: {processed_file.path}")

        try:
            # Check DEP-3 compliance - returns (is_compliant: bool, list[FeedbackItem])
            is_compliant, feedback_items = check_dep3_compliance(patch_content, processed_file.path)

            self.logger.debug(
                f"DEP-3 compliance result for {processed_file.path}: compliant={is_compliant}, issues={len(feedback_items)}"
            )

            # Add all feedback items to our feedback list
            self.feedback.extend(feedback_items)

            self.logger.debug(f"DEP-3 compliance check completed for {processed_file.path}")

        except Exception as e:
            self.logger.error(f"Error checking DEP-3 compliance for {processed_file.path}: {e}")
            # Create feedback for parsing errors using the existing source span
            source_span = SourceSpan(
                path=processed_file.path,
                start_line=1,
                start_col=1,
                end_line=1,
                end_col=1,
                content=processed_file.source_span.content,
                content_with_context=processed_file.source_span.content_with_context,
            )

            feedback = FeedbackItem(
                message=f"Failed to parse patch for DEP-3 compliance: {str(e)}",
                rule_id=ErrorCode.PATCH_DEP3_FORMAT,
                severity=Severity.WARNING,
                span=source_span,
            )

            self.feedback.append(feedback)

    def check_upstream_patch(self, processed_file, patch_content):
        """
        Compare patch against upstream version if Origin tag is present.

        Issues warnings if:
        - Patch is marked as a backport
        - Patch content differs from upstream (beyond line numbers/context)
        """
        self.logger.debug(f"Checking upstream patch for: {processed_file.path}")

        try:
            # Extract Origin tag from patch content
            origin_match = self._extract_dep3_field(patch_content, "Origin")
            if not origin_match:
                self.logger.debug(f"No Origin tag found in {processed_file.path}")
                return

            upstream_ref = UpstreamReference.from_origin_tag(origin_match)
            self.logger.debug(f"Upstream reference: {upstream_ref}")

            # Issue warning for backports
            if upstream_ref.is_backport:
                source_span = SourceSpan(
                    path=processed_file.path,
                    start_line=1,
                    start_col=1,
                    end_line=1,
                    end_col=1,
                    content=processed_file.source_span.content,
                    content_with_context=processed_file.source_span.content_with_context,
                )

                feedback = FeedbackItem(
                    message="Patch is marked as backport in Origin tag",
                    rule_id=ErrorCode.PATCH_UPSTREAM_BACKPORT,
                    severity=Severity.WARNING,
                    span=source_span,
                    doc_url="https://dep-team.pages.debian.net/deps/dep3/",
                )

                self.feedback.append(feedback)

            # Try to fetch upstream patch and compare
            if upstream_ref.url:
                self._compare_with_upstream_patch(processed_file, patch_content, upstream_ref)
            elif upstream_ref.commit:
                self.logger.debug(
                    f"Upstream commit {upstream_ref.commit} found but no URL; skipping fetch"
                )

        except Exception as e:
            self.logger.debug(f"Error checking upstream patch for {processed_file.path}: {e}")

    def _extract_dep3_field(self, patch_content: str, field_name: str) -> str | None:
        """
        Extract a DEP-3 field value from patch content.

        Args:
            patch_content: The full patch content
            field_name: The field name to extract (e.g., "Origin")

        Returns:
            The field value if found, None otherwise
        """
        import re

        lines = patch_content.split("\n")
        field_pattern = rf"^{field_name}\s*:\s*(.+)$"

        for line in lines:
            # Stop at the separator
            if line.startswith("---"):
                break

            # Strip comment prefix if present
            stripped = line.lstrip()
            if stripped.startswith("#"):
                stripped = stripped[1:].lstrip()

            match = re.match(field_pattern, stripped)
            if match:
                return match.group(1).strip()

        return None

    def _compare_with_upstream_patch(
        self, processed_file, patch_content: str, upstream_ref: UpstreamReference
    ):
        """
        Fetch upstream patch and compare with local patch.

        Args:
            processed_file: The processed file object
            patch_content: Local patch content
            upstream_ref: Upstream reference information
        """
        if not upstream_ref.url:
            return

        self.logger.debug(f"Attempting to fetch upstream patch from: {upstream_ref.url}")

        try:
            upstream_patch = fetch_patch_from_upstream(upstream_ref.url, timeout=10)

            if not upstream_patch:
                self.logger.debug(f"Could not fetch upstream patch from {upstream_ref.url}")
                return

            # Compare patches
            matches, differences = compare_patch_content(patch_content, upstream_patch)

            if not matches and differences:
                self.logger.debug(f"Patch differs from upstream: {differences}")

                source_span = SourceSpan(
                    path=processed_file.path,
                    start_line=1,
                    start_col=1,
                    end_line=1,
                    end_col=1,
                    content=processed_file.source_span.content,
                    content_with_context=processed_file.source_span.content_with_context,
                )

                # Create feedback for each difference
                for diff_msg in differences:
                    feedback = FeedbackItem(
                        message=f"Patch differs from upstream: {diff_msg}",
                        rule_id=ErrorCode.PATCH_UPSTREAM_DIVERGED,
                        severity=Severity.WARNING,
                        span=source_span,
                        doc_url="https://dep-team.pages.debian.net/deps/dep3/",
                    )

                    self.feedback.append(feedback)

        except Exception as e:
            self.logger.debug(f"Error comparing with upstream patch: {e}")
