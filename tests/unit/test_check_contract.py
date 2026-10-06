"""The contract check, against a local repository instead of GitHub."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from scripts.check_contract import CONTRACT_DIR, ContractError, check, read_manifest

TAG = "contract-v9.9.9"
FILES = {
    "fixtures/revisions.json": b'{"revisions": []}\n',
    "expected/pagination.json": b'{"checks": []}\n',
}


def git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def write(root: Path, files: dict[str, bytes]) -> None:
    for name, data in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


@pytest.fixture(autouse=True)
def _isolated_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the developer's git configuration, such as signing, out of the test."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "test@example.com")


@pytest.fixture
def upstream(tmp_path: Path) -> tuple[str, str]:
    """A repository with a tagged contract: its URL and the tag's commit."""
    repo = tmp_path / "upstream"
    write(repo, {**FILES, "spec/api.md": b"# Not vendored\n"})
    git("init", "--quiet", cwd=repo)
    git("add", ".", cwd=repo)
    git("commit", "--quiet", "--message", "Contract", cwd=repo)
    git("tag", "--annotate", TAG, "--message", "Contract", cwd=repo)
    return repo.as_uri(), git("rev-parse", "HEAD", cwd=repo)


@pytest.fixture
def contract(tmp_path: Path) -> Path:
    contract = tmp_path / "contract"
    write(contract, FILES)
    (contract / "CONTRACT.json").write_text("{}")
    return contract


def test_matching_copy_passes(upstream: tuple[str, str], contract: Path) -> None:
    repository, commit = upstream
    assert check(contract, repository, TAG, commit) == []


def test_changed_byte_fails(upstream: tuple[str, str], contract: Path) -> None:
    repository, commit = upstream
    (contract / "fixtures/revisions.json").write_bytes(b'{"revisions": []}\r\n')
    assert check(contract, repository, TAG, commit) == [
        "differs: contract/fixtures/revisions.json"
    ]


def test_missing_file_fails(upstream: tuple[str, str], contract: Path) -> None:
    repository, commit = upstream
    (contract / "expected/pagination.json").unlink()
    assert check(contract, repository, TAG, commit) == [
        "missing: contract/expected/pagination.json"
    ]


def test_extra_files_fail(upstream: tuple[str, str], contract: Path) -> None:
    repository, commit = upstream
    write(contract, {"expected/new.json": b"{}", "notes.md": b"notes"})
    assert check(contract, repository, TAG, commit) == [
        "extra: contract/expected/new.json",
        "extra: contract/notes.md",
    ]


def test_tag_on_another_commit_fails(upstream: tuple[str, str], contract: Path) -> None:
    repository, commit = upstream
    other = "0" * 40
    assert check(contract, repository, TAG, other) == [
        f"{TAG} in {repository} points to {commit}, not {other}"
    ]


def test_unknown_tag_fails(upstream: tuple[str, str], contract: Path) -> None:
    repository, commit = upstream
    with pytest.raises(ContractError, match="git fetch failed"):
        check(contract, repository, "contract-v0.0.0", commit)


def test_manifest_names_the_pinned_contract() -> None:
    assert read_manifest(CONTRACT_DIR) == (
        "https://github.com/nslaughter/financial-data-api",
        "contract-v0.3.0",
        "ecd6f685b0acb69f5eba1a03041441497a039eed",
    )
    manifest = json.loads((CONTRACT_DIR / "CONTRACT.json").read_text())
    assert manifest["api_image"] is None


@pytest.mark.parametrize(
    "manifest",
    [
        {"tag": TAG, "commit": "c" * 40, "api_image": None},
        {"repository": "r", "commit": "c" * 40, "api_image": None},
        {"repository": "r", "tag": TAG, "commit": "", "api_image": None},
        {"repository": "r", "tag": TAG, "commit": "c" * 40, "api_image": 7},
        {"repository": "r", "tag": TAG, "commit": "c" * 40},
    ],
)
def test_incomplete_manifest_is_refused(tmp_path: Path, manifest: object) -> None:
    (tmp_path / "CONTRACT.json").write_text(json.dumps(manifest))
    with pytest.raises(ContractError):
        read_manifest(tmp_path)
