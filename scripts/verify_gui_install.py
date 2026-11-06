#!/usr/bin/env python3
"""Verify embedded assets and API behavior in the installed native GUI."""
import json
from pathlib import Path
import re
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

qy = Path(sys.argv[1]).resolve()
with tempfile.TemporaryDirectory() as tmp:
    workspace = Path(tmp)
    config = workspace / "config.toml"
    config.write_text("")
    (workspace / "connections.toml").write_text(
        '[smoke]\nengine="sqlite"\nurl="sqlite:///probe.sqlite3"\n'
    )
    with sqlite3.connect(workspace / "probe.sqlite3") as connection:
        connection.execute("CREATE TABLE marker(value TEXT)")
        connection.execute("INSERT INTO marker VALUES('preserved')")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    base = f"http://127.0.0.1:{port}"
    with (workspace / "gui.log").open("w+b") as log:
        proc = subprocess.Popen(
            [qy, "--config", config, "--workspace", workspace, "gui",
             "--no-browser", "--port", str(port)], cwd=workspace, stdout=log, stderr=log,
        )
        try:
            for _ in range(100):
                assert proc.poll() is None, "GUI exited during startup"
                try:
                    with opener.open(base + "/api/version", timeout=1) as response:
                        version = json.load(response)
                    break
                except OSError:
                    time.sleep(0.1)
            else:
                raise AssertionError("GUI never became ready")

            with opener.open(base + "/quarry/", timeout=5) as response:
                html = response.read().decode()
            assets = set(re.findall(r'(?:src|href)="(/quarry/assets/[^"]+)"', html))
            assert any(asset.endswith(".js") for asset in assets)
            assert any(asset.endswith(".css") for asset in assets)
            for asset in sorted(assets):
                with opener.open(base + asset, timeout=5) as response:
                    assert response.read(), f"Embedded asset is empty: {asset}"

            with opener.open(base + "/api/changelog", timeout=5) as response:
                changelog = json.load(response)
            assert any(release["entries"] for release in changelog)

            request = urllib.request.Request(
                base + "/api/query",
                data=json.dumps({"db": "smoke", "sql": "SELECT value FROM marker"}).encode(),
                headers={"Content-Type": "application/json"}, method="POST",
            )
            with opener.open(request, timeout=5) as response:
                assert json.load(response)["rows"] == [{"value": "preserved"}]

            foreign_host = urllib.request.Request(base + "/api/version", headers={"Host": "evil.invalid"})
            try:
                opener.open(foreign_host, timeout=5)
            except urllib.error.HTTPError as error:
                assert error.code == 403
            else:
                raise AssertionError("foreign Host accepted")
        except Exception:
            log.seek(0)
            print(log.read().decode(errors="replace"), file=sys.stderr)
            raise
        finally:
            if proc.poll() is None:
                proc.send_signal(signal.SIGINT)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                raise

print("Packaged GUI assets, changelog, SQLite API and origin boundary passed:", version)
