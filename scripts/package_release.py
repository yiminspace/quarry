#!/usr/bin/env python3
"""Package a Cargo-built Quarry executable as a native release archive."""
import argparse
import hashlib
from pathlib import Path
import tarfile
import tomllib

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--asset", required=True, choices=[
    "linux-x86_64", "linux-arm64", "macos-x86_64", "macos-arm64",
])
parser.add_argument("--binary", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()

binary = args.binary.resolve(strict=True)
if not binary.is_file() or not binary.stat().st_mode & 0o111:
    parser.error(f"{binary} must be an executable file")

version = tomllib.loads((root / "Cargo.toml").read_text())["workspace"]["package"]["version"]
args.output.mkdir(parents=True, exist_ok=True)
archive = args.output / f"quarry-{version}-{args.asset}.tar.gz"
with tarfile.open(archive, "w:gz") as tar:
    tar.add(binary, arcname="qy")
    tar.add(root / "LICENSE", arcname="LICENSE")
    tar.add(root / "docs/install.md", arcname="INSTALL.md")

with archive.open("rb") as file:
    digest = hashlib.file_digest(file, "sha256").hexdigest()
archive.with_name(archive.name + ".sha256").write_text(f"{digest}  {archive.name}\n")
print(archive)
