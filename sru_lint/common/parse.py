import re

DEBIAN_CHANGELOG = "debian/changelog"
DEBIAN_PATCHES = "debian/patches/"

REVIEW_STATES = {"New", "Unapproved"}

UNRELEASED_DISTRIBUTION = "UNRELEASED"

POCKET_SUFFIXES = ("-proposed", "-updates", "-security", "-backports")


def parse_distributions_field(value: str) -> list[str]:
    """
    Debian changelog 'distributions' can contain multiple suites separated by
    whitespace and sometimes commas. Examples: 'jammy', 'jammy-proposed',
    'jammy jammy-proposed', 'UNRELEASED'.
    """
    if not value:
        return []
    # normalize commas to spaces, collapse whitespace
    norm = re.sub(r"[,\s]+", " ", value).strip()
    tokens = norm.split()
    return tokens


def base_release(distributions: str) -> str | None:
    """Return the base Ubuntu release from a changelog distributions field.

    Strips pocket suffixes (``-proposed`` / ``-updates`` / ``-security`` /
    ``-backports``) so ``jammy-security`` becomes ``jammy``. Only the first
    suite is considered. Returns None for empty or UNRELEASED.
    """
    tokens = parse_distributions_field(distributions)
    if not tokens or tokens[0] == UNRELEASED_DISTRIBUTION:
        return None
    target = tokens[0]
    for pocket in POCKET_SUFFIXES:
        if target.endswith(pocket):
            return target[: -len(pocket)]
    return target


def find_offset(lines, search_text: str) -> tuple[int, int]:
    """
    Find the line offset of a given text in a source span.

    Args:
        source_span: The source span to search within
        search_text: The text to find
    Returns:
        A tuple containing the line number and column offset of the found text, or (-1, -1) if not found.
    """
    for line_number, line in enumerate(lines):
        column_offset = line.content.find(search_text)
        if column_offset != -1:
            return (line_number, column_offset)
    return (-1, -1)
