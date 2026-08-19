"""Markdown-link validator coverage."""

import importlib.util
from pathlib import Path


def _load_checker():
    path = Path(__file__).parents[1] / "check_md_links.py"
    spec = importlib.util.spec_from_file_location("check_md_links", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_extract_links_supports_inline_and_reference_destinations(tmp_path):
    checker = _load_checker()
    document = tmp_path / "guide.md"
    document.write_text(
        "\n".join(
            [
                '[inline](guide-(draft).md "Inline title")',
                "[reference link][draft guide]",
                "[unused][unused guide]",
                "[draft guide]: <guide-(draft).md> 'Reference title'",
                "[unused guide]: guide-(draft).md (Reference title)",
            ]
        ),
        encoding="utf-8",
    )

    assert checker.extract_links(document) == [
        ("inline", "guide-(draft).md"),
        ("reference link", "guide-(draft).md"),
        ("unused", "guide-(draft).md"),
    ]


def test_extract_links_ignores_parentheses_in_quoted_inline_titles(tmp_path):
    checker = _load_checker()
    document = tmp_path / "guide.md"
    document.write_text('[guide](guide.md "Title with (")', encoding="utf-8")

    assert checker.extract_links(document) == [("guide", "guide.md")]


def test_check_links_reports_missing_internal_targets_only(tmp_path, monkeypatch):
    checker = _load_checker()
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "guide-(draft).md").write_text("# Guide\n", encoding="utf-8")
    (docs / "index.md").write_text(
        "\n".join(
            [
                '[inline](guide-(draft).md "Guide")',
                "[reference][guide]",
                '[guide]: <guide-(draft).md> "Guide"',
                "[missing reference][missing]",
                "[missing]: missing.md 'Missing'",
                "[external](https://example.com/missing.md)",
                "[anchor](#section)",
                "[outside](../../outside.md)",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(checker, "REPO_ROOT", tmp_path)

    errors = checker.check_links()

    assert len(errors) == 1
    assert "[missing reference](missing.md)" in errors[0]
