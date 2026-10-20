"""Unit tests for the packaging config that governs `pip install -e .`.

Regression guard for https://github.com/yiminspace/quarry/issues/42: without
`dev-mode-exact`, hatchling's editable install just appends the source
directory to sys.path. If that directory later disappears (e.g. a git
worktree gets removed), `import quarry.cli` fails with a bare, unhelpful
`ModuleNotFoundError: No module named 'quarry.cli'`. With `dev-mode-exact`,
each package maps to its exact path, so the same situation instead raises a
`FileNotFoundError` naming the missing path — actionable, not a dead end.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_editable_install_uses_exact_dev_mode() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    wheel_config = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert wheel_config.get("dev-mode-exact") is True
