#!/usr/bin/env python3
"""Prepare the MCP manifest only after the exact PyPI release is available."""

from __future__ import annotations

import argparse
import json
import re
import time
import tomllib
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen


SERVER_NAME = "io.github.yiminspace/quarry"


def release_manifest(root: Path, tag: str) -> dict:
    match = re.fullmatch(r"v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", tag)
    if match is None:
        raise ValueError("expected a stable SemVer release tag")
    version = tag[1:]
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    manifest = json.loads((root / "server.json").read_text())
    if project["version"] != version or manifest["version"] != version:
        raise ValueError("release tag, project and server versions must match")
    if manifest["name"] != SERVER_NAME:
        raise ValueError("unexpected MCP server namespace")
    packages = manifest["packages"]
    if len(packages) != 1 or packages[0]["registryType"] != "pypi" or packages[0]["identifier"] != "quarry-db":
        raise ValueError("expected the quarry-db PyPI package")
    # Semantic Release updates the top-level server version. The nested package
    # version must follow the same release, not the version of an earlier tag.
    packages[0]["version"] = version
    return manifest


def verify_pypi(metadata: dict, version: str) -> None:
    info = metadata["info"]
    if info["version"] != version:
        raise ValueError("PyPI returned a different package version")
    marker = rf"mcp-name:\s*{re.escape(SERVER_NAME)}(?=\s|<|-->)"
    if re.search(marker, info.get("description", "")) is None:
        raise ValueError("PyPI release is missing the MCP ownership marker")
    if not metadata.get("urls"):
        raise ValueError("PyPI release has no published distributions")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = release_manifest(Path(__file__).resolve().parents[1], args.tag)
    version = manifest["version"]
    url = f"https://pypi.org/pypi/quarry-db/{version}/json"
    for attempt in range(6):
        try:
            with urlopen(url, timeout=20) as response:
                verify_pypi(json.load(response), version)
            break
        except (HTTPError, URLError, TimeoutError, ValueError) as exc:
            if attempt == 5:
                raise SystemExit(f"PyPI release not ready for MCP publication: {exc}") from exc
            time.sleep(10)
    args.output.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"Verified quarry-db {version}; prepared {SERVER_NAME}")


if __name__ == "__main__":
    main()
