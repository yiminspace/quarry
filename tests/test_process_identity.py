"""Process identity must be stable across different GUI and CLI locales."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from quarry import tunnel


@pytest.mark.unit
def test_process_identity_is_independent_of_caller_locale_and_timezone(monkeypatch):
    seen_environments = []

    def fake_ps(_args, **kwargs):
        env = kwargs.get("env", os.environ)
        seen_environments.append((env.get("LC_ALL"), env.get("TZ")))
        identity = ("Fri Oct  2 14:24:07 2026 keeper"
                    if env.get("LC_ALL") == "C" and env.get("TZ") == "UTC"
                    else "五 10月/ 2 22:24:07 2026 keeper")
        return SimpleNamespace(returncode=0, stdout=identity)

    monkeypatch.setattr(tunnel.subprocess, "run", fake_ps)
    monkeypatch.setenv("LC_ALL", "zh_CN.UTF-8")
    monkeypatch.setenv("TZ", "Asia/Shanghai")
    chinese_gui_identity = tunnel._process_identity(12345)
    monkeypatch.setenv("LC_ALL", "C.UTF-8")
    monkeypatch.setenv("TZ", "Pacific/Auckland")
    english_cli_identity = tunnel._process_identity(12345)

    assert chinese_gui_identity == english_cli_identity == "v2:Fri Oct  2 14:24:07 2026 keeper"
    assert seen_environments == [("C", "UTC"), ("C", "UTC")]


@pytest.mark.unit
def test_identity_start_epoch_is_locale_independent(monkeypatch):
    monkeypatch.setenv("LC_ALL", "zh_CN.UTF-8")
    identity = "v2:Fri Oct  2 14:24:07 2026 keeper"
    assert tunnel._identity_start_epoch(identity) == datetime(
        2026, 10, 2, 14, 24, 7, tzinfo=timezone.utc).timestamp()
    assert tunnel._identity_start_epoch("Fri Oct  2 14:24:07 2026 keeper") is None


@pytest.mark.unit
def test_legacy_locale_replay_uses_system_timezone(monkeypatch):
    seen_environments = []
    recorded = "五 10月/ 2 22:24:07 2026 keeper"

    def fake_ps(_args, **kwargs):
        env = kwargs["env"]
        seen_environments.append((env.get("LC_ALL"), env.get("TZ")))
        return SimpleNamespace(returncode=0, stdout=recorded)

    monkeypatch.setattr(tunnel.subprocess, "run", fake_ps)
    monkeypatch.setenv("TZ", "UTC")
    assert tunnel._legacy_process_identity(12345, recorded) == recorded
    assert seen_environments == [("zh_CN.UTF-8", None)]
    assert tunnel._legacy_process_identity(12345, "unknown locale keeper") is None
    assert tunnel._legacy_process_identity(12345, "v2:" + recorded) is None
    assert seen_environments == [("zh_CN.UTF-8", None)]


@pytest.mark.integration
def test_real_process_identity_stays_same_across_caller_timezones(monkeypatch):
    monkeypatch.setenv("LC_ALL", "zh_CN.UTF-8")
    monkeypatch.setenv("TZ", "Asia/Shanghai")
    first = tunnel._process_identity(os.getpid())
    monkeypatch.setenv("LC_ALL", "C")
    monkeypatch.setenv("TZ", "UTC")
    second = tunnel._process_identity(os.getpid())
    assert first and first.startswith("v2:")
    assert first == second
