"""Structured package index searching and deterministic aggregation."""

import gzip
import re
from dataclasses import dataclass
from typing import IO

from debian.deb822 import Packages


@dataclass(frozen=True)
class Result:
    package: str
    version: str
    category: str
    suite: str
    architectures: tuple[str, ...]

    def line(self) -> str:
        return " | ".join(
            (self.package, self.version, self.category, self.suite, ", ".join(self.architectures))
        )


def search_index(index: IO[bytes], queries: list[str], regex: bool = False) -> set[tuple[str, str]]:
    patterns = [re.compile(query) for query in queries] if regex else []
    found = set()
    with gzip.open(index, "rt", encoding="utf-8") as stream:
        for paragraph in Packages.iter_paragraphs(stream, use_apt_pkg=False):
            name, version = paragraph.get("Package"), paragraph.get("Version")
            if (
                name
                and version
                and (any(p.search(name) for p in patterns) if regex else name in queries)
            ):
                found.add((name, version))
    return found


def search_source_index(index: IO[bytes], source: str) -> set[tuple[str, str]]:
    """Find binary records built from a source package and their source versions.

    Source defaults to Package and Version when the Source field is absent.
    A version in Source takes precedence over the binary version (e.g. binNMUs).
    """
    found = set()
    with gzip.open(index, "rt", encoding="utf-8") as stream:
        for paragraph in Packages.iter_paragraphs(stream, use_apt_pkg=False):
            name, version = paragraph.get("Package"), paragraph.get("Version")
            if not name or not version:
                continue
            source_field = paragraph.get("Source")
            if source_field:
                match = re.fullmatch(r"([^\s()]+)(?:\s+\(([^()]+)\))?", source_field.strip())
                if not match or match.group(1) != source:
                    continue
                version = match.group(2) or version
            elif name != source:
                continue
            found.add((name, version))
    return found


def aggregate(rows: list[tuple[str, str, str, str, str]]) -> list[Result]:
    grouped: dict[tuple[str, str, str, str], set[str]] = {}
    for package, version, category, suite, architecture in rows:
        grouped.setdefault((package, version, category, suite), set()).add(architecture)
    return [Result(*key, tuple(sorted(arches))) for key, arches in sorted(grouped.items())]
