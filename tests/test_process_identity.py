"""Process identity must be stable across different GUI and CLI locales."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from quarry import tunnel


@pytest.mark.unit
def test_process_identity_is_independent_of_caller_locale(monkeypatch):
    seen_locales = []

    def fake_ps(_args, **kwargs):
        locale = kwargs.get("env", os.environ).get("LC_ALL")
        seen_locales.append(locale)
        identity = "Fri Oct  2 22:24:07 2026 keeper" if locale == "C" else "五 10月/ 2 22:24:07 2026 keeper"
        return SimpleNamespace(returncode=0, stdout=identity)

    monkeypatch.setattr(tunnel.subprocess, "run", fake_ps)
    monkeypatch.setenv("LC_ALL", "zh_CN.UTF-8")
    chinese_gui_identity = tunnel._process_identity(12345)
    monkeypatch.setenv("LC_ALL", "C.UTF-8")
    english_cli_identity = tunnel._process_identity(12345)

    assert chinese_gui_identity == english_cli_identity == "Fri Oct  2 22:24:07 2026 keeper"
    assert seen_locales == ["C", "C"]
