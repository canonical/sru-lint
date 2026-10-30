from debian import changelog

from sru_lint.common.doc_links import DocLinks
from sru_lint.common.errors import ErrorCode
from sru_lint.common.feedback import Severity
from sru_lint.common.parse import base_release
from sru_lint.madison.api import DEFAULT_ARCHITECTURES, search_repositories
from sru_lint.madison.cache import IndexCache
from sru_lint.plugins.plugin_base import Plugin, ProcessedFile
from sru_lint.plugins.uca import UCA_VERSION_SUFFIX_RE


class MadisonCollision(Plugin):
    """Checks that the version in debian/changelog does not already exist in
    non-public repositories (ESM, Ubuntu Pro, and Canonical PPAs) as reported
    by madison."""

    def __init__(self):
        super().__init__()
        self.cache: IndexCache | None = None

    def register_file_patterns(self):
        self.add_file_pattern("debian/changelog")

    def process_file(self, processed_file: ProcessedFile) -> None:
        source_span = processed_file.source_span
        added = "\n".join(line.content for line in source_span.lines_added)
        if not added.strip():
            return

        try:
            cl = changelog.Changelog(added)
        except Exception as e:
            self.logger.error(f"Failed to parse changelog: {e}")
            return

        package = cl.get_package()
        if not package or cl.version is None or not cl.distributions:
            self.logger.debug("Skipping madison check: no changelog entry could be parsed")
            return
        version = str(cl.version)

        if UCA_VERSION_SUFFIX_RE.search(version):
            self.logger.debug(f"Skipping madison check for UCA upload: {package} {version}")
            return

        release = base_release(str(cl.distributions))
        if release is None:
            self.logger.debug(
                f"Skipping madison check: no releasable distribution ({cl.distributions!r})"
            )
            return

        self.logger.info(f"Checking madison for {package} {version} (release={release})")
        try:
            if self.cache is None:
                self.cache = IndexCache()
            outcome = search_repositories(
                [package],
                architectures=DEFAULT_ARCHITECTURES,
                release=release,
                cache=self.cache,
            )
        except Exception as e:
            self.logger.error(f"Madison check failed for {package} {version}: {e}")
            self.create_line_feedback(
                message=f"Failed to check madison for {package} {version}: {e}",
                rule_id=ErrorCode.MADISON_CHECK_ERROR,
                severity=Severity.WARNING,
                source_span=source_span,
                target_line_content=version,
            )
            return

        collisions = sorted(
            {
                f"{r.category}/{r.suite}"
                for r in outcome.results
                if r.package == package and r.version == version
            }
        )
        if collisions:
            self.create_line_feedback(
                message=(
                    f"Version '{version}' of '{package}' already exists in non-public "
                    f"repositories: {', '.join(collisions)}"
                ),
                rule_id=ErrorCode.MADISON_VERSION_COLLISION,
                severity=Severity.ERROR,
                source_span=source_span,
                target_line_content=version,
                doc_url=DocLinks.VERSION_STRING_FORMAT,
            )
        else:
            self.logger.info(f"{package} {version} not found in non-public repositories")

        if outcome.failures:
            self.create_line_feedback(
                message=(
                    f"Madison search for {package} {version} was incomplete: "
                    f"{outcome.failures} index(es) failed, so an existing version may "
                    f"have been missed"
                ),
                rule_id=ErrorCode.MADISON_CHECK_ERROR,
                severity=Severity.WARNING,
                source_span=source_span,
                target_line_content=version,
            )
