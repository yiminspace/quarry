#!/usr/bin/env python3
"""Decide whether a verified Git change can reuse browser coverage."""
from collections.abc import Callable, Mapping
from datetime import date
import json
import os
from pathlib import Path
import re
import subprocess
import tomllib

ROOT_DOCS = frozenset({
    "AGENTS.md", "ARCHITECTURE.zh-CN.md", "CLAUDE.md", "CODE_OF_CONDUCT.md",
    "COMPATIBILITY.md", "CONTRIBUTING.md", "README.md", "README.zh-CN.md",
    "SECURITY.md", "TESTING.md",
})
CARGO_FILES = frozenset({"Cargo.toml", "Cargo.lock"})
WORKSPACE_PACKAGES = frozenset({
    "quarry-assistant", "quarry-cli", "quarry-core", "quarry-local",
    "quarry-platform", "quarry-runtime", "quarry-server", "quarry-service",
})
CHANGELOG = "CHANGELOG.md"
UNRELEASED = b"\n## [Unreleased]\n\n"


def git(root: Path, *arguments: str) -> bytes:
    return subprocess.check_output(
        ["git", *arguments], cwd=root, stderr=subprocess.PIPE, timeout=15,
    )


def commit_id(value: object) -> bool:
    return isinstance(value, str) and value != "0" * 40 and bool(re.fullmatch(r"[0-9a-f]{40}", value))


def verified_range(
    root: Path, environment: Mapping[str, str], event: object,
) -> tuple[str, str] | None:
    workflow = environment.get("GITHUB_WORKFLOW_REF")
    if (
        not isinstance(event, dict)
        or not workflow
        or workflow != environment.get("CI_JOB_WORKFLOW_REF")
        or environment.get("CI_CHECKOUT_REF")
    ):
        return None

    event_name = environment.get("GITHUB_EVENT_NAME")
    if event_name not in {"push", "pull_request"}:
        return None

    head = environment.get("GITHUB_SHA")
    if not commit_id(head) or git(root, "rev-parse", "HEAD").decode().strip() != head:
        return None

    if event_name == "push":
        if (
            environment.get("GITHUB_REF") != "refs/heads/main"
            or event.get("ref") != "refs/heads/main"
            or event.get("after") != head
            or event.get("forced") is not False
        ):
            return None
        base = event.get("before")
    else:
        pull = event["pull_request"]
        if (
            pull["base"]["ref"] != "main"
            or environment.get("GITHUB_REF") != f"refs/pull/{event['number']}/merge"
        ):
            return None
        base, branch_head = pull["base"]["sha"], pull["head"]["sha"]
        parents = git(root, "rev-list", "--parents", "-n", "1", head).decode().split()
        if parents != [head, base, branch_head]:
            return None

    if not commit_id(base) or base == head:
        return None
    git(root, "merge-base", "--is-ancestor", base, head)
    return base, head


def changed_files(root: Path, base: str, head: str) -> set[str] | None:
    raw = git(root, "diff", "--raw", "-z", "--no-renames", "--no-ext-diff", base, head, "--")
    if not raw or not raw.endswith(b"\0"):
        return None
    entries = raw[:-1].split(b"\0")
    if len(entries) % 2:
        return None
    files = set()
    for metadata, path in zip(entries[::2], entries[1::2]):
        old_mode, new_mode, _, _, status = metadata.split()
        if (
            status not in {b"A", b"D", b"M"}
            or old_mode not in {b":000000", b":100644"}
            or new_mode not in {b"000000", b"100644"}
        ):
            return None
        files.add(path.decode())
    return files


def is_document(path: str) -> bool:
    return path in ROOT_DOCS or (path.startswith("docs/") and path.endswith(".md"))


def version_order(version: str) -> tuple[int, int, int, bool, int]:
    match = re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-rc\.([1-9]\d*))?", version)
    if not match:
        raise ValueError("unsupported version")
    major, minor, patch, candidate = match.groups()
    return int(major), int(minor), int(patch), candidate is None, int(candidate or 0)


