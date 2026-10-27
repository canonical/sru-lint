"""Per-user, validator-addressed package index cache."""

import gzip
import hashlib
import json
import os
import shutil
import tempfile
import urllib.error
import urllib.request
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import IO


def _validator(headers: Mapping[str, str]) -> tuple[str, str] | None:
    for name in ("ETag", "Last-Modified"):
        value = headers.get(name)
        if value:
            return name, value
    return None


class IndexCache:
    def __init__(self, directory: Path | None = None):
        self.directory = (
            directory
            or Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
            / "sru-lint"
            / "madison"
        )

    def clear(self) -> None:
        """Remove published entries, not in-flight downloads."""
        if self.directory.exists():
            for path in self.directory.iterdir():
                if path.is_file() and path.name.endswith(".gz"):
                    path.unlink(missing_ok=True)

    def _target(self, url: str, validator: tuple[str, str]) -> Path:
        key = json.dumps([url, *validator], ensure_ascii=True, separators=(",", ":"))
        return self.directory / f"{hashlib.sha256(key.encode()).hexdigest()}.gz"

    @contextmanager
    def open_index(self, url: str) -> Iterator[IO[bytes] | None]:
        """Open a validated index, or yield None for an unavailable combination.

        Failures never silently fall back to potentially stale results. The open
        descriptor remains usable even if another process clears the cache.
        """
        self.directory.mkdir(parents=True, exist_ok=True)
        try:
            response = urllib.request.urlopen(
                urllib.request.Request(url, method="HEAD"), timeout=30
            )
        except urllib.error.HTTPError as error:
            code = error.code
            error.close()
            if code == 404:
                yield None
                return
            if code not in (405, 501):
                raise
        else:
            with response:
                validator = _validator(response.headers)
            if validator:
                try:
                    cached_index = self._target(url, validator).open("rb")
                except FileNotFoundError:
                    pass  # A concurrent clear can remove the file before open.
                else:
                    with cached_index:
                        yield cached_index
                    return

        try:
            response = urllib.request.urlopen(urllib.request.Request(url), timeout=30)
        except urllib.error.HTTPError as error:
            code = error.code
            error.close()
            if code == 404:
                yield None
                return
            raise

        temporary = None
        try:
            with (
                response,
                tempfile.NamedTemporaryFile(mode="w+b", dir=self.directory, delete=False) as index,
            ):
                temporary = Path(index.name)
                shutil.copyfileobj(response, index)
                validator = _validator(response.headers)
                index.flush()
                index.seek(0)
                # GzipFile accepts an empty input as EOF, but it is not a gzip file.
                if index.read(2) != b"\x1f\x8b":
                    raise gzip.BadGzipFile("Not a gzip file")
                index.seek(0)
                with gzip.GzipFile(fileobj=index, mode="rb") as compressed:
                    while compressed.read(1024 * 1024):
                        pass
                index.seek(0)
                if validator:
                    temporary.replace(self._target(url, validator))
                yield index.file
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
