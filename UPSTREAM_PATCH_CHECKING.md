# Upstream Patch Comparison Feature

## Overview

This feature adds automatic checking for new quilt patches to compare them against their upstream versions using the DEP-3 "Origin:" tag. It issues warnings when:

1. **PATCH_UPSTREAM_BACKPORT**: Patch is marked as a backport
2. **PATCH_UPSTREAM_DIVERGED**: Patch content differs from upstream (beyond context lines or line numbers)

## How It Works

### DEP-3 Origin Tag Parsing

The feature parses the DEP-3 "Origin:" field to extract upstream references:

```
Origin: backport from https://github.com/owner/repo/commit/abc123def456
Origin: upstream, commit abc123def456abc123def456abc123def456abc123de
Origin: https://github.com/owner/repo/pull/123
```

### Upstream Fetching

Currently supports:
- **GitHub commits**: Automatically converts to `.patch` format
- **GitHub pull requests**: Automatically converts to `.patch` format
- **Other URLs**: Attempts to append `.patch` and fetch

### Patch Content Comparison

Compares patches while ignoring:
- Context lines (unchanged lines in the hunk)
- Line number differences

Detects:
- Meaningful code changes that differ from upstream
- Added or removed change lines

## Error Codes

### PATCH_UPSTREAM_BACKPORT (WARNING)

Issued when patch Origin tag contains "backport":

```
Origin: backport of https://github.com/owner/repo/commit/abc123
```

### PATCH_UPSTREAM_DIVERGED (WARNING)

Issued when patch content differs from upstream:

```
Patch differs from upstream: Patch content differs from upstream (not just line numbers/context)
Lines added locally not in upstream: 2 line(s)
```

## Example

Given a new patch file `debian/patches/0001-fix-bug.patch`:

```
Description: Fix critical bug in parser
Origin: backport from https://github.com/example/project/commit/abc123def456
Bug: https://bugs.launchpad.net/example/+bug/123456
---
 parser.c |  5 +++--
 1 file changed, 3 insertions(+), 2 deletions(-)

--- a/parser.c
+++ b/parser.c
@@ -42,8 +42,8 @@ parse_input(char *input)
         int i;
         char *result;

-        result = malloc(strlen(input));
+        result = malloc(strlen(input) + 1);
         for (i = 0; input[i]; i++)
             result[i] = process_char(input[i]);

         return result;
```

The feature will:
1. Detect the "backport" keyword → Issue `PATCH_UPSTREAM_BACKPORT` warning
2. Fetch the upstream patch from the GitHub URL
3. Compare content with upstream
4. If differences exist, issue `PATCH_UPSTREAM_DIVERGED` warning

## Implementation Details

### Module Structure

- **upstream_patch.py**: Core comparison logic
  - `UpstreamReference`: Parses DEP-3 Origin tags
  - `extract_patch_hunks()`: Extracts diff hunks from patches
  - `compare_patch_content()`: Compares two patches

- **upstream_fetcher.py**: Upstream patch fetching
  - `fetch_github_patch()`: Fetches from GitHub
  - `fetch_patch_from_upstream()`: Generic upstream fetcher

- **patch_format.py**: Enhanced plugin
  - `check_upstream_patch()`: Main upstream check logic
  - `_extract_dep3_field()`: Extracts DEP-3 fields
  - `_compare_with_upstream_patch()`: Orchestrates comparison

## Graceful Degradation

The feature is designed to fail gracefully:
- If upstream fetch fails, no error is raised (just logged)
- If Origin tag is missing or malformed, checks are skipped
- If URL parsing fails, comparison is skipped
- Patches without Origin tags are unaffected

## Testing

Run the test suite:

```bash
poetry run python -m unittest tests.test_upstream_patch -v
```

All 245 existing and new tests pass.
