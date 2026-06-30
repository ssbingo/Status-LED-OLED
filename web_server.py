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
import json
import os
import posixpath
import re
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from base64 import b64decode

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
BIND = os.environ.get("WEB_BIND", "0.0.0.0")
PORT = int(os.environ.get("WEB_PORT", "8080"))
STATE_PATH = os.environ.get("WEB_STATE_PATH", "/run/status-led/state.json")
SECRET_PATH = os.environ.get("WEB_SECRET", "/etc/status-led/web.secret")
REALM = "Status-LED"

# Steuer-Aktionen (Update/Neustart) nur, wenn ausdruecklich freigeschaltet.
# Dann laeuft der Dienst als root (siehe setup-web.sh) und darf genau diese
# fest verdrahteten Befehle ausfuehren - nichts aus der Anfrage wird interpoliert.
CONTROL = os.environ.get("WEB_CONTROL", "0") == "1"
ACTIONS = {
    "update":             ["systemctl", "start", "--no-block", "status-led-update.service"],
    "restart:status-led": ["systemctl", "restart", "--no-block", "status-led.service"],
    "restart:web":        ["systemctl", "restart", "--no-block", "status-led-web.service"],
    "restart:backup":     ["systemctl", "restart", "--no-block", "status-led-backup.timer"],
}

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
    ".ico": "image/x-icon",
    ".png": "image/png",
}


def _run(args: list[str], timeout: int = 6) -> str:
    """Liest-only Kommando ausfuehren; bei Fehler leeren String liefern."""
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _show(unit: str, props: list[str]) -> dict:
    """'systemctl show' -> dict der angefragten Properties."""
    out = _run(["systemctl", "show", unit, *[f"-p{p}" for p in props]])
    d: dict[str, str] = {}
    for line in out.splitlines():
        key, _, val = line.partition("=")
        d[key] = val
    return d


def _usec_to_epoch(value: str):
    try:
        n = int(value)
    except (ValueError, TypeError):
        return None
    return n / 1_000_000 if n > 0 else None


def backup_info() -> dict:
    """Backup-Zeitplan, letzter Lauf, Zustand und Log (alles nur lesend)."""
    timer = _show("status-led-backup.timer",
                  ["LoadState", "NextElapseUSecRealtime", "LastTriggerUSec", "TimersCalendar"])
    if timer.get("LoadState") != "loaded":
        return {"configured": False}
    svc = _show("status-led-backup.service", ["ExecMainStartTimestamp", "Result", "ActiveState"])
    sched = ""
    m = re.search(r"OnCalendar=([^;}]+)", timer.get("TimersCalendar", ""))
    if m:
        sched = m.group(1).strip()
    state = None
    try:
        with open("/run/status-led/backup") as f:
            state = f.read().strip()
    except OSError:
        pass
    log = _run(["journalctl", "-u", "status-led-backup", "-n", "60", "--no-pager", "-o", "short-iso"])
    return {
        "configured": True,
        "state": state,
        "schedule": sched,
        "next_run": _usec_to_epoch(timer.get("NextElapseUSecRealtime", "")),
        "last_trigger": _usec_to_epoch(timer.get("LastTriggerUSec", "")),
        "last_start": svc.get("ExecMainStartTimestamp") or None,
        "last_result": svc.get("Result") or None,
        "active": svc.get("ActiveState") or None,
        "log": log.strip(),
    }


def system_info() -> dict:
    """Zustand der Status-LED-Dienste (nur lesend)."""
    units = [
        ("Status-LED", "status-led.service"),
        ("Web-Dashboard", "status-led-web.service"),
        ("Backup-Timer", "status-led-backup.timer"),
    ]
    services = []
    for label, unit in units:
        d = _show(unit, ["LoadState", "ActiveState", "SubState", "ActiveEnterTimestamp"])
        if d.get("LoadState") != "loaded":
            services.append({"name": label, "unit": unit, "active": "not-installed", "sub": "", "since": ""})
        else:
            services.append({"name": label, "unit": unit,
                             "active": d.get("ActiveState", ""), "sub": d.get("SubState", ""),
                             "since": d.get("ActiveEnterTimestamp", "")})
    return {"services": services, "control": CONTROL}


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

    def _send_json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj).encode("utf-8"), CONTENT_TYPES[".json"], no_store=True)

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
        elif path == "/api/backup":
            self._send_json(backup_info())
        elif path == "/api/system":
            self._send_json(system_info())
        else:
            self._serve_static(path)

    do_HEAD = do_GET

    def do_POST(self) -> None:
        if not self._authorized():
            self._require_auth()
            return
        if self.path != "/api/action":
            self._send(404, b"Not Found", "text/plain; charset=utf-8")
            return
        if not CONTROL:
            self._send_json({"ok": False, "error": "Steuerung ist deaktiviert (status-led web-setup)."}, 403)
            return
        # CSRF-Schutz: ein Custom-Header, den nur unsere Seite (fetch) setzen kann.
        if self.headers.get("X-Status-LED-Action") is None:
            self._send_json({"ok": False, "error": "fehlender Header"}, 403)
            return
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
            data = json.loads(self.rfile.read(length) or b"{}") if length else {}
        except (ValueError, OSError):
            data = {}
        cmd = ACTIONS.get(data.get("action", ""))
        if not cmd:
            self._send_json({"ok": False, "error": "unbekannte Aktion"}, 400)
            return
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
            ok = r.returncode == 0
            self._send_json({"ok": ok, "error": "" if ok else (r.stderr.strip() or "Fehler")})
        except (OSError, subprocess.SubprocessError) as e:
            self._send_json({"ok": False, "error": str(e)}, 500)


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