def exact_changelog_stamp(before: bytes, after: bytes, version: str) -> bool:
    start, marker, suffix = before.partition(UNRELEASED)
    prefix = start + marker
    if not marker or not (after.startswith(prefix) and after.endswith(suffix)):
        return False
    heading = after[len(prefix):len(after) - len(suffix)]
    match = re.fullmatch(
        rb"## \[" + re.escape(version.encode()) + rb"\] - (\d{4}-\d{2}-\d{2})\n\n",
        heading,
    )
    if not match:
        return False
    date.fromisoformat(match[1].decode())
    return True


def version_only(root: Path, base: str, head: str, stamp_changelog: bool) -> bool:
    def contents(commit: str, path: str) -> bytes:
        return git(root, "show", f"{commit}:{path}")

    old_manifest = contents(base, "Cargo.toml")
    new_manifest = contents(head, "Cargo.toml")
    old_version = tomllib.loads(old_manifest.decode())["workspace"]["package"]["version"]
    new_version = tomllib.loads(new_manifest.decode())["workspace"]["package"]["version"]
    if version_order(new_version) <= version_order(old_version):
        return False
    old_line = f'[workspace.package]\nversion = "{old_version}"\n'.encode()
    new_line = f'[workspace.package]\nversion = "{new_version}"\n'.encode()
    if old_manifest.count(old_line) != 1 or old_manifest.replace(old_line, new_line) != new_manifest:
        return False

    lock = contents(base, "Cargo.lock")
    workspace_packages = [package for package in tomllib.loads(lock.decode())["package"] if "source" not in package]
    if (
        len(workspace_packages) != len(WORKSPACE_PACKAGES)
        or {package["name"] for package in workspace_packages} != WORKSPACE_PACKAGES
        or any(package["version"] != old_version for package in workspace_packages)
    ):
        return False
    for package in workspace_packages:
        stanza = f'\n[[package]]\nname = "{package["name"]}"\nversion = "'.encode()
        old_line, new_line = [stanza + f'{version}"\n'.encode() for version in (old_version, new_version)]
        if lock.count(old_line) != 1:
            return False
        lock = lock.replace(old_line, new_line)
    if lock != contents(head, "Cargo.lock"):
        return False

    return not stamp_changelog or exact_changelog_stamp(
        contents(base, CHANGELOG), contents(head, CHANGELOG), new_version,
    )


def green_main_predecessor(repository: str, commit: str) -> bool:
    result = subprocess.check_output(
        [
            "gh", "run", "list", "--repo", repository, "--workflow", "ci.yml", "--branch", "main",
            "--event", "push", "--commit", commit, "--limit", "1",
            "--json", "headSha,status,conclusion",
        ],
        stderr=subprocess.PIPE,
        timeout=15,
    )
    return json.loads(result) == [{"headSha": commit, "status": "completed", "conclusion": "success"}]


def browser_scope(
    root: Path,
    environment: Mapping[str, str],
    event: object,
    predecessor_green: Callable[[str, str], bool] = green_main_predecessor,
) -> tuple[bool, str]:
    try:
        revisions = verified_range(root, environment, event)
        if revisions is None:
            return True, "no verified direct Git change range"
        base, head = revisions

        files = changed_files(root, base, head)
        if not files:
            return True, "no supported changed files"
        if all(is_document(path) for path in files):
            reason = "documentation-only change"
        elif (
            CARGO_FILES <= files
            and all(is_document(path) for path in files - CARGO_FILES - {CHANGELOG})
            and version_only(root, base, head, CHANGELOG in files)
        ):
            reason = "Cargo version stamp only"
        else:
            return True, "runtime, test, or unrecognized changes"

        # A green exact base carries browser coverage through each eligible change.
        if not predecessor_green(environment["GITHUB_REPOSITORY"], base):
            return True, "main base has no successful CI"
        return False, reason
    except (KeyError, OSError, TypeError, ValueError, subprocess.SubprocessError):
        return True, "change scope or predecessor could not be verified"


def main() -> None:
    try:
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    except (KeyError, OSError, ValueError):
        event = {}
    required, reason = browser_scope(Path.cwd(), os.environ, event)
    print(f"Browser matrix {'required' if required else 'skipped'} ({reason})")
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
        print(f"browser_required={str(required).lower()}", file=output)


if __name__ == "__main__":
    main()
