#!/usr/bin/env python3
"""Check that the release tag matches the Cargo workspace version."""
from pathlib import Path
import re
import sys
import tomllib

root = Path(__file__).resolve().parents[1]
if len(sys.argv) != 2 or not re.fullmatch(r"v\d+\.\d+\.\d+(?:-rc\.[1-9]\d*)?", sys.argv[1]):
    raise SystemExit("expected vX.Y.Z or vX.Y.Z-rc.N")
tag = sys.argv[1]
version = tomllib.loads((root / "Cargo.toml").read_text())["workspace"]["package"]["version"]
if tag != f"v{version}":
    raise SystemExit(f"{tag} does not match the Cargo workspace version {version}")
print(f"Release tag matches the Cargo workspace: {tag}")
