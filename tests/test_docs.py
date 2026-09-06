"""The documentation set, kept honest.

This repo previously grew nine markdown files that overlapped, contradicted each
other in places, and went stale in different directions. It is now four, and
this test is what stops it becoming nine again -- plus the usual rot checks: a
link that points at a deleted file, a code fence nobody closed, a diagram block
that was never finished.

Docs are a deliverable here, so they get a test like any other deliverable.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

#: The whole documentation set. Adding a file here should be a decision, not an
#: accident, which is why the test names them rather than globbing.
EXPECTED_DOCS = {
    "README.md",
    "docs/01-functional-spec.md",
    "docs/02-implementation-guide.md",
    "docs/03-architecture.md",
}

#: Markdown that is not documentation and is allowed to exist.
ALLOWED_ELSEWHERE: set[str] = set()

#: Generated or vendored trees. These are gitignored, so they are not part of
#: the repository even when they exist in a working directory.
IGNORED_DIRS = (
    ".venv",
    ".git",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "node_modules",
    "_to_delete",
    "reports",
)


def _markdown_files() -> list[Path]:
    return [p for p in ROOT.rglob("*.md") if not any(part in IGNORED_DIRS for part in p.parts)]


def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


# ---------------------------------------------------------------------------
# the set itself
# ---------------------------------------------------------------------------


def test_the_documentation_set_is_exactly_what_we_decided_on():
    found = {_rel(p) for p in _markdown_files()}
    assert found == EXPECTED_DOCS | ALLOWED_ELSEWHERE, (
        f"unexpected: {sorted(found - EXPECTED_DOCS - ALLOWED_ELSEWHERE)}; "
        f"missing: {sorted((EXPECTED_DOCS | ALLOWED_ELSEWHERE) - found)}"
    )


@pytest.mark.parametrize("rel", sorted(EXPECTED_DOCS))
def test_every_document_exists_and_says_something(rel):
    path = ROOT / rel
    assert path.exists(), rel
    assert len(path.read_text(encoding="utf-8").split()) > 200, f"{rel} is a stub"


def test_the_readme_links_to_every_document():
    """A document nobody can find from the front page may as well not exist."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for rel in sorted(EXPECTED_DOCS - {"README.md"}):
        assert f"({rel})" in readme, f"README does not link to {rel}"


# ---------------------------------------------------------------------------
# rot
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rel", sorted(EXPECTED_DOCS))
def test_code_fences_are_balanced(rel):
    text = (ROOT / rel).read_text(encoding="utf-8")
    assert text.count("```") % 2 == 0, f"{rel} has an unclosed code fence"


@pytest.mark.parametrize("rel", sorted(EXPECTED_DOCS))
def test_relative_links_resolve(rel):
    """Catches the exact failure this consolidation could cause: a link left
    pointing at a file that was folded into another one."""
    path = ROOT / rel
    text = path.read_text(encoding="utf-8")
    broken = []
    for target in re.findall(r"\]\(([^)\s]+)\)", text):
        if target.startswith(("http://", "https://", "#", "mailto:")):
            continue
        resolved = (path.parent / target.split("#")[0]).resolve()
        if not resolved.exists():
            broken.append(target)
    assert broken == [], f"{rel} links to missing files: {broken}"


@pytest.mark.parametrize("rel", sorted(EXPECTED_DOCS))
def test_no_document_references_a_file_that_was_deleted(rel):
    """Backtick-quoted paths rot as silently as links do."""
    text = (ROOT / rel).read_text(encoding="utf-8")
    gone = []
    for candidate in re.findall(r"`([a-zA-Z0-9_./-]+\.(?:py|toml|yaml|yml|md|sh|ps1))`", text):
        if "/" not in candidate and not candidate.startswith("."):
            continue  # a bare filename is prose, not a path
        if candidate.startswith(("http", "sap/", "api/")):
            continue
        if not (ROOT / candidate).exists() and not list(ROOT.rglob(Path(candidate).name)):
            gone.append(candidate)
    assert gone == [], f"{rel} references files that do not exist: {gone}"


# ---------------------------------------------------------------------------
# diagrams
# ---------------------------------------------------------------------------


def test_the_architecture_document_carries_its_diagrams():
    """These render natively on GitHub; there are no image files to go stale."""
    text = (ROOT / "docs/03-architecture.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```mermaid\n(.*?)```", text, re.S)
    assert len(blocks) >= 12, f"only {len(blocks)} diagrams"
    for i, block in enumerate(blocks):
        first = next(line for line in block.splitlines() if line.strip())
        assert first.split()[0] in {
            "flowchart",
            "graph",
            "sequenceDiagram",
            "stateDiagram-v2",
            "pie",
            "erDiagram",
        }, f"diagram {i} starts with {first!r}"


def test_no_document_embeds_a_binary_image():
    """A picture you cannot diff is a picture that goes stale."""
    for rel in sorted(EXPECTED_DOCS):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert not re.search(r"!\[[^\]]*\]\([^)]+\.(png|jpg|jpeg|gif|svg)\)", text), rel


# ---------------------------------------------------------------------------
# the claims the docs make about the repo
# ---------------------------------------------------------------------------


def test_the_documents_name_packages_that_actually_exist():
    packages = {p.name for p in (ROOT / "packages").iterdir() if p.is_dir()}
    for rel in sorted(EXPECTED_DOCS):
        text = (ROOT / rel).read_text(encoding="utf-8")
        for named in re.findall(r"`packages/([a-z_]+)/", text):
            assert named in packages, f"{rel} refers to packages/{named}, which does not exist"


def test_the_readme_and_the_makefile_agree_on_the_commands():
    """A README command that the Makefile does not have is a command nobody has
    run since it was written."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    targets = set(re.findall(r"^([a-z-]+):", makefile, re.M))
    for referenced in set(re.findall(r"\bmake ([a-z-]+)\b", readme)):
        assert referenced in targets, (
            f"README says `make {referenced}`, Makefile has no such target"
        )
