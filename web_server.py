#!/usr/bin/env python3
"""web_server.py - schlanker Webserver fuer das Status-LED-Dashboard.

Liefert die statische Oberflaeche (web/) und den Endpoint /api/state, der die
vom Hauptdienst geschriebene Statusdatei (state.json) zurueckgibt. Nutzt nur die
Python-Standardbibliothek. Zugriff per HTTP-Basic-Auth.

Konfiguration ueber Umgebungsvariablen (systemd EnvironmentFile=/etc/status-led/web.env):
  WEB_BIND        Bind-Adresse (Standard 0.0.0.0)
  WEB_PORT        Port (Standard 8080)
  WEB_STATE_PATH  Pfad zur Statusdatei (Standard /run/status-led/state.json)
  WEB_SECRET      Pfad zur Zugangsdatei mit username=/password= (Standard /etc/status-led/web.secret)
"""

from __future__ import annotations

import hmac
import os
import posixpath
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from base64 import b64decode

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
BIND = os.environ.get("WEB_BIND", "0.0.0.0")
PORT = int(os.environ.get("WEB_PORT", "8080"))
STATE_PATH = os.environ.get("WEB_STATE_PATH", "/run/status-led/state.json")
SECRET_PATH = os.environ.get("WEB_SECRET", "/etc/status-led/web.secret")
REALM = "Status-LED"

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
    ".ico": "image/x-icon",
    ".png": "image/png",
}


def load_credentials() -> tuple[str, str] | None:
    """Liest username/password aus der Secret-Datei (KEY=VALUE je Zeile)."""
    try:
        creds: dict[str, str] = {}
        with open(SECRET_PATH) as f:
            for line in f:
                key, _, val = line.partition("=")
                creds[key.strip()] = val.strip()
        user, pw = creds.get("username", ""), creds.get("password", "")
        if user and pw:
            return user, pw
    except OSError:
        pass
    return None


class Handler(BaseHTTPRequestHandler):
    server_version = "status-led-web"

    def log_message(self, *args):           # systemd-Journal nicht zuspammen
        pass

    # --- Authentifizierung ---------------------------------------------------
    def _authorized(self) -> bool:
        creds = load_credentials()
        if creds is None:
            return False
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            decoded = b64decode(header[6:]).decode("utf-8", "replace")
        except ValueError:
            return False
        user, _, pw = decoded.partition(":")
        ok_user = hmac.compare_digest(user, creds[0])
        ok_pw = hmac.compare_digest(pw, creds[1])
        return ok_user and ok_pw

    def _require_auth(self) -> None:
        self.send_response(401)
        self.send_header("WWW-Authenticate", f'Basic realm="{REALM}"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    # --- Antworten -----------------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str, no_store: bool = False) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if no_store:
            self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _serve_state(self) -> None:
        try:
            with open(STATE_PATH, "rb") as f:
                body = f.read()
            self._send(200, body, CONTENT_TYPES[".json"], no_store=True)
        except OSError:
            self._send(503, b'{"error":"keine Statusdaten - laeuft status-led.service?"}',
                       CONTENT_TYPES[".json"], no_store=True)

    def _serve_static(self, url_path: str) -> None:
        rel = url_path.lstrip("/") or "index.html"
        # Pfad-Traversal verhindern: auf WEB_DIR begrenzen
        safe = posixpath.normpath("/" + rel).lstrip("/")
        full = os.path.realpath(os.path.join(WEB_DIR, safe))
        if not (full == WEB_DIR or full.startswith(WEB_DIR + os.sep)) or not os.path.isfile(full):
            self._send(404, b"Not Found", "text/plain; charset=utf-8")
            return
        ext = os.path.splitext(full)[1].lower()
        try:
            with open(full, "rb") as f:
                body = f.read()
        except OSError:
            self._send(404, b"Not Found", "text/plain; charset=utf-8")
            return
        # no_store verhindert, dass der Browser nach einem Update veraltete
        # index.html/app.js/style.css mischt (sonst passen IDs nicht zusammen).
        self._send(200, body, CONTENT_TYPES.get(ext, "application/octet-stream"), no_store=True)

    def do_GET(self) -> None:
        if not self._authorized():
            self._require_auth()
            return
        path = self.path.split("?", 1)[0]
        if path == "/api/state":
            self._serve_state()
        else:
            self._serve_static(path)

    do_HEAD = do_GET


def main() -> None:
    if load_credentials() is None:
        print(f"WARNUNG: keine gueltige Zugangsdatei unter {SECRET_PATH} - "
              f"Zugriff wird abgelehnt. 'status-led web-setup' ausfuehren.")
    httpd = ThreadingHTTPServer((BIND, PORT), Handler)
    print(f"Status-LED Web-Dashboard auf http://{BIND}:{PORT}/ (Daten: {STATE_PATH})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
