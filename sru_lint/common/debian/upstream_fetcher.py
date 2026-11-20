"""
Utilities for fetching patches from upstream repositories.

Supports fetching patches from GitHub and other git hosting services.
"""

from __future__ import annotations

from urllib.error import URLError
from urllib.request import urlopen

from sru_lint.common.logging import get_logger

logger = get_logger("upstream_fetcher")


def fetch_github_patch(url: str, timeout: int = 10) -> str | None:
    """
    Fetch a patch from GitHub using the .patch endpoint.

    Args:
        url: GitHub URL (can be commit, PR, or regular URL)
        timeout: Request timeout in seconds

    Returns:
        Patch content if successful, None otherwise
    """
    try:
        # Convert GitHub web URLs to raw patch URLs
        # https://github.com/owner/repo/commit/abc123 -> https://github.com/owner/repo/commit/abc123.patch
        # https://github.com/owner/repo/pull/123 -> https://github.com/owner/repo/pull/123.patch
        if "github.com" in url:
            # Remove any trailing .patch if already present
            patch_url = url.rstrip("/")
            if not patch_url.endswith(".patch"):
                patch_url = patch_url + ".patch"

            logger.debug(f"Fetching GitHub patch from: {patch_url}")

            with urlopen(patch_url, timeout=timeout) as response:
                if response.status == 200:
                    content_bytes = response.read()
                    content: str = content_bytes.decode("utf-8")
                    logger.debug(f"Successfully fetched patch ({len(content)} bytes)")
                    return content
                else:
                    logger.warning(f"GitHub returned status {response.status}")
                    return None
    except URLError as e:
        logger.debug(f"Failed to fetch from GitHub: {e}")
        return None
    except Exception as e:
        logger.debug(f"Error fetching patch from {url}: {e}")
        return None

    return None


def fetch_patch_from_upstream(url: str, timeout: int = 10) -> str | None:
    """
    Fetch a patch from an upstream URL.

    Supports GitHub and other common git hosting services.

    Args:
        url: URL to the upstream patch/commit
        timeout: Request timeout in seconds

    Returns:
        Patch content if successful, None otherwise
    """
    if not url:
        return None

    logger.debug(f"Attempting to fetch upstream patch from: {url}")

    # Try GitHub first
    if "github.com" in url:
        patch = fetch_github_patch(url, timeout)
        if patch:
            return patch

    # For other URLs, try appending .patch
    try:
        patch_url = url.rstrip("/")
        if not patch_url.endswith(".patch"):
            patch_url = patch_url + ".patch"

        logger.debug(f"Trying generic patch fetch from: {patch_url}")
        with urlopen(patch_url, timeout=timeout) as response:
            if response.status == 200:
                content_bytes = response.read()
                content: str = content_bytes.decode("utf-8")
                logger.debug(f"Successfully fetched patch ({len(content)} bytes)")
                return content
    except Exception as e:
        logger.debug(f"Failed to fetch from {patch_url}: {e}")

    return None
