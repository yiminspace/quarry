"""Release contracts for the organization MCP Registry entry."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


spec = importlib.util.spec_from_file_location(
    "prepare_mcp_release", Path(__file__).resolve().parents[1] / "scripts/prepare_mcp_release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def test_manifest_pins_nested_package_to_the_release(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "2.0.0"\n')
    manifest = {"name": release.SERVER_NAME, "version": "2.0.0", "packages": [
        {"registryType": "pypi", "identifier": "quarry-db", "version": "1.0.0"}]}
    (tmp_path / "server.json").write_text(json.dumps(manifest))
    assert release.release_manifest(tmp_path, "v2.0.0")["packages"][0]["version"] == "2.0.0"
    with pytest.raises(ValueError, match="versions must match"):
        release.release_manifest(tmp_path, "v2.0.1")
    manifest["name"] = "io.github.someone-else/quarry"
    (tmp_path / "server.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="namespace"):
        release.release_manifest(tmp_path, "v2.0.0")


def test_pypi_requires_exact_version_ownership_and_published_files():
    metadata = {"info": {"version": "2.0.0", "description":
        f"<!-- mcp-name: {release.SERVER_NAME} -->"}, "urls": [{"filename": "quarry.whl"}]}
    release.verify_pypi(metadata, "2.0.0")
    with pytest.raises(ValueError, match="different package version"):
        release.verify_pypi(metadata, "2.0.1")
    metadata["info"]["description"] = f"mcp-name: {release.SERVER_NAME}-other"
    with pytest.raises(ValueError, match="ownership marker"):
        release.verify_pypi(metadata, "2.0.0")
    metadata["info"]["description"] = f"<!-- mcp-name: {release.SERVER_NAME} -->"
    metadata["urls"] = []
    with pytest.raises(ValueError, match="distributions"):
        release.verify_pypi(metadata, "2.0.0")
