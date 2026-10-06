"""Check that contract/ is a byte-for-byte copy of the API's contract.

Reads contract/CONTRACT.json, fetches the tag it names from the repository it
names, and checks that the tag points to the commit it names. It then
compares every file in contract/, other than CONTRACT.json, with the tag's
fixtures/ and expected/, and fails on any file that differs, is missing, or
is extra.

Run it from anywhere with `uv run python scripts/check_contract.py`. It needs
git and network access to the repository.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONTRACT_DIR = ROOT / "contract"
MANIFEST = "CONTRACT.json"
VENDORED = ("fixtures", "expected")


class ContractError(Exception):
    """The manifest cannot be read, or the tag cannot be fetched."""


def _git(*args: str, cwd: Path) -> bytes:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True)
    if result.returncode != 0:
        message = result.stderr.decode(errors="replace").strip()
        raise ContractError(f"git {args[0]} failed: {message}")
    return result.stdout


def read_manifest(contract_dir: Path) -> tuple[str, str, str]:
    """Return the repository, the tag, and the commit that the manifest names."""
    path = contract_dir / MANIFEST
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ContractError(f"cannot read {path}: {error}") from error
    if not isinstance(manifest, dict):
        raise ContractError(f"{path} is not a JSON object")
    fields = []
    for name in ("repository", "tag", "commit"):
        value = manifest.get(name)
        if not isinstance(value, str) or not value:
            raise ContractError(f"{path} has no {name}")
        fields.append(value)
    image = manifest.get("api_image", "")
    if image is not None and (not isinstance(image, str) or not image):
        raise ContractError(f"{path}: api_image must be null or an image name")
    repository, tag, commit = fields
    return repository, tag, commit


def fetch_tag(repository: str, tag: str, into: Path) -> str:
    """Fetch the tag into a new repository and return the commit it points to."""
    ref = f"refs/tags/{tag}"
    _git("init", "--quiet", cwd=into)
    _git(
        "fetch",
        "--quiet",
        "--depth=1",
        "--no-tags",
        repository,
        f"{ref}:{ref}",
        cwd=into,
    )
    return _git("rev-parse", "--verify", f"{ref}^{{commit}}", cwd=into).decode().strip()


def tagged_files(repo: Path, commit: str) -> dict[str, tuple[str, str]]:
    """Map each path under the vendored directories to its object type and ID."""
    listing = _git(
        "ls-tree", "-r", "-z", "--full-tree", commit, "--", *VENDORED, cwd=repo
    )
    files = {}
    for entry in listing.split(b"\0"):
        if entry:
            info, path = entry.split(b"\t", 1)
            _mode, kind, name = info.decode().split()
            files[path.decode()] = (kind, name)
    return files


def local_files(contract_dir: Path) -> set[str]:
    """Return every file in contract_dir other than the manifest."""
    return {
        path.relative_to(contract_dir).as_posix()
        for path in contract_dir.rglob("*")
        if not path.is_dir() and path.relative_to(contract_dir).as_posix() != MANIFEST
    }


def compare(contract_dir: Path, repo: Path, commit: str) -> list[str]:
    """List every file that differs from the commit, is missing, or is extra."""
    tagged = tagged_files(repo, commit)
    local = local_files(contract_dir)
    problems = []
    for path in sorted(tagged.keys() | local):
        shown = f"contract/{path}"
        if path not in local:
            problems.append(f"missing: {shown}")
        elif path not in tagged:
            problems.append(f"extra: {shown}")
        else:
            kind, name = tagged[path]
            if kind != "blob":
                problems.append(f"differs: {shown} is a {kind} in the tag")
            elif (
                _git("cat-file", "blob", name, cwd=repo)
                != (contract_dir / path).read_bytes()
            ):
                problems.append(f"differs: {shown}")
    return problems


def check(contract_dir: Path, repository: str, tag: str, commit: str) -> list[str]:
    """Return every way contract_dir differs from the tag of the repository."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp)
        found = fetch_tag(repository, tag, repo)
        if found != commit:
            return [f"{tag} in {repository} points to {found}, not {commit}"]
        return compare(contract_dir, repo, commit)


def main() -> int:
    try:
        repository, tag, commit = read_manifest(CONTRACT_DIR)
        problems = check(CONTRACT_DIR, repository, tag, commit)
    except ContractError as error:
        print(f"check_contract: {error}", file=sys.stderr)
        return 1
    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        print(f"contract/ does not match {tag} of {repository}", file=sys.stderr)
        return 1
    print(f"contract/ matches {tag} ({commit}) of {repository}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
