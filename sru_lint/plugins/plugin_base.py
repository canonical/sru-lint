import fnmatch
import re
from abc import ABC, abstractmethod

from debian.debian_support import Version

from sru_lint.common.errors import ErrorCode
from sru_lint.common.feedback import FeedbackItem, Severity, SourceSpan
from sru_lint.common.logging import get_logger
from sru_lint.common.madison import get_madison
from sru_lint.common.patch_processor import ProcessedFile


class Plugin(ABC):
    """Base class for plugins that process patches (parsed by unidiff)."""

    __symbolic_name__: str | None = None

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        # Only fill in if not explicitly provided or empty
        if not getattr(cls, "__symbolic_name__", None):
            cls.__symbolic_name__ = cls._generate_symbolic_name(cls.__name__)

    def __init__(self):
        """Initialize the plugin with its file patterns and Launchpad helper."""
        from sru_lint.common.launchpad_helper import get_launchpad_helper

        self._file_patterns: set[str] = set()
        self.feedback: list[FeedbackItem] = []  # List to collect feedback items
        self.lp_helper = get_launchpad_helper()
        self.use_madison = False  # set by the CLI's --madison flag
        self.logger = get_logger(f"plugins.{self.__symbolic_name__}")
        self.register_file_patterns()

    def __enter__(self):
        self.logger.debug(f"Entering plugin context: {self.__symbolic_name__}")
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.logger.debug(f"Exiting plugin context: {self.__symbolic_name__}")
        self.post_process()

    def post_process(self):  # noqa: B027
        """Hook for any post-processing after all files have been processed."""
        pass  # Default: subclasses can override

    @staticmethod
    def _generate_symbolic_name(name: str) -> str:
        # strip leading underscores, split Camel/PascalCase (keeps acronyms), include digits
        name = name.lstrip("_")
        parts = re.findall(r"[A-Z]+(?=[A-Z][a-z]|$)|[A-Z]?[a-z]+|\d+", name)
        return "-".join(p.lower() for p in parts)

    def register_file_patterns(self):  # noqa: B027
        """
        Register file patterns that this plugin wants to check.

        Subclasses should override this method to register their file patterns
        using add_file_pattern() or add_file_patterns().

        Example:
            def register_file_patterns(self):
                self.add_file_pattern("debian/changelog")
                self.add_file_patterns(["*.py", "*.pyx"])
        """
        pass  # Default: subclasses should override

    def add_file_pattern(self, pattern: str):
        """
        Add a single file pattern to check.

        Args:
            pattern: A file pattern (supports wildcards like *, ?, [seq])
                    Examples: "debian/changelog", "*.py", "src/**/*.c"
        """
        self._file_patterns.add(pattern)
        self.logger.debug(f"Added file pattern: {pattern}")

    def add_file_patterns(self, patterns: list[str]):
        """
        Add multiple file patterns to check.

        Args:
            patterns: List of file patterns
        """
        self._file_patterns.update(patterns)
        self.logger.debug(f"Added file patterns: {patterns}")

    def matches_file(self, filepath: str) -> bool:
        """
        Check if a file path matches any of the registered patterns.

        Args:
            filepath: The file path to check

        Returns:
            True if the file matches any registered pattern, False otherwise
        """
        for pattern in self._file_patterns:
            if fnmatch.fnmatch(filepath, pattern) or fnmatch.fnmatch(filepath, f"*/{pattern}"):
                self.logger.debug(f"File {filepath} matches pattern {pattern}")
                return True
        self.logger.debug(f"File {filepath} does not match any patterns: {self._file_patterns}")
        return False

    def process(self, processed_files: list[ProcessedFile]) -> list[FeedbackItem]:
        """
        Process the given processed files and perform plugin-specific actions.

        This method iterates through all processed files and calls process_file()
        for each file that matches the registered patterns.

        Args:
            processed_files: List of ProcessedFile objects

        Returns:
            List of FeedbackItem objects from all processed files
        """
        # Clear previous feedback
        self.feedback.clear()
        self.logger.info(f"Starting processing with plugin {self.__symbolic_name__}")

        # Filter files that match this plugin's patterns
        matching_files = [pf for pf in processed_files if self.matches_file(pf.path)]

        self.logger.info(
            f"Processing {len(matching_files)} matching files out of {len(processed_files)} total"
        )

        # Process each matching file
        for processed_file in matching_files:
            self.logger.info(f"Processing file: {processed_file.path}")
            self.process_file(processed_file)

        self.logger.info(
            f"Processed {len(matching_files)} files, found {len(self.feedback)} issues"
        )
        return self.feedback

    def check_madison_version(
        self, source_span: SourceSpan, package: str, version: str, release: str
    ) -> None:
        """Warn if ``version`` collides with, or is lower than, a version of ``package``
        published for ``release`` in the Pro/ESM, kernel, Landscape or MAAS repositories.

        Does nothing unless ``use_madison`` is set.
        """
        if not self.use_madison:
            return

        release = release.split("-", 1)[0]
        try:
            result = get_madison().source_versions(package)
            new_version = Version(version)
            collisions: set[str] = set()
            higher: dict[str, set[str]] = {}
            for entry in result.entries:
                if entry.release != release:
                    continue
                location = f"{entry.category}/{entry.suite}"
                published = Version(entry.source_version)
                if published == new_version:
                    collisions.add(location)
                elif published > new_version:
                    higher.setdefault(entry.source_version, set()).add(location)
        except Exception as e:
            self.logger.warning(f"Madison lookup failed for {package}: {type(e).__name__}: {e}")
            return

        if collisions:
            self.create_line_feedback(
                message=(
                    f"Version {version} of {package} is already published in: "
                    f"{', '.join(sorted(collisions))}"
                ),
                rule_id=ErrorCode.MADISON_VERSION_COLLISION,
                severity=Severity.WARNING,
                source_span=source_span,
                target_line_content=version,
            )
        if higher:
            details = "; ".join(
                f"{v} in {', '.join(sorted(locations))}"
                for v, locations in sorted(higher.items(), key=lambda item: Version(item[0]))
            )
            self.create_line_feedback(
                message=(
                    f"Higher version(s) of {package} already published for {release}: {details}"
                ),
                rule_id=ErrorCode.MADISON_HIGHER_VERSION_EXISTS,
                severity=Severity.WARNING,
                source_span=source_span,
                target_line_content=version,
            )

    @abstractmethod
    def process_file(self, processed_file: ProcessedFile) -> None:
        """
        Process a single file that matches the plugin's registered patterns.

        This is the callback method that subclasses must implement to perform
        their specific checks on the file. Implementations should add any
        discovered issues to self.feedback using add_feedback() or the
        create_feedback() helper methods.

        Args:
            processed_file: A ProcessedFile object containing the file path,
                           source span with content, and original patch reference
        """
        raise NotImplementedError("Subclasses must implement process_file()")

    def add_feedback(self, feedback_item: FeedbackItem) -> None:
        """
        Add a feedback item to the collection.

        Args:
            feedback_item: The FeedbackItem to add
        """
        self.feedback.append(feedback_item)
        self.logger.debug(
            f"Added feedback: {feedback_item.severity.value} - {feedback_item.message}"
        )

    def create_feedback(
        self,
        message: str,
        rule_id: ErrorCode,
        severity: Severity = Severity.ERROR,
        source_span: SourceSpan | None = None,
        line_number: int | None = None,
        col_start: int = 1,
        col_end: int | None = None,
        doc_url: str | None = None,
    ) -> FeedbackItem:
        """
        Create a FeedbackItem with proper span information.

        Args:
            message: The feedback message
            rule_id: The rule identifier
            severity: The severity level
            source_span: The source span to use (optional)
            line_number: Specific line number (optional, overrides source_span)
            col_start: Column start position
            col_end: Column end position (optional)

        Returns:
            The created FeedbackItem (also automatically added to self.feedback)
        """
        if source_span:
            # Use provided source span but allow overrides
            feedback_span = SourceSpan(
                path=source_span.path,
                start_line=line_number or source_span.start_line,
                start_col=col_start,
                end_line=line_number or source_span.end_line,
                end_col=col_end or col_start,
                start_offset=0,
                end_offset=0,
                content=source_span.content,
                content_with_context=source_span.content_with_context,
            )
        else:
            # Create minimal span
            feedback_span = SourceSpan(
                path="unknown",
                start_line=line_number or 1,
                start_col=col_start,
                end_line=line_number or 1,
                end_col=col_end or col_start,
                start_offset=0,
                end_offset=0,
            )

        feedback_item = FeedbackItem(
            message=message, span=feedback_span, rule_id=rule_id, severity=severity, doc_url=doc_url
        )

        self.add_feedback(feedback_item)
        return feedback_item

    def create_line_feedback(
        self,
        message: str,
        rule_id: ErrorCode,
        source_span: SourceSpan,
        target_line_content: str,
        severity: Severity = Severity.ERROR,
        doc_url: str | None = None,
    ) -> FeedbackItem:
        """
        Create feedback for a specific line content found in the source span.

        Args:
            message: The feedback message
            rule_id: The rule identifier
            source_span: The source span to search in
            target_line_content: The line content to search for
            severity: The severity level
            doc_url: Optional documentation URL for the feedback that may be helpful to fix the issue

        Returns:
            The created FeedbackItem (also automatically added to self.feedback)
        """
        line_number = source_span.start_line
        col_start = 1
        col_end = len(target_line_content)

        self.logger.debug(
            f"Searching for line content: '{target_line_content}' in {source_span.path}"
        )

        # Search for the line in the source span content
        found = False
        for line in source_span.content_with_context:
            if target_line_content in line.content:
                line_number = line.line_number or line_number
                # Find column position of the content
                col_start = line.content.find(target_line_content) + 1
                col_end = col_start + len(target_line_content)
                found = True
                self.logger.debug(
                    f"Found target content at line {line_number}, col {col_start}-{col_end}"
                )
                break

        if not found:
            self.logger.warning(
                f"Target line content '{target_line_content}' not found in source span, using defaults"
            )

        return self.create_feedback(
            message=message,
            rule_id=rule_id,
            severity=severity,
            source_span=source_span,
            line_number=line_number,
            col_start=col_start,
            col_end=col_end,
            doc_url=doc_url,
        )
