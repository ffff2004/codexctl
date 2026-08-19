#!/usr/bin/env python3
"""Validate the Codex source reference against the checked-out submodule."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REFERENCE_FILE = Path("docs/codex-reference.md")
SUBMODULE_PATH = Path("vendor/codex")
FRONTMATTER_FIELDS = frozenset(
    {
        "codex_repository",
        "codex_submodule_path",
        "codex_git_tag",
        "codex_commit",
        "codex_commit_date",
    }
)
FRONTMATTER_FIELD_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):[ \t]*(.+)$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


class CheckError(ValueError):
    """An expected repository or frontmatter invariant is not satisfied."""


@dataclass(frozen=True)
class GitState:
    """Codex metadata read from the superproject and submodule."""

    gitlink_commit: str
    worktree_commit: str
    repository: str
    tags: tuple[str, ...]
    commit_date: str
    worktree_changes: tuple[str, ...]


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def parse_frontmatter(path: Path) -> dict[str, str]:
    """Parse and validate the flat scalar frontmatter used by the reference."""
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] != "---":
        raise CheckError(f"{path}: missing opening frontmatter delimiter")

    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise CheckError(f"{path}: missing closing frontmatter delimiter") from exc

    values: dict[str, str] = {}
    for line_number, line in enumerate(lines[1:end], start=2):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = FRONTMATTER_FIELD_RE.fullmatch(line)
        if match is None:
            raise CheckError(f"{path}:{line_number}: invalid frontmatter field")
        key, value = match.groups()
        if key in values:
            raise CheckError(f"{path}:{line_number}: duplicate field {key!r}")
        values[key] = _unquote(value.strip())

    missing = sorted(FRONTMATTER_FIELDS - values.keys())
    unexpected = sorted(values.keys() - FRONTMATTER_FIELDS)
    if missing:
        raise CheckError(f"{path}: missing frontmatter field(s): {', '.join(missing)}")
    if unexpected:
        raise CheckError(
            f"{path}: unexpected frontmatter field(s): {', '.join(unexpected)}"
        )
    return values


def _git(directory: Path, *arguments: str, clear_index: bool = False) -> str:
    environment = None
    if clear_index:
        environment = os.environ.copy()
        environment.pop("GIT_INDEX_FILE", None)
    result = subprocess.run(
        ["git", *arguments],
        cwd=directory,
        check=False,
        capture_output=True,
        env=environment,
        text=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        command = "git " + " ".join(arguments)
        raise CheckError(f"{directory}: {command} failed: {detail}")
    return result.stdout.strip()


def _read_gitlink(repo_root: Path) -> str:
    entry = _git(repo_root, "ls-tree", "-z", "HEAD", "--", str(SUBMODULE_PATH))
    records = [record for record in entry.split("\0") if record]
    if len(records) != 1:
        raise CheckError(
            f"HEAD:{SUBMODULE_PATH}: expected exactly one gitlink, found {len(records)}"
        )

    metadata, path = records[0].split("\t", maxsplit=1)
    mode, object_type, object_name = metadata.split(" ", maxsplit=2)
    if path != str(SUBMODULE_PATH) or mode != "160000" or object_type != "commit":
        raise CheckError(
            f"HEAD:{SUBMODULE_PATH}: expected a 160000 commit gitlink, found "
            f"{metadata}\t{path}"
        )
    if not COMMIT_RE.fullmatch(object_name):
        raise CheckError(f"HEAD:{SUBMODULE_PATH}: invalid commit {object_name!r}")
    return object_name


def _repository_slug(remote: str) -> str:
    """Normalize common GitHub remote forms to ``owner/repository``."""
    value = remote.strip().removesuffix("/").removesuffix(".git")
    if value.startswith("https://github.com/"):
        return value.removeprefix("https://github.com/")
    if value.startswith("http://github.com/"):
        return value.removeprefix("http://github.com/")
    if value.startswith("git@github.com:"):
        return value.removeprefix("git@github.com:")
    return value


def read_git_state(repo_root: Path) -> GitState:
    """Read the submodule metadata from the superproject and working tree."""
    submodule = repo_root / SUBMODULE_PATH
    if not submodule.is_dir():
        raise CheckError(f"{submodule}: submodule working tree is missing")

    gitlink_commit = _read_gitlink(repo_root)
    worktree_commit = _git(submodule, "rev-parse", "HEAD")
    if not COMMIT_RE.fullmatch(worktree_commit):
        raise CheckError(f"{submodule}: invalid HEAD commit {worktree_commit!r}")

    repository = _repository_slug(_git(submodule, "remote", "get-url", "origin"))
    tags = tuple(
        tag
        for tag in _git(submodule, "tag", "--points-at", worktree_commit).splitlines()
        if tag
    )
    commit_date = _git(submodule, "show", "-s", "--format=%cs", worktree_commit)
    worktree_changes = tuple(
        change
        for change in _git(
            submodule,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            clear_index=True,
        ).splitlines()
        if change
    )
    return GitState(
        gitlink_commit=gitlink_commit,
        worktree_commit=worktree_commit,
        repository=repository,
        tags=tags,
        commit_date=commit_date,
        worktree_changes=worktree_changes,
    )


def compare(frontmatter: dict[str, str], git_state: GitState) -> list[str]:
    """Return all mismatches between the document and Git state."""
    errors: list[str] = []
    if frontmatter["codex_repository"] != git_state.repository:
        errors.append(
            "codex_repository differs: "
            f"frontmatter={frontmatter['codex_repository']!r}, "
            f"vendor/codex origin={git_state.repository!r}"
        )
    if frontmatter["codex_submodule_path"] != str(SUBMODULE_PATH):
        errors.append(
            "codex_submodule_path differs: "
            f"frontmatter={frontmatter['codex_submodule_path']!r}, "
            f"repository={str(SUBMODULE_PATH)!r}"
        )
    if git_state.gitlink_commit != git_state.worktree_commit:
        errors.append(
            "submodule commit differs: "
            f"HEAD:{SUBMODULE_PATH}={git_state.gitlink_commit}, "
            f"vendor/codex HEAD={git_state.worktree_commit}"
        )
    if frontmatter["codex_commit"] != git_state.gitlink_commit:
        errors.append(
            "codex_commit differs: "
            f"frontmatter={frontmatter['codex_commit']}, "
            f"HEAD:{SUBMODULE_PATH}={git_state.gitlink_commit}"
        )
    if frontmatter["codex_commit"] != git_state.worktree_commit:
        errors.append(
            "codex_commit differs: "
            f"frontmatter={frontmatter['codex_commit']}, "
            f"vendor/codex HEAD={git_state.worktree_commit}"
        )
    if frontmatter["codex_git_tag"] not in git_state.tags:
        actual = ", ".join(git_state.tags) or "<none>"
        errors.append(
            "codex_git_tag is not attached to vendor/codex HEAD: "
            f"frontmatter={frontmatter['codex_git_tag']!r}, actual={actual}"
        )
    if frontmatter["codex_commit_date"] != git_state.commit_date:
        errors.append(
            "codex_commit_date differs: "
            f"frontmatter={frontmatter['codex_commit_date']!r}, "
            f"vendor/codex commit date={git_state.commit_date!r}"
        )
    if git_state.worktree_changes:
        errors.append(
            "vendor/codex has uncommitted changes: "
            + "; ".join(git_state.worktree_changes)
        )
    return errors


def check(repo_root: Path) -> list[str]:
    """Validate the reference document and its Codex submodule."""
    reference_file = repo_root / REFERENCE_FILE
    try:
        frontmatter = parse_frontmatter(reference_file)
    except (OSError, CheckError) as exc:
        return [str(exc)]

    try:
        git_state = read_git_state(repo_root)
    except CheckError as exc:
        return [str(exc)]

    return compare(frontmatter, git_state)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="repository root (defaults to the repository containing this script)",
    )
    args = parser.parse_args()

    errors = check(args.repo_root.resolve())
    if errors:
        print("Codex reference check failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    print("Codex reference is consistent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
