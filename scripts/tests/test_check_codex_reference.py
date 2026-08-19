"""Codex source reference validator coverage."""

import importlib.util
import sys
from pathlib import Path


def _load_checker():
    path = Path(__file__).parents[1] / "check_codex_reference.py"
    spec = importlib.util.spec_from_file_location("check_codex_reference", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_parse_frontmatter_returns_expected_fields(tmp_path):
    checker = _load_checker()
    reference = tmp_path / "codex-reference.md"
    reference.write_text(
        "\n".join(
            [
                "---",
                "codex_repository: openai/codex",
                "codex_submodule_path: vendor/codex",
                "codex_git_tag: rust-v0.147.0",
                "codex_commit: be6e8eac029b183056b7e4402879f15d2c85f61b",
                "codex_commit_date: 2026-08-06",
                "---",
                "# Codex source reference",
            ]
        ),
        encoding="utf-8",
    )

    assert checker.parse_frontmatter(reference) == {
        "codex_repository": "openai/codex",
        "codex_submodule_path": "vendor/codex",
        "codex_git_tag": "rust-v0.147.0",
        "codex_commit": "be6e8eac029b183056b7e4402879f15d2c85f61b",
        "codex_commit_date": "2026-08-06",
    }


def test_parse_frontmatter_rejects_missing_and_unexpected_fields(tmp_path):
    checker = _load_checker()
    reference = tmp_path / "codex-reference.md"
    reference.write_text(
        "\n".join(
            [
                "---",
                "codex_repository: openai/codex",
                "extra: value",
                "---",
            ]
        ),
        encoding="utf-8",
    )

    try:
        checker.parse_frontmatter(reference)
    except checker.CheckError as exc:
        assert "missing frontmatter field(s)" in str(exc)
    else:
        raise AssertionError("invalid frontmatter was accepted")


def test_compare_reports_commit_and_tag_mismatches():
    checker = _load_checker()
    frontmatter = {
        "codex_repository": "openai/codex",
        "codex_submodule_path": "vendor/codex",
        "codex_git_tag": "rust-v0.147.0",
        "codex_commit": "a" * 40,
        "codex_commit_date": "2026-08-06",
    }
    git_state = checker.GitState(
        gitlink_commit="b" * 40,
        worktree_commit="c" * 40,
        repository="openai/codex",
        tags=("rust-v0.146.0",),
        commit_date="2026-08-07",
        worktree_changes=(),
    )

    errors = checker.compare(frontmatter, git_state)

    assert any("submodule commit differs" in error for error in errors)
    assert any("codex_commit differs" in error for error in errors)
    assert any("codex_git_tag" in error for error in errors)
    assert any("codex_commit_date differs" in error for error in errors)


def test_compare_reports_uncommitted_submodule_changes():
    checker = _load_checker()
    frontmatter = {
        "codex_repository": "openai/codex",
        "codex_submodule_path": "vendor/codex",
        "codex_git_tag": "rust-v0.147.0",
        "codex_commit": "a" * 40,
        "codex_commit_date": "2026-08-06",
    }
    git_state = checker.GitState(
        gitlink_commit="a" * 40,
        worktree_commit="a" * 40,
        repository="openai/codex",
        tags=("rust-v0.147.0",),
        commit_date="2026-08-06",
        worktree_changes=(" M src/lib.rs", "?? scratch.txt"),
    )

    errors = checker.compare(frontmatter, git_state)

    assert errors == [
        "vendor/codex has uncommitted changes:  M src/lib.rs; ?? scratch.txt"
    ]
