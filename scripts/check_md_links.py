#!/usr/bin/env python3
"""Validate repository-internal Markdown links."""

from __future__ import annotations

import re
import sys
from pathlib import Path

REFERENCE_DEFINITION_RE = re.compile(r"(?m)^[ \t]{0,3}\[([^\]\n]+)\]:[ \t]*(.*)$")
REFERENCE_LINK_RE = re.compile(r"(?<!!)\[([^\]\n]+)\]\[([^\]\n]*)\]")
REPO_ROOT = Path(__file__).resolve().parent.parent


def find_md_files() -> list[Path]:
    return sorted(REPO_ROOT.rglob("*.md"))


def _is_escaped(text: str, position: int) -> bool:
    backslashes = 0
    position -= 1
    while position >= 0 and text[position] == "\\\\":
        backslashes += 1
        position -= 1
    return backslashes % 2 == 1


def _find_unescaped(text: str, character: str, start: int) -> int | None:
    for position in range(start, len(text)):
        if text[position] == character and not _is_escaped(text, position):
            return position
    return None


def _inline_links(text: str) -> list[tuple[str, str]]:
    links: list[tuple[str, str]] = []
    position = 0
    while position < len(text):
        start = text.find("[", position)
        if start == -1:
            break
        label_end = _find_unescaped(text, "]", start + 1)
        if (
            label_end is None
            or label_end + 1 >= len(text)
            or text[label_end + 1] != "("
        ):
            position = start + 1
            continue

        target_end = _inline_link_end(text, label_end + 2)
        if target_end is None:
            position = label_end + 2
            continue

        destination = _destination(text[label_end + 2 : target_end - 1])
        if destination is not None:
            links.append((text[start + 1 : label_end], destination))
        position = target_end
    return links


def _inline_link_end(text: str, start: int) -> int | None:
    """Return the position after an inline link, including an optional title."""
    position = start
    while position < len(text) and text[position].isspace():
        position += 1
    if position >= len(text):
        return None

    if text[position] == "<":
        destination_end = _find_unescaped(text, ">", position + 1)
        if destination_end is None:
            return None
        position = destination_end + 1
    else:
        destination_depth = 0
        while position < len(text):
            character = text[position]
            if not _is_escaped(text, position):
                if character == "(":
                    destination_depth += 1
                elif character == ")":
                    if destination_depth == 0:
                        return position + 1
                    destination_depth -= 1
                elif character.isspace() and destination_depth == 0:
                    break
            position += 1
        if destination_depth or position >= len(text):
            return None

    while position < len(text) and text[position].isspace():
        position += 1
    if position >= len(text):
        return None
    if text[position] == ")":
        return position + 1

    if text[position] in {'"', "'"}:
        title_end = _find_unescaped(text, text[position], position + 1)
        if title_end is None:
            return None
        position = title_end + 1
    elif text[position] == "(":
        title_depth = 1
        position += 1
        while position < len(text) and title_depth:
            if not _is_escaped(text, position):
                if text[position] == "(":
                    title_depth += 1
                elif text[position] == ")":
                    title_depth -= 1
            position += 1
        if title_depth:
            return None
    else:
        return None

    while position < len(text) and text[position].isspace():
        position += 1
    return position + 1 if position < len(text) and text[position] == ")" else None


def _destination(link_contents: str) -> str | None:
    contents = link_contents.strip()
    if not contents:
        return None
    if contents.startswith("<"):
        end = _find_unescaped(contents, ">", 1)
        return contents[1:end] if end is not None else None
    return contents.split(maxsplit=1)[0]


def _reference_id(identifier: str) -> str:
    return " ".join(identifier.casefold().split())


def extract_links(md_file: Path) -> list[tuple[str, str]]:
    text = md_file.read_text(encoding="utf-8")
    definitions = {
        _reference_id(match.group(1)): _destination(match.group(2))
        for match in REFERENCE_DEFINITION_RE.finditer(text)
    }
    links = _inline_links(text)
    referenced: set[str] = set()
    for match in REFERENCE_LINK_RE.finditer(text):
        identifier = _reference_id(match.group(2) or match.group(1))
        destination = definitions.get(identifier)
        if destination is not None:
            links.append((match.group(1), destination))
            referenced.add(identifier)
    for identifier, destination in definitions.items():
        if identifier not in referenced and destination is not None:
            links.append((identifier, destination))
    return links


def is_external(path: str) -> bool:
    return path.startswith(("#", "//")) or bool(
        re.match(r"^[a-z][a-z0-9+.-]*:", path, re.IGNORECASE)
    )


def check_links() -> list[str]:
    errors: list[str] = []
    root = REPO_ROOT.resolve()
    for md_file in find_md_files():
        if ".venv" in md_file.parts or "vendor" in md_file.parts:
            continue
        for text, raw_path in extract_links(md_file):
            path = raw_path.split("#", 1)[0].split("?", 1)[0]
            if not path or is_external(path):
                continue
            target = (md_file.parent / path).resolve()
            try:
                target.relative_to(root)
            except ValueError:
                continue
            if not target.exists():
                errors.append(
                    f"{md_file.relative_to(root)}: [{text}]({raw_path}) -> {target}"
                )
    return errors


def main() -> int:
    errors = check_links()
    if errors:
        print(f"Found {len(errors)} broken link(s):")
        for error in errors:
            print(f"  {error}")
        return 1
    print("All repository-internal links are valid.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
