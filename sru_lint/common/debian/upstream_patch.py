"""
Utilities for comparing patches against upstream versions.

This module provides functionality to extract upstream references from DEP-3
headers and compare local patches against their upstream counterparts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sru_lint.common.logging import get_logger

logger = get_logger("upstream_patch")


@dataclass
class UpstreamReference:
    """Represents an upstream patch reference from a DEP-3 Origin tag."""

    url: str | None = None
    commit: str | None = None
    is_backport: bool = False
    raw_value: str = ""

    @classmethod
    def from_origin_tag(cls, origin_value: str) -> UpstreamReference:
        """
        Parse an Origin tag value to extract upstream reference information.

        Args:
            origin_value: The value of the Origin field from DEP-3 header

        Returns:
            UpstreamReference object with parsed information
        """
        ref = cls(raw_value=origin_value)

        if not origin_value:
            return ref

        # Check if this is explicitly marked as a backport
        origin_lower = origin_value.lower()
        ref.is_backport = "backport" in origin_lower

        # Look for URLs (commonly github.com with /commit/)
        url_pattern = r"https?://[^\s]+"
        url_matches = re.findall(url_pattern, origin_value)
        if url_matches:
            ref.url = url_matches[0]
            # Extract commit hash from GitHub URLs
            if ref.url and "github.com" in ref.url and "/commit/" in ref.url:
                commit_match = re.search(r"/commit/([a-f0-9]+)", ref.url)
                if commit_match:
                    ref.commit = commit_match.group(1)

        # Look for commit hashes (SHA-1 or SHA-256 format)
        # Git hashes are typically 40 chars (SHA-1) or 64 chars (SHA-256)
        hash_pattern = r"\b([a-f0-9]{40}|[a-f0-9]{64})\b"
        hash_matches = re.findall(hash_pattern, origin_value)
        if hash_matches and not ref.commit:
            ref.commit = hash_matches[0]

        # Also check for short commit hashes (7+ chars)
        if not ref.commit:
            short_hash_pattern = r"\bcommit\s+([a-f0-9]{7,})\b"
            short_matches = re.findall(short_hash_pattern, origin_value)
            if short_matches:
                ref.commit = short_matches[0]

        return ref


def extract_patch_hunks(patch_content: str) -> list[str]:
    """
    Extract the actual diff hunks from a patch, excluding the DEP-3 header.

    Args:
        patch_content: The full patch file content

    Returns:
        List of hunk content lines (starting from the first @@)
    """
    lines = patch_content.split("\n")
    hunks = []
    in_header = True

    for line in lines:
        if in_header:
            # Header ends at the --- separator or at a hunk start
            if line.startswith("---"):
                in_header = False
                continue
            if line.startswith("@@"):
                in_header = False
                hunks.append(line)
                continue
        else:
            hunks.append(line)

    return hunks


def compare_patch_content(local_patch: str, upstream_patch: str) -> tuple[bool, list[str]]:
    """
    Compare local patch against upstream patch.

    Checks if there are substantive differences beyond context lines
    and line numbers.

    Args:
        local_patch: The local/backported patch content
        upstream_patch: The upstream patch content

    Returns:
        Tuple of (matches_upstream, differences) where:
        - matches_upstream: True if patches are substantively identical
        - differences: List of detected differences
    """
    differences = []

    local_hunks = extract_patch_hunks(local_patch)
    upstream_hunks = extract_patch_hunks(upstream_patch)

    if not local_hunks or not upstream_hunks:
        return False, ["Could not extract hunks from one or both patches"]

    # Extract only the meaningful content (+ and - lines, not context)
    def get_meaningful_lines(hunks):
        meaningful = []
        for line in hunks:
            if line.startswith(("+", "-")) and not line.startswith(("+++", "---")):
                # Remove the leading +/- for comparison
                meaningful.append(line[1:])
        return set(meaningful)

    local_meaningful = get_meaningful_lines(local_hunks)
    upstream_meaningful = get_meaningful_lines(upstream_hunks)

    if local_meaningful != upstream_meaningful:
        differences.append("Patch content differs from upstream (not just line numbers/context)")

        # Find specific differences
        only_in_local = local_meaningful - upstream_meaningful
        only_in_upstream = upstream_meaningful - local_meaningful

        if only_in_local:
            differences.append(f"Lines added locally not in upstream: {len(only_in_local)} line(s)")
        if only_in_upstream:
            differences.append(
                f"Lines from upstream missing locally: {len(only_in_upstream)} line(s)"
            )

    return not differences, differences
