#!/usr/bin/env python3
"""Loopback-only hosted UI preview. All API responses are demo fixtures."""

import argparse
import json
import sqlite3
import tempfile
import webbrowser
from contextlib import closing
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ASSETS = Path(__file__).resolve().parents[1] / "src/quarry/web_dist"
SESSION = {
    "access_token": "preview-only", "refresh_token": "preview-only",
    "expires_in": 3600, "user": {"id": "local-preview"},
}
RESOURCES = [
    {"id": f"demo-{db}", "project": "yiminlab", "db": db, "env": "prod",
     "engine": "postgres", "production": True}
    for db in ("auth", "bifrost", "blog_rag")
]


class PreviewHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ASSETS), **kwargs)

    def reply(self, value, status=200, content_type="application/json"):
        data = value if isinstance(value, bytes) else json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path in ("/", "/quarry", "/quarry/", "/quarry/index.html"):
            bootstrap = ('<meta name="quarry-mode" content="platform"><script>'
                         'localStorage.setItem("ylab.console.session.v1",'
                         + json.dumps(json.dumps(SESSION)) + ');'
                         'const key="qy_platform:"+JSON.stringify([location.origin,"local-preview"])+":pairing";'
                         + ('localStorage.removeItem(key);' if self.server.disconnected else
                            'localStorage.setItem(key,JSON.stringify({url:location.origin,token:"preview-local"}));')
                         + '</script>')
            html = (ASSETS / "index.html").read_text().replace("<head>", "<head>" + bootstrap, 1)
            self.reply(html.encode(), content_type="text/html; charset=utf-8")
        elif path.startswith("/api/"):
            self.api(path, {})
        elif path == "/quarry/quarry-offline.js":
            # Preview must always show the latest build, without worker caches.
            self.reply({"error": "Service worker disabled in preview"}, 404)
        elif path.startswith("/quarry/"):
            self.path = self.path[len("/quarry"):]
            super().do_GET()
        else:
            self.reply({"error": "Preview route unavailable"}, 404)

    def do_POST(self):
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if size > 1_000_000:
                self.reply({"error": "Preview request too large"}, 413)
                return
            body = json.loads(self.rfile.read(size) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self.reply({"error": "Invalid JSON"}, 400)
            return
        self.api(urlsplit(self.path).path, body)

    def api(self, path, body):
        if path == "/api/iam/auth/refresh":
            self.reply(SESSION)
            return
        if path in ("/api/platform", "/api/personal", "/api/unpair"):
            if self.headers.get("X-Quarry-Companion-Token") != "preview-local":
                self.reply({"error": "Preview pairing required"}, 401)
                return
            if path == "/api/platform":
                self.reply({"mode": "companion", "principal": SESSION["user"], "resources": []})
            elif path == "/api/unpair":
                self.reply({"revoked": True})
            else:
                from urllib.parse import parse_qs
                params = parse_qs(urlsplit(self.path).query)
                kind = body.get("kind") if self.command == "POST" else params.get("kind", [""])[0]
                key = body.get("key") if self.command == "POST" else params.get("key", [""])[0]
                with closing(sqlite3.connect(self.server.state_path)) as db, db:
                    db.execute("BEGIN IMMEDIATE")
                    stored = db.execute("SELECT value,revision FROM documents WHERE kind=? AND key=?", (kind, key)).fetchone()
                    value, revision = (json.loads(stored[0]), stored[1]) if stored else (None, 0)
                    if self.command == "POST":
                        if body.get("revision") != revision:
                            self.reply({"error": "revision conflict"}, 409)
                            return
                        value, revision = body.get("value"), revision + 1
                        db.execute("INSERT OR REPLACE INTO documents VALUES(?,?,?,?)", (kind, key, json.dumps(value), revision))
                    self.reply({"value": value, "revision": revision})
            return
        path = path.removeprefix("/api/db/")
        if path == "platform":
            value = {"mode": "hosted", "principal": SESSION["user"],
                     "resources": RESOURCES, "capabilities": {"assistant": False},
                     "personalStorage": "companion"}
        elif path == "tables":
            value = {"engine": "postgres", "tables": ["demo_entries"], "capped": False}
        elif path == "columns":
            value = {"columns": ["id", "note"], "types": {"id": "integer", "note": "text"}}
        elif path == "health":
            value = {"ok": True}
        elif path == "query":
            value = {"columns": [{"name": "id", "type": "integer"}, {"name": "note", "type": "text"}],
                     "rows": [{"id": 1, "note": "Demo preview — SQL is not executed"}],
                     "rowCount": 1, "truncated": False, "elapsedMs": 0, "engine": "postgres",
                     "sql": body.get("sql", ""), "downloadBytes": 64, "sizeIsEstimated": True}
        elif path == "cancel":
            value = {"cancelled": True}
        elif path == "assistant/config":
            value = {"configured": False}
        else:
            self.reply({"error": "Demo preview only: this operation needs a real hosted backend or local ylab."}, 501)
            return
        self.reply(value)

    def log_message(self, *_args):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18765)
    parser.add_argument("--no-open", action="store_true")
    parser.add_argument("--disconnected", action="store_true", help="Preview browser-only recovery without a local connection")
    args = parser.parse_args()
    if not (ASSETS / "index.html").exists():
        parser.error("Build web assets first: cd web && npm run build")
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), PreviewHandler)
    except OSError as error:
        parser.exit(1, f"Cannot start preview: {error}\nTry make preview-hosted PREVIEW_ARGS='--port 18767'\n")
    with server, tempfile.TemporaryDirectory(prefix="quarry-preview-") as state_dir:
        server.disconnected = args.disconnected
        server.state_path = str(Path(state_dir) / "personal.sqlite")
        with closing(sqlite3.connect(server.state_path)) as db, db:
            db.execute("CREATE TABLE documents(kind TEXT,key TEXT,value TEXT,revision INTEGER,PRIMARY KEY(kind,key))")
        url = f"http://127.0.0.1:{server.server_port}/quarry/"
        print(f"Hosted GUI preview: {url}\nDemo data only; SQL is not executed. Ctrl+C to stop.\nTemporary personal SQLite: {server.state_path}\nLocal connection: {'disconnected' if args.disconnected else 'connected'}", flush=True)
        if not args.no_open:
            webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nPreview stopped.")


if __name__ == "__main__":
    main()
