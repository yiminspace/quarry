#!/usr/bin/env python3
"""Compute the next native version and update the Cargo release metadata."""

import argparse
from pathlib import Path
import re
import subprocess
import tomllib


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True).strip()


def next_version(previous: str, commits: list[str]) -> str | None:
    match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", previous)
    if not match:
        raise ValueError(f"expected a stable release tag, got {previous!r}")
    major, minor, patch = map(int, match.groups())
    level = 0
    for commit in commits:
        subject, _, body = commit.partition("\n")
        header = re.match(r"^([a-z]+)(?:\([^\n]+?\))?(!)?: ", subject)
        if not header:
            continue
        if header[2] or re.search(r"(?m)^BREAKING[ -]CHANGE: ", body):
            level = 3
        elif header[1] == "feat":
            level = max(level, 2)
        elif header[1] in {"fix", "perf"}:
            level = max(level, 1)
    if level == 3:
        return f"{major + 1}.0.0"
    if level == 2:
        return f"{major}.{minor + 1}.0"
    if level == 1:
        return f"{major}.{minor}.{patch + 1}"
    return None


def update_workspace(version: str) -> None:
    manifest = Path("Cargo.toml")
    lockfile = Path("Cargo.lock")
    cargo = manifest.read_text()
    locked = lockfile.read_text()
    workspace = tomllib.loads(cargo)["workspace"]
    old = workspace["package"]["version"]
    # Read the actual workspace member list, so a third-party package with
    # the same version never gets rewritten.
    members = {
        tomllib.loads(member.read_text())["package"]["name"]
        for pattern in workspace["members"]
        for member in Path.cwd().glob(f"{pattern}/Cargo.toml")
    }
    local_versions = {
        entry["name"]: entry["version"]
        for entry in tomllib.loads(locked)["package"]
        if "source" not in entry
    }
    if local_versions != dict.fromkeys(members, old):
        raise ValueError("Cargo.lock workspace packages do not match Cargo.toml")
    cargo, count = re.subn(
        r'(?m)^(\[workspace\.package\]\nversion = )"[^"]+"',
        lambda match: f'{match[1]}"{version}"',
        cargo,
    )
    if count != 1:
        raise ValueError("expected one Cargo workspace version")
    for name in sorted(members):
        locked, count = re.subn(
            rf'(?m)^(\[\[package\]\]\nname = "{re.escape(name)}"\nversion = )"{re.escape(old)}"\n(?!(?:source|checksum) = )',
            lambda match: f'{match[1]}"{version}"\n',
            locked,
        )
        if count != 1:
            raise ValueError(f"expected one Cargo.lock entry for {name}")
    manifest.write_text(cargo)
    lockfile.write_text(locked)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("previous_tag")
    args = parser.parse_args()
    # Automated version commits are tagged off main, so their shared ancestor
    # is the last released product commit.
    base = git("merge-base", args.previous_tag, "HEAD")
    messages = git("log", "--first-parent", "--format=%B%x00", f"{base}..HEAD")
    version = next_version(args.previous_tag, [item.strip() for item in messages.split("\0")])
    if version:
        update_workspace(version)
        print(f"v{version}")


if __name__ == "__main__":
    main()
