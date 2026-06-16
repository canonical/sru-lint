"""
dep3_checker.py
================

This module provides a function to validate whether a given patch file (passed
as a string) contains a header that complies with the Debian DEP-3 Patch
Tagging Guidelines.

DEP-3 (Debian Enhancement Proposal 3) defines a minimal set of meta-data
fields that should be embedded at the top of any patch distributed within a
Debian source package.  These fields are stored in one or more RFC-2822 style
headers and allow tooling and humans to understand the purpose and origin
of a patch.  According to the specification:

* A header starts on the first non-empty line of the patch and ends on the
  first empty line.  A second header, called the *pseudo-header*, may appear
  after a blank line.  Any parsing must stop when a line containing exactly
  three dashes (``---``) is encountered.
* Each field consists of a name followed by a colon and its value.  Continuation
  lines begin with a single space (or ``#  `` when the header is commented
  out) and are folded into the value of the previous field.  Unknown lines
  outside of field definitions are treated as free-form text and appended to
  the description.
* At least one of ``Description`` or ``Subject`` must be present, and the
  short description (the text after the colon on the first line) must be
  non-empty.
* An ``Origin`` field is required unless an ``Author`` (or its alias ``From``)
  is provided.  ``Subject`` is considered an alias
  for ``Description``, and ``From`` is an alias for ``Author``.
* Additional optional fields, such as ``Bug``, ``Forwarded``, ``Last-Update``
  and ``Applied-Upstream``, are ignored for the purposes of compliance
  checking.  However, if present, this implementation performs basic
  validation of ``Last-Update`` (must follow the ISO ``YYYY-MM-DD`` date
  format) and ``Forwarded`` (must be either ``no``, ``not-needed`` or a
  plausible URL as per the guidelines).

The :func:`check_dep3_compliance` function returns a tuple consisting of a
boolean indicating compliance and a list of strings describing any detected
issues.  A patch is considered compliant if it includes a non-empty
``Description`` or ``Subject`` and either an ``Origin`` or an ``Author``/
``From`` field.  Additional validity checks are performed on optional
fields when they are present.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlparse

from sru_lint.common.errors import ErrorCode
from sru_lint.common.feedback import FeedbackItem, Severity, SourceLine, SourceSpan


def _strip_comment_prefix(line: str) -> str:
    """Remove a leading comment marker (`#`) and a single following space.

    Some patch management tools store DEP-3 meta-data inside shell comments
    (e.g. `# Description: …`).  This helper strips one leading `#` along
    with a single space that may follow so that the remainder of the line
    contains only the field specification.  Lines with multiple `#`
    characters (common in diff hunks) are left unchanged.

    Parameters
    ----------
    line: str
        The raw line from the patch file.

    Returns
    -------
    str
        The line with the first comment marker removed, if present.
    """
    stripped = line.lstrip()
    # Only strip a single '#' and an optional following space
    if stripped.startswith("#"):
        # Remove the first '#'
        stripped = stripped[1:]
        # Remove one leading space if present
        if stripped.startswith(" "):
            stripped = stripped[1:]
        return stripped
    return line


def _is_valid_date(value: str) -> bool:
    """Return True if *value* is a valid ISO date (YYYY-MM-DD)."""
    try:
        datetime.strptime(value.strip(), "%Y-%m-%d")
        return True
    except Exception:
        return False


def _is_plausible_url(value: str) -> bool:
    """Return True if *value* looks like a URL with scheme and network location.

    The DEP-3 guidelines recommend using URLs for fields such as ``Origin``
    (when available) and for the ``Forwarded`` field when the patch has been
    sent upstream. A reasonable heuristic is to accept strings that parse
    into a scheme and network location using :func:`urllib.parse.urlparse`.
    """
    v = value.strip()
    if not v:
        return False
    parsed = urlparse(v)
    return bool(parsed.scheme) and bool(parsed.netloc)


def _validate_forwarded(value: str) -> bool:
    """Validate Forwarded field value.

    According to DEP-3, any value other than 'no' or 'not-needed' indicates
    that the patch has been forwarded upstream; ideally it should be a URL.
    """
    v = value.strip().lower()
    if not v:
        return True  # Empty is acceptable
    if v in ("no", "not-needed"):
        return True
    return _is_plausible_url(value)


@dataclass
class Dep3FieldDefinition:
    """Definition of a DEP3 header field.

    Attributes:
        names: List of field names (first is primary, rest are aliases)
        required: Whether at least one of the names must be present
        requires_content: Whether the field must have non-empty content
        allow_multiline: Whether the field value can span multiple lines
        validator: Optional function to validate the field value
        error_code: ErrorCode to use when validation fails
        error_message: Error message template for validation failures
        severity: Severity level for validation failures
    """

    names: list[str]
    required: bool
    requires_content: bool = False
    allow_multiline: bool = False
    validator: Callable[[str], bool] | None = None
    error_code: ErrorCode | None = None
    error_message: str | None = None
    severity: Severity = Severity.ERROR


# Define DEP3 field requirements
DEP3_FIELD_DEFINITIONS = [
    # Description/Subject is required and must have non-empty content
    Dep3FieldDefinition(
        names=["description", "subject"],
        required=True,
        requires_content=True,
        allow_multiline=True,
        error_code=ErrorCode.PATCH_DEP3_MISSING_DESCRIPTION,
        error_message="Missing required Description/Subject field",
        severity=Severity.ERROR,
    ),
    # Origin OR Author/From is required (handled specially in validation)
    Dep3FieldDefinition(
        names=["origin"],
        required=False,  # Required as group with Author/From
        requires_content=False,
        allow_multiline=False,
    ),
    Dep3FieldDefinition(
        names=["author", "from"],
        required=False,  # Required as group with Origin
        requires_content=False,
        allow_multiline=False,
    ),
    # Optional fields with validation
    Dep3FieldDefinition(
        names=["last-update"],
        required=False,
        allow_multiline=False,
        validator=_is_valid_date,
        error_code=ErrorCode.PATCH_DEP3_INVALID_DATE,
        error_message="Last-Update field must be a valid ISO date (YYYY-MM-DD)",
        severity=Severity.WARNING,
    ),
    Dep3FieldDefinition(
        names=["forwarded"],
        required=False,
        allow_multiline=False,
        validator=_validate_forwarded,
        error_code=ErrorCode.PATCH_DEP3_INVALID_FORWARDED,
        error_message='Forwarded field should be either "no", "not-needed" or a valid URL',
        severity=Severity.WARNING,
    ),
    Dep3FieldDefinition(
        names=["bug", "bug-ubuntu", "bug-debian"],
        required=False,
        requires_content=True,
        allow_multiline=False,
    ),
]


class Dep3HeaderParser:
    """Parser for DEP3 patch headers."""

    def __init__(self, field_definitions: list[Dep3FieldDefinition]):
        self.field_definitions = field_definitions
        # Build a mapping from field names to their definitions
        self.field_map: dict[str, Dep3FieldDefinition] = {}
        for field_def in field_definitions:
            for name in field_def.names:
                self.field_map[name.lower()] = field_def

    def parse(self, patch_text: str, file_path: str = "patch") -> dict[str, tuple[str, int]]:
        """Parse DEP3 headers from patch text.

        Returns:
            Dictionary mapping field names to (value, line_number) tuples.
        """
        lines = patch_text.replace("\r\n", "\n").split("\n")

        # Find header section (stops at ---)
        header_lines: list[tuple[str, int]] = []
        for line_num, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped == "---":
                break
            header_lines.append((line, line_num))

        # Parse fields
        fields: dict[str, tuple[str, int]] = {}
        current_field: str | None = None
        current_value: str = ""
        current_line: int = 1

        for raw_line, line_num in header_lines:
            line = _strip_comment_prefix(raw_line).rstrip("\r\n")

            # Empty line ends the current stanza
            if not line.strip():
                if current_field:
                    fields[current_field] = (current_value, current_line)
                    current_field = None
                    current_value = ""
                continue

            # Check for field definition: <name>:<value>
            m = re.match(r"^(?P<name>[\w.-]+)\s*:\s*(?P<value>.*)$", line)
            if m:
                # Save previous field if any
                if current_field:
                    fields[current_field] = (current_value, current_line)

                # Start new field
                current_field = m.group("name").strip().lower()
                current_value = m.group("value")
                current_line = line_num
                continue

            # Continuation line (starts with space or tab)
            if current_field and line.startswith((" ", "\t")):
                current_value += "\n" + line.strip()
                continue

            # Unknown line that is not a field header and not a continuation:
            # per DEP-3, such free-form text is appended to the current field
            # value (typically the Subject body).
            if current_field:
                current_value += "\n" + line.strip()

        # Save final field if any
        if current_field:
            fields[current_field] = (current_value, current_line)

        return fields


def check_dep3_compliance(
    patch_text: str, file_path: str = "patch"
) -> tuple[bool, list[FeedbackItem]]:
    """Check whether a patch complies with the Debian DEP-3 Tagging Guidelines.

    Parameters
    ----------
    patch_text: str
        The complete contents of a patch file.  Newlines may be either LF or
        CR-LF; line endings are normalised internally.
    file_path: str
        The path to the patch file for creating source spans.

    Returns
    -------
    (bool, list of FeedbackItem)
        A tuple containing a boolean indicating compliance and a list of
        FeedbackItem objects explaining any issues that were found.  An empty
        list of feedback items implies that the patch is compliant.

    Examples
    --------
    >>> compliant_patch = (
    ...     "Description: Fix widget frobnication speeds\n"
    ...     "Forwarded: http://lists.example.com/2010/03/1234.html\n"
    ...     "Author: John Doe <jdoe@example.com>\n"
    ...     "Last-Update: 2010-03-29\n"
    ...     "---\n"
    ... )
    >>> compliance_result, feedback_items = check_dep3_compliance(compliant_patch)
    >>> compliance_result
    True
    >>> len(feedback_items)
    0

    >>> non_compliant_patch = (
    ...     "# Patch without mandatory fields\n"
    ...     "---\n"
    ... )
    >>> compliance_result, feedback_items = check_dep3_compliance(non_compliant_patch)
    >>> compliance_result
    False
    >>> len(feedback_items) > 0
    True
    """
    # Parse headers using the structured parser
    parser = Dep3HeaderParser(DEP3_FIELD_DEFINITIONS)
    fields = parser.parse(patch_text, file_path)

    feedback_items: list[FeedbackItem] = []

    # Helper function to create a SourceSpan for DEP-3 feedback
    def create_dep3_source_span(line_number: int) -> SourceSpan:
        source_line = SourceLine(
            content="",
            line_number=line_number,
            is_added=True,
        )
        return SourceSpan(
            path=file_path,
            start_line=line_number,
            start_col=1,
            end_line=line_number,
            end_col=1,
            content=[source_line],
            content_with_context=[source_line],
        )

    # Check each field definition
    for field_def in DEP3_FIELD_DEFINITIONS:
        # Check if any of the alternative names for this field are present
        found_fields = [(name, fields[name]) for name in field_def.names if name in fields]

        if field_def.required and not found_fields:
            # Required field is missing
            source_span = create_dep3_source_span(1)
            feedback_items.append(
                FeedbackItem(
                    message=field_def.error_message
                    or f"Missing required field: {'/'.join(field_def.names)}",
                    rule_id=field_def.error_code or ErrorCode.PATCH_DEP3_FORMAT,
                    severity=field_def.severity,
                    span=source_span,
                )
            )
            continue

        # Check content requirement and validation
        for field_name, (value, line_num) in found_fields:
            if not field_def.allow_multiline and "\n" in value:
                source_span = create_dep3_source_span(line_num)
                feedback_items.append(
                    FeedbackItem(
                        message=f"The {field_name.capitalize()} field must be a single line",
                        rule_id=ErrorCode.PATCH_DEP3_FORMAT,
                        severity=field_def.severity,
                        span=source_span,
                    )
                )

            # Check if field requires non-empty content. The content must be on the
            # first line (the text after the colon on the field-name line itself).
            if field_def.requires_content and not value.split("\n")[0].strip():
                source_span = create_dep3_source_span(line_num)
                feedback_items.append(
                    FeedbackItem(
                        message=f"The {field_name.capitalize()} field must contain a short description on its first line",
                        rule_id=ErrorCode.PATCH_DEP3_EMPTY_DESCRIPTION,
                        severity=field_def.severity,
                        span=source_span,
                    )
                )

            # Run validator if provided
            if field_def.validator and value.strip():
                if not field_def.validator(value):
                    source_span = create_dep3_source_span(line_num)
                    feedback_items.append(
                        FeedbackItem(
                            message=field_def.error_message or f"Invalid value for {field_name}",
                            rule_id=field_def.error_code or ErrorCode.PATCH_DEP3_FORMAT,
                            severity=field_def.severity,
                            span=source_span,
                        )
                    )

    # Special case: Check that either Origin OR Author/From is present
    origin_present = "origin" in fields
    author_present = any(name in fields for name in ["author", "from"])

    if not (origin_present or author_present):
        source_span = create_dep3_source_span(1)
        feedback_items.append(
            FeedbackItem(
                message="Either an Origin field or an Author/From field must be provided",
                rule_id=ErrorCode.PATCH_DEP3_MISSING_ORIGIN_AUTHOR,
                severity=Severity.ERROR,
                span=source_span,
            )
        )

    return (not feedback_items, feedback_items)


__all__ = [
    "check_dep3_compliance",
]
