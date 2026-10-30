from debian import changelog
from debian.debian_support import Version

from sru_lint.common.distro_helper import is_esm_only_release
from sru_lint.common.errors import ErrorCode
from sru_lint.common.feedback import Severity, SourceSpan
from sru_lint.common.launchpad_helper import LaunchpadHelper
from sru_lint.common.parse import UNRELEASED_DISTRIBUTION, parse_distributions_field
from sru_lint.madison.search import Result
from sru_lint.plugins.plugin_base import Plugin, ProcessedFile

_POCKETS = ("-proposed", "-updates", "-security", "-backports")


class ProVersionNumbers(Plugin):
    """For ESM/Pro changelog entries, ensure the version is higher than
    every version already published in the Ubuntu Pro PPAs."""

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

        release = self._base_release(cl.distributions)
        esm_only = release is not None and is_esm_only_release(release)
        if release is None or (self.madison is None and not esm_only):
            self.logger.debug(
                f"Skipping: not an ESM-only release (distributions={cl.distributions!r})"
            )
            return

        package = cl.get_package()
        if not package or cl.version is None:
            self.logger.warning("Changelog is missing a source package or version")
            return
        changelog_version = str(cl.version)
        self.logger.info(
            f"Checking published versions for {package} {changelog_version} (release={release})"
        )

        highest_version: str | None = None
        highest_ppa: str | None = None
        if self.madison is not None:
            highest = self._highest_madison_version(
                source_span, package, changelog_version, release
            )
            if highest is not None:
                highest_version = highest.version
                highest_ppa = f"{highest.category}/{highest.suite} (binary {highest.package})"

        # Preserve the default check, including staging and source-only uploads.
        for ppa in LaunchpadHelper.PRO_PPAS if esm_only else ():
            try:
                ver = self.lp_helper.get_highest_version_in_ppa(ppa, package)
            except Exception as e:
                self.logger.warning(f"Skipping {ppa}: {type(e).__name__}: {e}")
                continue
            if ver is None:
                continue
            if highest_version is None or Version(ver) > Version(highest_version):
                highest_version = ver
                highest_ppa = ppa

        if highest_version is None:
            self.logger.info(
                f"{package} has no current publications in the checked repositories; nothing to compare"
            )
            return

        if Version(changelog_version) > Version(highest_version):
            self.logger.info(
                f"{package} {changelog_version} > {highest_version} (highest in {highest_ppa}); OK"
            )
            return

        self.create_line_feedback(
            message=(
                f"Version {changelog_version} for {package} is not higher than "
                f"{highest_version} already published in {highest_ppa}"
            ),
            rule_id=ErrorCode.PRO_VERSION_NOT_HIGHER,
            severity=Severity.ERROR,
            source_span=source_span,
            target_line_content=changelog_version,
        )

    def _highest_madison_version(
        self, source_span: SourceSpan, package: str, changelog_version: str, release: str
    ) -> Result | None:
        assert self.madison is not None
        lookup = self.madison.lookup_source(package, release)
        if lookup.failures:
            self.logger.warning("Madison lookup failures: %s", "; ".join(lookup.failures))
            self.create_line_feedback(
                message=(
                    f"Madison search for {package} is incomplete: "
                    f"{len(lookup.failures)} index(es) failed; version check may miss publications"
                ),
                rule_id=ErrorCode.PRO_VERSION_LOOKUP_FAILED,
                severity=Severity.WARNING,
                source_span=source_span,
                target_line_content=changelog_version,
            )
        return max(lookup.results, key=lambda result: Version(result.version), default=None)

    @staticmethod
    def _base_release(distributions: str) -> str | None:
        """Return the base Ubuntu release from a changelog distributions field.

        Strips pocket suffixes (``-proposed`` / ``-updates`` / ``-security`` /
        ``-backports``) so ``is_esm_only_release`` can match against the
        release names ``distro_info`` exposes (e.g. ``xenial``, not
        ``xenial-security``). Returns None for empty or UNRELEASED.
        """
        tokens = parse_distributions_field(distributions)
        if not tokens or tokens[0] == UNRELEASED_DISTRIBUTION:
            return None
        target = tokens[0]
        for pocket in _POCKETS:
            if target.endswith(pocket):
                return target[: -len(pocket)]
        return target
