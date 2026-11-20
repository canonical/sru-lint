"""
Tests for upstream patch comparison functionality.
"""

import unittest

from sru_lint.common.debian.upstream_patch import (
    UpstreamReference,
    compare_patch_content,
    extract_patch_hunks,
)


class TestUpstreamReference(unittest.TestCase):
    """Test parsing of upstream references from Origin tags."""

    def test_backport_detection(self):
        """Backport keyword should be detected."""
        ref = UpstreamReference.from_origin_tag("backport from upstream")
        self.assertTrue(ref.is_backport)

    def test_github_url_extraction(self):
        """GitHub URLs should be extracted."""
        origin = "https://github.com/owner/repo/commit/abc123def456"
        ref = UpstreamReference.from_origin_tag(origin)
        self.assertEqual(ref.url, "https://github.com/owner/repo/commit/abc123def456")
        self.assertEqual(ref.commit, "abc123def456")

    def test_github_url_with_backport(self):
        """GitHub URL with backport keyword should work."""
        origin = "backport of https://github.com/owner/repo/commit/abc123"
        ref = UpstreamReference.from_origin_tag(origin)
        self.assertTrue(ref.is_backport)
        self.assertEqual(ref.url, "https://github.com/owner/repo/commit/abc123")

    def test_commit_hash_extraction(self):
        """Commit hashes should be extracted."""
        origin = "upstream commit abc123def456abc123def456abc123def456abc123de"
        ref = UpstreamReference.from_origin_tag(origin)
        self.assertIsNotNone(ref.commit)

    def test_empty_origin(self):
        """Empty origin should be handled."""
        ref = UpstreamReference.from_origin_tag("")
        self.assertFalse(ref.is_backport)
        self.assertIsNone(ref.url)
        self.assertIsNone(ref.commit)

    def test_no_url_in_origin(self):
        """Origin without URL should still parse."""
        ref = UpstreamReference.from_origin_tag("some upstream reference")
        self.assertIsNone(ref.url)
        self.assertFalse(ref.is_backport)


class TestPatchHunkExtraction(unittest.TestCase):
    """Test extraction of hunks from patches."""

    def test_extract_hunks_simple(self):
        """Simple hunk extraction should work."""
        patch = """Description: Fix something
Origin: upstream
---
 file.c | 2 ++
 1 file changed, 2 insertions(+)

--- a/file.c
+++ b/file.c
@@ -1,3 +1,5 @@
 line 1
+added line
 line 2
"""
        hunks = extract_patch_hunks(patch)
        self.assertTrue(any(line.startswith("@@") for line in hunks))

    def test_extract_hunks_no_header(self):
        """Patches without DEP-3 header should still work."""
        patch = """--- a/file.c
+++ b/file.c
@@ -1,3 +1,4 @@
 line 1
+added line
 line 2
"""
        hunks = extract_patch_hunks(patch)
        self.assertTrue(any(line.startswith("@@") for line in hunks))

    def test_extract_hunks_multiple(self):
        """Multiple hunks should be extracted."""
        patch = """--- a/file1.c
+++ b/file1.c
@@ -1,3 +1,4 @@
 line 1
+added
 line 2

--- a/file2.c
+++ b/file2.c
@@ -5,3 +5,4 @@
 line 5
+added
 line 6
"""
        hunks = extract_patch_hunks(patch)
        hunk_starts = [line for line in hunks if line.startswith("@@")]
        self.assertEqual(len(hunk_starts), 2)


class TestPatchComparison(unittest.TestCase):
    """Test comparing patches."""

    def test_identical_patches(self):
        """Identical patches should match."""
        patch1 = """--- a/file.c
+++ b/file.c
@@ -1,3 +1,4 @@
 line 1
+added
 line 2
"""
        patch2 = patch1
        matches, diffs = compare_patch_content(patch1, patch2)
        self.assertTrue(matches)
        self.assertEqual(len(diffs), 0)

    def test_different_patches(self):
        """Different patches should not match."""
        patch1 = """--- a/file.c
+++ b/file.c
@@ -1,3 +1,4 @@
 line 1
+added line 1
 line 2
"""
        patch2 = """--- a/file.c
+++ b/file.c
@@ -1,3 +1,4 @@
 line 1
+added line 2
 line 2
"""
        matches, diffs = compare_patch_content(patch1, patch2)
        self.assertFalse(matches)
        self.assertTrue(any("content differs" in d.lower() for d in diffs))

    def test_context_only_differences(self):
        """Patches differing only in context should match."""
        # Same content changes but different context lines
        patch1 = """--- a/file.c
+++ b/file.c
@@ -1,3 +1,4 @@
 context 1
+added
 context 2
"""
        patch2 = """--- a/file.c
+++ b/file.c
@@ -5,3 +5,4 @@
 different context 1
+added
 different context 2
"""
        matches, diffs = compare_patch_content(patch1, patch2)
        # Both have the same added line, so should match in terms of content
        self.assertTrue(matches)

    def test_line_number_only_differences(self):
        """Patches differing only in line numbers should match."""
        patch1 = """--- a/file.c
+++ b/file.c
@@ -1,3 +1,4 @@
 same
+change
 same
"""
        patch2 = """--- a/file.c
+++ b/file.c
@@ -10,3 +10,4 @@
 same
+change
 same
"""
        matches, diffs = compare_patch_content(patch1, patch2)
        self.assertTrue(matches)


if __name__ == "__main__":
    unittest.main()
