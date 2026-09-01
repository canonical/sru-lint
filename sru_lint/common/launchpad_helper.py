"""
Helper module for Launchpad integration.
Provides cached Launchpad connection and utility functions.
Uses thread-local storage for connections (httplib2 is not thread-safe).
"""

import re
import threading
from collections.abc import Iterator
from dataclasses import dataclass

from debian.debian_support import Version
from launchpadlib import uris
from launchpadlib.credentials import KeyringCredentialStore, SystemWideConsumer
from launchpadlib.launchpad import Launchpad

from sru_lint.common.errors import ErrorCode
from sru_lint.common.logging import get_logger

# Thread-local storage for Launchpad connections
_thread_local = threading.local()

# Shared cache for valid distributions (protected by lock)
_distributions_cache: set[str] | None = None
_distributions_lock = threading.Lock()

# Shared cache for UCA pairings (series -> set of openstack release names)
_uca_pairings_cache: dict[str, set[str]] | None = None
_uca_pairings_lock = threading.Lock()


@dataclass(frozen=True)
class ProPublication:
    """A source publication in an Ubuntu Pro stream and pocket."""

    version: str
    series: str
    stream: str
    pocket: str


class LaunchpadHelper:
    """
    Helper class for Launchpad interactions.

    Uses thread-local connections to avoid httplib2 thread-safety issues.
    Each thread gets its own Launchpad connection.
    """

    APPLICATION_NAME = "sru-lint"
    SERVICE_ROOT = "production"
    CACHE_DIR = "~/.launchpadlib/cache"
    API_VERSION = "devel"

    PRO_PPAS = [
        "ppa:ubuntu-esm/esm-infra-security",
        "ppa:ubuntu-esm/esm-infra-security-staging",
        "ppa:ubuntu-esm/esm-apps-security",
        "ppa:ubuntu-esm/esm-apps-security-staging",
        "ppa:ubuntu-esm/esm-infra-legacy-security",
        "ppa:ubuntu-esm/esm-infra-legacy-security-staging",
        "ppa:ubuntu-esm/esm-infra-updates",
        "ppa:ubuntu-esm/esm-infra-updates-staging",
        "ppa:ubuntu-esm/esm-apps-updates",
        "ppa:ubuntu-esm/esm-apps-updates-staging",
        "ppa:ubuntu-esm/esm-infra-legacy-updates",
        "ppa:ubuntu-esm/esm-infra-legacy-updates-staging",
    ]

    def __init__(self):
        """Initialize the LaunchpadHelper.

        The actual Launchpad connection is deferred until the first method
        that needs it (via ``_ensure_connection``). Constructing this object
        must not touch the network or the keyring, so that ``sru-lint
        plugins`` and plugin instantiation never trigger an OAuth prompt
        for plugins that don't end up querying Launchpad.
        """
        self.logger = get_logger("launchpad_helper")
        # Set by login() to indicate whether OAuth creds round-tripped
        # successfully through the keyring. None means login() was never
        # called on this instance.
        self.credentials_persisted: bool | None = None

    def _ensure_connection(self):
        """Ensure a Launchpad connection exists for the current thread.

        If credentials from a prior ``sru-lint login`` are present in the
        keyring, the connection is authenticated (so private bugs are
        visible). Otherwise an anonymous session is opened so non-interactive
        runs never trigger a browser OAuth prompt.
        """
        if hasattr(_thread_local, "launchpad"):
            return

        thread_name = threading.current_thread().name
        if self._has_stored_credentials():
            self.logger.info(f"Using stored Launchpad credentials for thread {thread_name}")
            _thread_local.launchpad = Launchpad.login_with(
                self.APPLICATION_NAME,
                self.SERVICE_ROOT,
                self.CACHE_DIR,
                version=self.API_VERSION,
            )
        else:
            self.logger.info(
                f"No stored credentials; opening anonymous Launchpad session "
                f"for thread {thread_name}"
            )
            _thread_local.launchpad = Launchpad.login_anonymously(
                self.APPLICATION_NAME,
                self.SERVICE_ROOT,
                self.CACHE_DIR,
                version=self.API_VERSION,
            )
        _thread_local.ubuntu = _thread_local.launchpad.distributions["ubuntu"]
        _thread_local.archive = _thread_local.ubuntu.main_archive
        self.logger.debug("Launchpad connection established")

    @classmethod
    def _has_stored_credentials(cls) -> bool:
        """Return True if launchpadlib has OAuth credentials cached for this app.

        Non-interactive probe: looks up the same keyring entry that
        launchpadlib's KeyringCredentialStore writes to during login_with.
        Never triggers the OAuth browser flow.

        The credential is stored under ``unique_consumer_id``, which
        launchpadlib derives as ``consumer.key + "@" + service_root_url``
        (see ``RequestTokenAuthorizationEngine.unique_consumer_id``) — not
        the bare consumer key, which would miss the entry.
        """
        try:
            store = KeyringCredentialStore()
            consumer_key = SystemWideConsumer(cls.APPLICATION_NAME).key
            service_root_url = uris.lookup_service_root(cls.SERVICE_ROOT)
            unique_consumer_id = f"{consumer_key}@{service_root_url}"
            return store.load(unique_consumer_id) is not None
        except Exception:
            # Locked / missing / unreadable keyring — treat as "no creds"
            # rather than failing the whole CLI. The anonymous fallback
            # keeps `check` usable.
            return False

    def login(self) -> Launchpad:
        """
        Perform an authenticated Launchpad login via OAuth.

        Triggers the browser-based authorization flow on first use and caches
        the resulting credentials so subsequent invocations reuse them.
        Replaces the current thread's connection with the authenticated one.

        After the OAuth flow completes, this method verifies that credentials
        were actually persisted by re-loading them from the keyring.
        ``self.credentials_persisted`` is set to True on a successful
        round-trip, False otherwise. Persistence can fail silently when
        no usable keyring backend is available (headless session, missing
        GNOME Keyring / KWallet daemon, or snap confinement blocking the
        keyring); the login itself still succeeds for the current process.

        Returns:
            The authenticated Launchpad instance for the current thread.
        """
        self.logger.info(
            f"Performing authenticated Launchpad login for thread {threading.current_thread().name}"
        )

        save_failed = False

        def _on_save_failed():
            nonlocal save_failed
            save_failed = True
            self.logger.warning(
                "launchpadlib reported credential persistence failure "
                "(no writable keyring backend available)"
            )

        _thread_local.launchpad = Launchpad.login_with(
            self.APPLICATION_NAME,
            self.SERVICE_ROOT,
            self.CACHE_DIR,
            version=self.API_VERSION,
            credential_save_failed=_on_save_failed,
        )
        _thread_local.ubuntu = _thread_local.launchpad.distributions["ubuntu"]
        _thread_local.archive = _thread_local.ubuntu.main_archive

        # Verify the save round-trips. The credential_save_failed callback
        # catches explicit save errors, but some keyring chains silently
        # no-op on save when no real backend is available — in that case
        # save_failed stays False but the load returns None.
        self.credentials_persisted = not save_failed and self._has_stored_credentials()
        if self.credentials_persisted:
            self.logger.debug(
                "Authenticated Launchpad connection established and "
                "credentials persisted to keyring"
            )
        else:
            self.logger.warning(
                "Authenticated to Launchpad but credentials did not persist; "
                "subsequent runs will fall back to anonymous access"
            )

        return _thread_local.launchpad

    @property
    def launchpad(self) -> Launchpad:
        """Get the Launchpad instance for the current thread."""
        self._ensure_connection()
        return _thread_local.launchpad

    @property
    def ubuntu(self):
        """Get the Ubuntu distribution object for the current thread."""
        self._ensure_connection()
        return _thread_local.ubuntu

    @property
    def archive(self):
        """Get the Ubuntu main archive object for the current thread."""
        self._ensure_connection()
        return _thread_local.archive

    @staticmethod
    def _parse_ppa_reference(reference: str) -> tuple[str, str]:
        """Parse a PPA reference into ``(owner, ppa_name)``.

        Accepts either the canonical short form ``ppa:<owner>/<name>``
        or the bare ``<owner>/<name>``.

        Raises:
            ValueError: If the reference is not in a recognized form.
        """
        cleaned = reference.strip()
        if cleaned.startswith("ppa:"):
            cleaned = cleaned[len("ppa:") :]
        if cleaned.count("/") != 1 or cleaned.startswith("/") or cleaned.endswith("/"):
            raise ValueError(
                f"Invalid PPA reference {reference!r}; expected '[ppa:]<owner>/<name>'"
            )
        owner, name = cleaned.split("/", 1)
        return owner, name

    def _get_published_sources_in_ppa(self, ppa_reference: str, package_name: str):
        """Return the published sources for ``package_name`` in a PPA."""
        self._ensure_connection()
        owner, name = self._parse_ppa_reference(ppa_reference)
        self.logger.debug(f"Resolving PPA {owner}/{name}")
        archive = _thread_local.launchpad.people[owner].getPPAByName(
            distribution=_thread_local.ubuntu, name=name
        )
        return archive.getPublishedSources(
            source_name=package_name, exact_match=True, status="Published"
        )

    def get_highest_version_in_ppa(self, ppa_reference: str, package_name: str) -> str | None:
        """Return the highest published version of ``package_name`` in a PPA.

        Walks the PPA's currently-published source publications for the
        given package and returns the highest version under Debian version
        ordering (epoch, upstream, revision). The same source/version can
        appear in multiple distroseries; the maximum across them wins.

        Args:
            ppa_reference: PPA in the form ``ppa:<owner>/<name>`` (the
                ``ppa:`` prefix is optional, e.g.
                ``ppa:ubuntu-esm/esm-infra-security``).
            package_name: Source package name to query.

        Returns:
            The highest published version string, or ``None`` if the
            package has no current publications in the PPA.

        Raises:
            ValueError: If ``ppa_reference`` is malformed.
        """
        publications = self._get_published_sources_in_ppa(ppa_reference, package_name)
        versions = [pub.source_package_version for pub in publications]
        if not versions:
            self.logger.debug(f"No published versions of {package_name!r} in {ppa_reference}")
            return None

        highest = max(versions, key=Version)
        self.logger.debug(f"Highest version of {package_name!r} in {ppa_reference}: {highest}")
        return str(highest)

    def get_pro_publications(self, package_name: str) -> Iterator[ProPublication]:
        """Yield source publications for a Pro package."""
        for ppa in self.PRO_PPAS:
            try:
                # ESM publications report Release; archive names encode stream and full pocket.
                archive_name = ppa.removeprefix("ppa:ubuntu-esm/esm-")
                for pocket in ("security-staging", "updates-staging", "security", "updates"):
                    if archive_name.endswith(f"-{pocket}"):
                        stream = archive_name.removesuffix(f"-{pocket}")
                        break
                yield from (
                    ProPublication(
                        publication.source_package_version,
                        publication.distro_series.name,
                        stream,
                        pocket,
                    )
                    for publication in self._get_published_sources_in_ppa(ppa, package_name)
                )
            except Exception as error:
                self.logger.warning(f"Skipping {ppa}: {type(error).__name__}: {error}")

    def get_bug(self, bug_number: int):
        """
        Get a bug by its number.

        Args:
            bug_number: The Launchpad bug number

        Returns:
            The bug object from Launchpad, or None if not found
        """
        self._ensure_connection()

        self.logger.debug(f"Fetching bug #{bug_number}")
        bug = _thread_local.launchpad.bugs[bug_number]
        self.logger.debug(f"Successfully fetched bug #{bug_number}")
        return bug

    def is_bug_targeted(self, bug_number: int, package: str, distribution: str) -> bool:
        """
        Check if a bug is targeted at a specific package and distribution.

        Args:
            bug_number: The Launchpad bug number
            package: The package name
            distribution: The distribution name (e.g., 'jammy', 'focal')

        Returns:
            True if the bug is targeted at the package and distribution, False otherwise
        """
        bug = self.get_bug(bug_number)
        if not bug:
            return False

        self.logger.debug(
            f"Checking if bug #{bug_number} is targeted at {package} in {distribution}"
        )

        for task in bug.bug_tasks:
            self.logger.debug(
                f"Checking task: package={task.target.name}, bug_target_name={task.bug_target_name}"
            )
            # Normalize distribution names for comparison
            package_match = task.target.name == package
            # Check if distribution is in bug_target_name (case-insensitive)
            dist_match = distribution.lower() in task.bug_target_name.lower()
            if package_match and dist_match:
                self.logger.debug(f"Bug #{bug_number} is targeted at {package} in {distribution}")
                return True

        self.logger.debug(f"Bug #{bug_number} is NOT targeted at {package} in {distribution}")
        return False

    def get_uca_bug_targeting(self, bug_number: int, openstack_release: str) -> tuple[bool, bool]:
        """
        Determine whether a bug is targeted at the ``cloud-archive`` Launchpad
        project, and whether a task exists for the specific OpenStack series.

        Args:
            bug_number: The Launchpad bug number.
            openstack_release: The OpenStack release name (e.g. ``'epoxy'``).

        Returns:
            ``(has_project_task, has_series_task)`` where ``has_project_task``
            is True if any task is on the cloud-archive project (or one of
            its series), and ``has_series_task`` is True if a task exists for
            ``cloud-archive/<openstack_release>`` specifically.
        """
        bug = self.get_bug(bug_number)
        if not bug:
            return (False, False)

        has_project = False
        has_series = False

        for task in bug.bug_tasks:
            target = task.target
            try:
                project = getattr(target, "project", None)
                if project is not None and getattr(project, "name", "") == "cloud-archive":
                    has_project = True
                    if getattr(target, "name", "") == openstack_release:
                        has_series = True
                    continue
            except Exception as e:
                self.logger.debug(f"Could not inspect task target as cloud-archive series: {e}")

            try:
                if getattr(target, "name", "") == "cloud-archive":
                    has_project = True
            except Exception as e:
                self.logger.debug(f"Could not inspect task target as cloud-archive project: {e}")

        self.logger.debug(
            f"Bug #{bug_number} UCA targeting: "
            f"project={has_project}, series({openstack_release})={has_series}"
        )
        return (has_project, has_series)

    def get_bug_tasks(self, bug_number: int) -> list:
        """
        Get all bug tasks for a bug.

        Args:
            bug_number: The Launchpad bug number

        Returns:
            List of bug tasks, or empty list if bug not found
        """
        bug = self.get_bug(bug_number)
        if not bug:
            return []

        tasks = list(bug.bug_tasks)
        self.logger.debug(f"Bug #{bug_number} has {len(tasks)} tasks")
        return tasks

    def search_series(self, series_name: str):
        """
        Search for a distribution series by name.

        Args:
            series_name: The series name (e.g., 'jammy', 'focal')

        Returns:
            The series object, or None if not found
        """
        self._ensure_connection()
        try:
            self.logger.debug(f"Searching for series '{series_name}'")
            series = _thread_local.ubuntu.getSeries(name_or_version=series_name)
            self.logger.debug(f"Found series '{series_name}'")
            return series
        except Exception as e:
            self.logger.error(f"Error fetching series '{series_name}': {e}")
            return None

    def get_valid_distributions(self, include_pockets: bool = True) -> set[str]:
        """
        Get a set of valid Ubuntu distribution names.

        This method caches the result to avoid repeated API calls.

        Args:
            include_pockets: If True, includes pocket suffixes like -proposed, -updates, -security

        Returns:
            Set of valid distribution names (e.g., {'jammy', 'focal', 'jammy-proposed', ...})
        """
        global _distributions_cache

        with _distributions_lock:
            if _distributions_cache is not None and include_pockets:
                self.logger.debug(f"Using cached distributions ({len(_distributions_cache)} items)")
                return _distributions_cache

        self._ensure_connection()
        self.logger.info(f"Fetching valid distributions (include_pockets={include_pockets})")

        distributions = set()
        pockets = (
            ["", "-proposed", "-updates", "-security", "-backports"] if include_pockets else [""]
        )

        try:
            # Get all series (including current and supported releases)
            series_count = 0
            for series in _thread_local.ubuntu.series:
                # Only include series that are current or supported
                if series.active:
                    series_name = series.name
                    self.logger.debug(f"Adding active series: {series_name}")
                    for pocket in pockets:
                        distributions.add(f"{series_name}{pocket}")
                    series_count += 1

            # Cache the full set (with pockets) for future use
            if include_pockets:
                with _distributions_lock:
                    _distributions_cache = distributions

            self.logger.info(
                f"Fetched {series_count} active series, generated {len(distributions)} distribution names"
            )

        except Exception as e:
            self.logger.error(f"Error fetching valid distributions: {e}")
            self.logger.warning("Using fallback distribution list")
            # Return a minimal set of known distributions as fallback
            distributions = {
                "questing",
                "questing-proposed",
                "questing-updates",
                "questing-security",
                "plucky",
                "plucky-proposed",
                "plucky-updates",
                "plucky-security",
                "noble",
                "noble-proposed",
                "noble-updates",
                "noble-security",
                "jammy",
                "jammy-proposed",
                "jammy-updates",
                "jammy-security",
                "focal",
                "focal-proposed",
                "focal-updates",
                "focal-security",
            }

        return distributions

    def is_valid_distribution(self, distribution: str) -> bool:
        """
        Check if a distribution name is valid.

        Args:
            distribution: The distribution name to check (e.g., 'jammy', 'jammy-proposed')

        Returns:
            True if the distribution is valid, False otherwise
        """
        valid_distributions = self.get_valid_distributions()
        is_valid = distribution in valid_distributions
        self.logger.debug(f"Distribution '{distribution}' is {'valid' if is_valid else 'invalid'}")
        return is_valid

    def get_uca_pairings(self) -> dict[str, set[str]]:
        """
        Get the mapping of Ubuntu series -> valid OpenStack releases for the
        Ubuntu Cloud Archive.

        Queries the ~ubuntu-cloud-archive team's PPAs. PPA names follow the
        pattern <openstack-release> or <openstack-release>-staging; the
        Ubuntu series is inferred from each PPA's published sources.
        Results are cached for the lifetime of the process.

        Returns:
            Dict like {'jammy': {'antelope', 'bobcat', 'caracal'}, ...}
        """
        global _uca_pairings_cache

        with _uca_pairings_lock:
            if _uca_pairings_cache is not None:
                self.logger.debug(f"Using cached UCA pairings ({len(_uca_pairings_cache)} series)")
                return _uca_pairings_cache

        self._ensure_connection()
        self.logger.info("Fetching UCA pairings from Launchpad")

        pairings: dict[str, set[str]] = {}
        try:
            team = _thread_local.launchpad.people["ubuntu-cloud-archive"]
            for ppa in team.ppas:
                ppa_name = ppa.name
                openstack = ppa_name
                for suffix in ("-staging", "-proposed"):
                    if openstack.endswith(suffix):
                        openstack = openstack[: -len(suffix)]
                        break
                if not openstack or not openstack.isalpha():
                    continue
                try:
                    for pub in ppa.getPublishedSources(status="Published"):
                        series_name = pub.distro_series.name
                        pairings.setdefault(series_name, set()).add(openstack)
                        break
                except Exception as e:
                    self.logger.debug(f"Could not determine series for PPA '{ppa_name}': {e}")
                    continue

            with _uca_pairings_lock:
                _uca_pairings_cache = pairings

            self.logger.info(
                f"Fetched UCA pairings for {len(pairings)} series "
                f"({sum(len(v) for v in pairings.values())} pockets)"
            )
        except Exception as e:
            self.logger.error(f"Error fetching UCA pairings: {e}")
            self.logger.warning("Using fallback UCA pairings list")
            pairings = {
                "focal": {"ussuri", "victoria", "wallaby", "xena", "yoga"},
                "jammy": {"yoga", "zed", "antelope", "bobcat", "caracal"},
                "noble": {"caracal", "dalmatian", "epoxy"},
            }

        return pairings

    def is_valid_uca_distribution(self, distribution: str) -> tuple[bool, ErrorCode | None]:
        """
        Validate that a distribution string matches a real Ubuntu Cloud
        Archive pocket of the form <ubuntu-series>-<openstack-release>.

        Args:
            distribution: The distribution name (e.g., 'jammy-caracal')

        Returns:
            (True, None) if valid.
            (False, UCA_INVALID_DISTRIBUTION) if the shape or series is wrong.
            (False, UCA_UNKNOWN_OPENSTACK_RELEASE) if the OpenStack name is
                not known in any UCA pairing.
            (False, UCA_INVALID_PAIRING) if both halves are known but do not
                pair together.
        """
        if not distribution or "-" not in distribution:
            return False, ErrorCode.UCA_INVALID_DISTRIBUTION
        series, _, openstack = distribution.rpartition("-")
        if not series or not openstack:
            return False, ErrorCode.UCA_INVALID_DISTRIBUTION

        pairings = self.get_uca_pairings()
        all_openstack = {os_name for s in pairings.values() for os_name in s}

        if series not in pairings:
            return False, ErrorCode.UCA_INVALID_DISTRIBUTION
        if openstack not in all_openstack:
            return False, ErrorCode.UCA_UNKNOWN_OPENSTACK_RELEASE
        if openstack not in pairings[series]:
            return False, ErrorCode.UCA_INVALID_PAIRING
        return True, None

    @staticmethod
    def extract_lp_bugs(text: str) -> list[int]:
        """
        Extract Launchpad bug numbers from text.

        Args:
            text: The text to search for bug numbers (e.g., changelog entry)

        Returns:
            List of bug numbers found in the text
        """
        logger = get_logger("launchpad_helper")
        matches = re.findall(r"LP:\s*#(\d+)", text)
        bug_numbers = [int(match) for match in matches]
        logger.debug(f"Extracted {len(bug_numbers)} LP bug numbers from text: {bug_numbers}")
        return bug_numbers

    @staticmethod
    def get_upload_queue_url(package_name: str, distribution: str) -> str:
        """
        Construct the Launchpad upload queue URL for a package and distribution.

        Args:
            package_name: The package name
            distribution: The distribution name (e.g., 'jammy', 'focal')
        Returns:
            The URL string to the upload queue page
        """
        return f"https://launchpad.net/ubuntu/{distribution}/+queue?queue_state=1&queue_text={package_name}"

    @staticmethod
    def get_publishing_history_url(package_name: str) -> str:
        """
        Construct the Launchpad publishing history URL for a package.

        Args:
            package_name: The package name
        Returns:
            The URL string to the publishing history page
        """
        return f"https://launchpad.net/ubuntu/+source/{package_name}/+publishinghistory"

    # (label, regex) pairs for the SRU template sections expected in a bug
    # description. Match is case-insensitive with flexible whitespace inside
    # the square brackets.
    SRU_TEMPLATE_TAGS: tuple[tuple[str, str], ...] = (
        ("[Impact]", r"\[\s*Impact\s*\]"),
        ("[Test Plan]", r"\[\s*Test\s+Plan\s*\]"),
        ("[Where problems could occur]", r"\[\s*Where\s+problems\s+could\s+occur\s*\]"),
    )

    def has_sru_template(self, bug_number: int) -> list[str]:
        """Return the SRU template tags missing from a bug's description.

        Each of :attr:`SRU_TEMPLATE_TAGS` is searched for case-insensitively
        with flexible whitespace inside the brackets. The return value is the
        list of tag labels (e.g. ``"[Impact]"``) NOT found in the description;
        an empty list means the template is complete.

        Note the polarity change from the previous bool API: an empty list
        is the "all present" case, so callers can write ``if missing:``.

        Args:
            bug_number: The Launchpad bug number.

        Returns:
            List of missing tag labels (empty list when none are missing).
        """
        bug = self.get_bug(bug_number)
        self.logger.debug(f"Checking SRU template for bug #{bug_number}")
        description = bug.description or ""

        missing: list[str] = []
        for label, pattern in self.SRU_TEMPLATE_TAGS:
            if re.search(pattern, description, re.IGNORECASE):
                self.logger.debug(f"Found SRU tag {label!r} in bug #{bug_number}")
            else:
                missing.append(label)

        if missing:
            self.logger.debug(f"LP: #{bug_number} is missing SRU template tags: {missing}")
        else:
            self.logger.debug(f"LP: #{bug_number} has a complete SRU template")
        return missing


# Create a single global instance
_launchpad_helper = None


def get_launchpad_helper() -> LaunchpadHelper:
    """
    Get the global LaunchpadHelper instance.

    Note: The helper uses thread-local connections internally,
    so it's safe to use from multiple threads.

    Returns:
        The LaunchpadHelper instance
    """
    global _launchpad_helper
    if _launchpad_helper is None:
        logger = get_logger("launchpad_helper")
        logger.debug("Creating new LaunchpadHelper instance")
        _launchpad_helper = LaunchpadHelper()
    return _launchpad_helper
