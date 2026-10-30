"""Repository catalogue and URL construction for madison."""

import re
from itertools import product

ARCHITECTURES = (
    "amd64",
    "i386",
    "arm64",
    "armel",
    "armhf",
    "powerpc",
    "ppc64el",
    "riscv64",
    "s390x",
)


def pockets(releases: str, suffix: str) -> tuple[str, ...]:
    return tuple(
        f"{release}-{suffix}-{pocket}"
        for release, pocket in product(releases.split(), ("security", "updates"))
    )


RECENT = ("bionic", "focal", "jammy", "noble", "resolute")
CATEGORIES: dict[str, tuple[str, ...]] = {
    "infra": pockets("trusty xenial bionic focal jammy noble resolute", "infra"),
    "infra-legacy": pockets("trusty xenial", "infra-legacy"),
    "apps": pockets("xenial bionic focal jammy noble resolute", "apps"),
    "apps-legacy": pockets("xenial", "apps-legacy"),
    "fips": ("xenial", "bionic", "focal", "jammy"),
    "fips-preview": ("focal", "jammy"),
    "fips-updates": tuple(f"{r}-updates" for r in ("xenial", "bionic", "focal", "jammy", "noble")),
    "kernel-ppa": ("focal", "jammy", "noble", "resolute"),
    "kernel-ppa2": ("focal", "jammy", "noble", "resolute"),
    "realtime": ("jammy", "noble"),
    "ros": tuple(
        f"{r}-{p}" for r, p in product(("xenial", "bionic", "focal"), ("updates", "security"))
    ),
    "ros-updates": ("xenial-updates", "bionic-updates"),
}
for version in (
    "19.10",
    "23.03",
    "24.04",
    "26.04",
    "beta",
    "latest-stable",
    "latest-stable-client",
):
    CATEGORIES[f"landscape-{version}"] = RECENT
for version in ("2.7", "2.8", "2.9", *(f"3.{minor}" for minor in range(9))):
    CATEGORIES[f"maas-{version}"] = RECENT


def suite_matches_release(suite: str, release: str) -> bool:
    """Return True if ``suite`` is ``release`` or one of its pockets, e.g. ``jammy-infra-security``."""
    return suite == release or suite.startswith(f"{release}-")


def index_url(category: str, suite: str, architecture: str) -> str:
    """Build an index URL, rejecting unrecognized or unsafe components."""
    if category not in CATEGORIES:
        raise ValueError(f"Unknown category: {category}")
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*", suite):
        raise ValueError(f"Invalid suite: {suite}")
    if architecture not in ARCHITECTURES:
        raise ValueError(f"Unknown architecture: {architecture}")
    if category.startswith("kernel-"):
        base = f"https://ppa.launchpadcontent.net/canonical-kernel-team/{category.removeprefix('kernel-')}/ubuntu"
    elif category.startswith("landscape-"):
        version = category.removeprefix("landscape-")
        ppa = (
            version
            if version == "19.10" or version.startswith("latest-stable")
            else f"self-hosted-{version}"
        )
        base = f"https://ppa.launchpadcontent.net/landscape/{ppa}/ubuntu"
    elif category.startswith("maas-"):
        base = f"https://ppa.launchpadcontent.net/maas/{category.removeprefix('maas-')}/ubuntu"
    else:
        base = f"https://esm.ubuntu.com/{category}/ubuntu"
    return f"{base}/dists/{suite}/main/binary-{architecture}/Packages.gz"
