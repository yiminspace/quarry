#!/usr/bin/env python3
"""Dual-theme GUI screenshots for review: serve the built app from this
checkout against a seeded throwaway workspace, click through to a rendered
data grid, and save dark.png + light.png.

Output directory: $RE_SNAPSHOT_DIR (or ./gui-screenshots). Needs the test
Postgres (QUARRY_TEST_DB_URL, default postgresql://localhost:5432/quarry_test),
Playwright + Chromium, and a prior `cd web && npm run build` (the wrapper
scripts/gui-screenshots.sh handles all of that).
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DB_URL = os.environ.get("QUARRY_TEST_DB_URL", "postgresql://localhost:5432/quarry_test")
OUT_DIR = Path(os.environ.get("RE_SNAPSHOT_DIR", REPO / "gui-screenshots"))


def make_workspace(root: Path) -> Path:
    """A small but representative workspace: a plain connection, an env-set
    with a prod member, and a saved query — enough for the sidebar, pills,
    badges and grid to all render."""
    ws = root / "ws"
    ws.mkdir()
    (ws / "connections.toml").write_text(
        f'[testpg]\nurl = "{DB_URL}"\nengine = "postgres"\nenv = "test"\ngroup = "acme"\n'
        f'[shop_dev]\nurl = "{DB_URL}"\nengine = "postgres"\nenv = "dev"\ndb = "shop"\ngroup = "acme"\n'
        f'[shop_prod]\nurl = "{DB_URL}"\nengine = "postgres"\nenv = "prod"\ndb = "shop"\ngroup = "acme"\n',
        encoding="utf-8",
    )
    qdir = ws / "queries"
    qdir.mkdir()
    (qdir / "top-customers.sql").write_text(
        "-- @name: top-customers\n-- @db: testpg\nSELECT * FROM customers ORDER BY id\n",
        encoding="utf-8",
    )
    return ws


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_up(port: int) -> None:
    for _ in range(80):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/version", timeout=1)
            return
        except Exception:
            time.sleep(0.1)
    raise RuntimeError(f"gui server on port {port} did not come up")


def shoot(port: int) -> None:
    from playwright.sync_api import sync_playwright

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1280, "height": 860})
        page = ctx.new_page()
        page.goto(f"http://127.0.0.1:{port}/", wait_until="domcontentloaded")
        page.wait_for_selector('.dbrow[data-db="testpg"]', timeout=20000)
        page.locator('.dbrow[data-db="testpg"]').click()
        page.wait_for_selector('#tbl-panel .tname[data-t="customers"]', timeout=20000)
        page.locator('#tbl-panel .tname[data-t="customers"]').click()
        page.wait_for_selector("#grid table tbody tr", timeout=20000)
        page.wait_for_timeout(400)
        page.screenshot(path=str(OUT_DIR / "dark.png"))  # dark is the default theme
        page.locator("#themeBtn").click()
        page.wait_for_timeout(300)
        page.screenshot(path=str(OUT_DIR / "light.png"))
        ctx.close()
        browser.close()


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="qy-shot-") as tmp:
        ws = make_workspace(Path(tmp))
        port = free_port()
        # isolate from the host's config.toml-registered workspaces
        env = {
            **os.environ,
            "PYTHONPATH": str(REPO / "src"),
            "QUARRY_CONFIG": str(Path(tmp) / "no-config.toml"),
        }
        code = f"from quarry import gui; gui.serve(port={port}, ws_path={str(ws)!r}, open_browser=False)"
        proc = subprocess.Popen(
            [sys.executable, "-c", code], env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            wait_up(port)
            shoot(port)
        finally:
            proc.terminate()
            proc.wait(timeout=10)
    print(f"screenshots written to {OUT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
