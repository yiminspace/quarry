"""Coverage decisions from real Git snapshots without an Actions runner."""
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from scripts import ci_browser_scope as scope


class BrowserScopeTest(TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.git("init", "-b", "main")
        self.manifest = '[workspace.package]\nversion = "2.0.1"\nedition = "2024"\n'
        self.lock = 'version = 4\n' + ''.join(
            f'\n[[package]]\nname = "{name}"\nversion = "2.0.1"\n'
            for name in sorted(scope.WORKSPACE_PACKAGES)
        ) + '\n[[package]]\nname = "dependency"\nversion = "2.0.1"\nsource = "registry+test"\n'
        self.changelog = '# Changelog\n\n## [Unreleased]\n\n- Existing notes.\n\n## [Unreleased]\n\n- Historical entry.\n'
        self.write("Cargo.toml", self.manifest)
        self.write("Cargo.lock", self.lock)
        self.write("CHANGELOG.md", self.changelog)
        self.write("README.md", "Original docs.\n")
        self.write("apps/qy/src/example.rs", "fn example() {}\n")
        self.base = self.commit()
        self.workflow = "yiminspace/quarry/.github/workflows/ci.yml@refs/heads/main"
        self.green = lambda repository, base: (repository, base) == ("yiminspace/quarry", self.base)

    def git(self, *args):
        return subprocess.check_output(
            ["git", "-c", "user.name=CI Fixture", "-c", "user.email=fixture@example.test",
             "-c", "commit.gpgSign=false", *args],
            cwd=self.root, stderr=subprocess.PIPE, text=True,
        ).strip()

    def write(self, path, content):
        path = self.root / path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def commit(self):
        self.git("add", "-A")
        self.git("commit", "--allow-empty", "-m", "fixture")
        return self.git("rev-parse", "HEAD")

    def push(self, head):
        env = {
            "GITHUB_EVENT_NAME": "push", "GITHUB_SHA": head, "GITHUB_REF": "refs/heads/main",
            "GITHUB_REPOSITORY": "yiminspace/quarry", "GITHUB_WORKFLOW_REF": self.workflow,
            "CI_JOB_WORKFLOW_REF": self.workflow,
        }
        return env, {"before": self.base, "after": head, "ref": env["GITHUB_REF"], "forced": False}

    def write_version_stamp(self):
        self.write("Cargo.toml", self.manifest.replace('"2.0.1"', '"2.1.0"'))
        self.write("Cargo.lock", self.lock.replace('version = "2.0.1"', 'version = "2.1.0"', 8))
        self.write("CHANGELOG.md", self.changelog.replace("- Existing", "## [2.1.0] - 2026-11-06\n\n- Existing"))

    def required(self, head=None, predecessor=None):
        env, event = self.push(head or self.commit())
        return scope.browser_scope(self.root, env, event, predecessor or self.green)[0]

    def test_documentation_only_reuses_green_base(self):
        self.write("README.md", "Updated docs.\n")
        self.write("docs/guides/start.md", "A new guide.\n")
        self.assertFalse(self.required())

    def test_version_stamp_reuses_green_base(self):
        self.write_version_stamp()
        self.write("docs/release-notes.md", "Release docs.\n")
        self.assertFalse(self.required())

    def test_embedded_changelog_content_always_requires_browser(self):
        self.write("CHANGELOG.md", self.changelog + "- Added release notes.\n")
        self.assertTrue(self.required())
        self.git("reset", "--hard", self.base)
        self.write_version_stamp()
        with (self.root / "CHANGELOG.md").open("a") as changelog:
            changelog.write("- Also changed content.\n")
        self.assertTrue(self.required())
        self.git("reset", "--hard", self.base)
        self.write_version_stamp()
        self.write("CHANGELOG.md", self.changelog.replace("- Historical", "## [2.1.0] - 2026-11-06\n\n- Historical"))
        self.assertTrue(self.required())

    def test_other_cargo_edits_require_browser(self):
        self.write("Cargo.toml", self.manifest.replace('"2.0.1"', '"2.1.0"'))
        self.assertTrue(self.required())
        self.git("reset", "--hard", self.base)
        self.write_version_stamp()
        self.write("Cargo.lock", (self.root / "Cargo.lock").read_text().replace('source = "registry+test"', 'source = "registry+other"'))
        self.assertTrue(self.required())

    def test_runtime_tests_unknown_paths_and_symlink_require_browser(self):
        for path in ("apps/qy/src/example.rs", "web/src/example.ts", "tests/test_new.py", "docs/new.html"):
            with self.subTest(path=path):
                self.git("reset", "--hard", self.base)
                self.write(path, "changed\n")
                self.assertTrue(self.required())
        self.git("reset", "--hard", self.base)
        (self.root / "README.md").unlink()
        (self.root / "README.md").symlink_to("apps/qy/src/example.rs")
        self.assertTrue(self.required())

    def test_absent_or_unavailable_base_ci_requires_browser(self):
        self.write("README.md", "Changed docs.\n")
        head = self.commit()
        self.assertTrue(self.required(head, lambda *_: False))

        def unavailable(*_):
            raise subprocess.TimeoutExpired("gh", 15)

        self.assertTrue(self.required(head, unavailable))

    def test_only_exact_green_actions_run_counts(self):
        exact = {"headSha": self.base, "status": "completed", "conclusion": "success"}
        with patch.object(scope.subprocess, "check_output", return_value=json.dumps([exact])) as gh:
            self.assertTrue(scope.green_main_predecessor("yiminspace/quarry", self.base))
            self.assertIn(self.base, gh.call_args.args[0])
        for result in ([], [dict(exact, status="queued")], [dict(exact, conclusion="failure")],
                       [dict(exact, headSha="1" * 40)]):
            with self.subTest(result=result), patch.object(scope.subprocess, "check_output", return_value=json.dumps(result)):
                self.assertFalse(scope.green_main_predecessor("yiminspace/quarry", self.base))

    def test_unknown_push_checkout_and_reusable_or_manual_workflow_require_browser(self):
        self.write("README.md", "Changed docs.\n")
        env, event = self.push(self.commit())
        for changed_env, changed_event in (
            (dict(env, GITHUB_EVENT_NAME="workflow_dispatch"), event),
            (dict(env, GITHUB_EVENT_NAME="workflow_call"), event),
            (dict(env, CI_JOB_WORKFLOW_REF="other caller"), event),
            (dict(env, CI_JOB_WORKFLOW_REF=""), event),
            (dict(env, CI_CHECKOUT_REF=self.base), event),
            (dict(env, GITHUB_SHA=self.base), event),
            (env, dict(event, before="0" * 40)),
            (env, dict(event, forced=True)),
            (env, []),
        ):
            with self.subTest(env=changed_env, event=changed_event):
                self.assertTrue(scope.browser_scope(self.root, changed_env, changed_event, self.green)[0])

    def test_pull_request_uses_verified_merge_commit_and_green_main_base(self):
        self.git("checkout", "-b", "feature")
        self.write("README.md", "Changed docs.\n")
        branch_head = self.commit()
        self.git("checkout", "main")
        self.git("merge", "--no-ff", "-m", "synthetic merge", "feature")
        env, _ = self.push(self.git("rev-parse", "HEAD"))
        env.update(GITHUB_EVENT_NAME="pull_request", GITHUB_REF="refs/pull/7/merge")
        event = {"number": 7, "pull_request": {
            "base": {"sha": self.base, "ref": "main"}, "head": {"sha": branch_head},
        }}
        self.assertFalse(scope.browser_scope(self.root, env, event, self.green)[0])
        self.assertTrue(scope.browser_scope(self.root, env, event, lambda *_: False)[0])
        event["pull_request"]["head"]["sha"] = self.base
        self.assertTrue(scope.browser_scope(self.root, env, event, self.green)[0])
