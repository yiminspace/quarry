import os
from pathlib import Path
import shutil
import tempfile
import tomllib
import unittest

from scripts.prepare_native_release import next_version, update_workspace


class NativeReleaseTest(unittest.TestCase):
    def test_highest_bump_from_squashed_commit_messages(self):
        self.assertIsNone(next_version("v2.3.1", ["ci: validate release", "docs: update installs"]))
        self.assertEqual(next_version("v2.3.1", ["fix(native): handle retries (#1)"]), "2.3.2")
        self.assertEqual(next_version("v2.3.1", ["perf: reduce startup work"]), "2.3.2")
        self.assertEqual(next_version("v2.3.1", ["feat: add refresh (#186)", "fix: retries"]), "2.4.0")
        for breaking in ["feat!: change config", "fix: update config\n\nBREAKING CHANGE: config moved"]:
            with self.assertRaisesRegex(ValueError, "coordinated native major release"):
                next_version("v2.3.1", [breaking, "feat: regular addition"])
        with self.assertRaises(ValueError):
            next_version("v2.4.0-rc.1", ["fix: update config"])

    def test_version_rewrites_workspace_packages_without_changing_dependencies(self):
        root = Path(__file__).resolve().parents[1]
        manifest = tomllib.loads((root / "Cargo.toml").read_text())
        before = tomllib.loads((root / "Cargo.lock").read_text())
        with tempfile.TemporaryDirectory() as tmp:
            fixture = Path(tmp)
            for filename in ["Cargo.toml", "Cargo.lock"]:
                shutil.copyfile(root / filename, fixture / filename)
            for pattern in manifest["workspace"]["members"]:
                for path in root.glob(f"{pattern}/Cargo.toml"):
                    copy = fixture / path.relative_to(root)
                    copy.parent.mkdir(parents=True)
                    shutil.copyfile(path, copy)
            cwd = Path.cwd()
            try:
                os.chdir(fixture)
                update_workspace("2.4.0")
            finally:
                os.chdir(cwd)
            manifest["workspace"]["package"]["version"] = "2.4.0"
            self.assertEqual(tomllib.loads((fixture / "Cargo.toml").read_text()), manifest)
            for package in before["package"]:
                if "source" not in package:
                    package["version"] = "2.4.0"
            self.assertEqual(tomllib.loads((fixture / "Cargo.lock").read_text()), before)
