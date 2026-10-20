"""Hatchling build hook: bundle CHANGELOG.md into standard wheels only.

CHANGELOG.md lives at the repo root, outside packages=["src/quarry"], but the
GUI's `GET /api/changelog` (What's New panel) needs it at runtime — so standard
wheels ship it next to gui.py.

This must NOT apply to editable wheels: a static
`[tool.hatch.build.targets.wheel.force-include]` also lands in the editable
wheel, materializing a real `site-packages/quarry/` directory that contains
only CHANGELOG.md. Python then treats that directory as a namespace package,
which takes precedence over the editable install's redirect to the source
tree, and every submodule import breaks (`ModuleNotFoundError: No module named
'quarry.cli'`). Editable installs read the repo-root CHANGELOG.md directly
instead; see `_changelog_path()` in gui.py.
"""

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class ChangelogBuildHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict) -> None:
        if version == "standard":
            build_data["force_include"]["CHANGELOG.md"] = "quarry/CHANGELOG.md"
