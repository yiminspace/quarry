#!/usr/bin/env python3
"""Validate exact versions before building or publishing any release."""
import json, pathlib, re, sys, tomllib
root = pathlib.Path(__file__).resolve().parents[1]
tag = sys.argv[1]
if not re.fullmatch(r"v\d+\.\d+\.\d+(?:-rc\.[1-9]\d*)?", tag):
    raise SystemExit("expected vX.Y.Z or vX.Y.Z-rc.N")
rust = tag[1:]
pep = re.sub(r"-rc\.(\d+)$", r"rc\1", rust)
assert tomllib.loads((root / "Cargo.toml").read_text())["workspace"]["package"]["version"] == rust
assert tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"] == pep
m = json.loads((root / "server.json").read_text())
assert m["version"] == rust and m["packages"][0]["version"] == pep
print(f"Release versions match: {tag} / {pep}")
