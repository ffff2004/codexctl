#!/usr/bin/env python3
"""Validate that relative links in Markdown files point to existing paths."""

from __future__ import annotations

import re
import sys
from pathlib import Path

LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")
REPO_ROOT = Path(__file__).resolve().parent.parent


def find_md_files() -> list[Path]:
    return sorted(REPO_ROOT.rglob("*.md"))


def extract_links(md_file: Path) -> list[tuple[str, str]]:
    text = md_file.read_text(encoding="utf-8")
    return [(m.group(1), m.group(2)) for m in LINK_RE.finditer(text)]


def is_external(path: str) -> bool:
    return bool(re.match(r"^(https?://|mailto:|#)", path))


def check_links() -> list[str]:
    errors: list[str] = []
    for md_file in find_md_files():
        if ".venv" in md_file.parts or "vendor" in md_file.parts:
            continue
        for text, raw_path in extract_links(md_file):
            if is_external(raw_path):
                continue
            path = raw_path.split("#")[0].split("?")[0]
            if not path:
                continue
            target = (md_file.parent / path).resolve()
            if not target.exists():
                errors.append(
                    f"{md_file.relative_to(REPO_ROOT)}: [{text}]({raw_path}) -> {target}"
                )
    return errors


def main() -> int:
    errors = check_links()
    if errors:
        print(f"Found {len(errors)} broken link(s):")
        for e in errors:
            print(f"  {e}")
        return 1
    print("All relative links are valid.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
