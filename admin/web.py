"""Camera management for browsers on the local network."""
import ipaddress
import json
import os
import re
import secrets
import socket
import socketserver
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit

ASSETS = Path(__file__).resolve().parent
CONTROL = "/run/control/control.sock"
MEDIA = "http://127.0.0.1:8888/"
MEDIA_KEY = os.environ.get("NESTUNIFI_HLS_KEY", "")
# Host names the owner reaches this page by, for example behind an own reverse proxy.
NAMES = {name.strip().lower() for name in os.environ.get("NESTUNIFI_HOSTNAMES", "").split(",") if name.strip()}
ACTIONS = {"list", "discover", "test", "rename", "add", "profile", "password", "remove", "view",
           "setup", "client", "project", "consent", "code", "backup", "restore", "auth", "claim", "login", "logout"}
# Everything else needs a session. A session starts when the worker accepts the admin password.
OPEN = {"auth", "claim", "login", "logout"}
SESSIONS = {}
LIFETIME = 30 * 24 * 3600
# Google returns the browser to /oauth2callback with the code in the query. The page itself hands it in.
STATIC = {"/": ("index.html", "text/html; charset=utf-8"), "/oauth2callback": ("index.html", "text/html; charset=utf-8"),
          "/app.css": ("app.css", "text/css"), "/app.js": ("app.js", "text/javascript")}
for asset in (ASSETS / "assets").glob("*.svg"):
    STATIC["/assets/" + asset.name] = ("assets/" + asset.name, "image/svg+xml")
STATIC["/assets/hls.light.min.js"] = ("assets/hls.light.min.js", "text/javascript")
LIVE = re.compile(r"/live/(cam-[a-z0-9-]+)/([A-Za-z0-9_-]+\.(?:m3u8|ts))")
direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # The media server is in this container.


class Handler(BaseHTTPRequestHandler):
    server_version = "NestUnifi"

    def log_message(self, *args):
        pass

    def reply(self, status, data=b"", mime="application/json", cookie=None):
        if not isinstance(data, bytes):
            data = json.dumps(data).encode()
        self.send_response(status)
        if cookie is not None:
            self.send_header("Set-Cookie", "nestunifi=" + cookie + "; Path=/; HttpOnly; SameSite=Strict")
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; media-src 'self' blob:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(data)

    def local(self):
        """A first barrier before the login: visitors that are not on the public internet (the home network, a
        VPN, this system itself) and that address the page by IP or by a listed name.

        A page elsewhere on the web can point a host name of its own at this address and reach it through the
        visitor's browser. Such a request carries that name, so an unknown name is refused. Behind a reverse
        proxy every visitor has the proxy's address, so this check is never the only protection.
        """
        host = urlsplit("//" + self.headers.get("Host", "")).hostname or ""
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if host != "localhost" and host not in NAMES:
                return False
        return not ipaddress.ip_address(self.client_address[0]).is_global

    def session(self):
        # Browsers also send the cookies of other apps on this address, whatever their port. Python's cookie
        # parser silently drops everything after a value it cannot read, so only our own pair is looked up.
        token = ""
        for pair in ";".join(self.headers.get_all("Cookie") or []).split(";"):
            name, _, value = pair.strip().partition("=")
            if name == "nestunifi" and value in SESSIONS:
                token = value
                break
        if SESSIONS.get(token, 0) > time.time():
            return token
        SESSIONS.pop(token, None)
        return ""

    def refuse(self):
        self.reply(403, "Open NestUnifi vanaf je thuisnetwerk via het IP-adres van dit systeem. Gebruik je een eigen "
                        "hostnaam, zet die dan in NESTUNIFI_HOSTNAMES.".encode(), "text/plain; charset=utf-8")

    def video(self, camera, part):
        request = urllib.request.Request(MEDIA + camera + "/" + part, headers={"Authorization": "Bearer " + MEDIA_KEY})
        try:
            with direct.open(request, timeout=20) as source:
                self.reply(200, source.read(), source.headers.get_content_type())
        except urllib.error.HTTPError as exc:
            self.reply(exc.code, {})
        except OSError:
            self.reply(502, {})

    def do_GET(self):
        if not self.local():
            return self.refuse()
        match = LIVE.fullmatch(self.path)
        if match:
            if not self.session():
                return self.reply(401, {})
            return self.video(match[1], match[2])
        path = urlsplit(self.path).path
        if path not in STATIC or (path != self.path and path != "/oauth2callback"):
            return self.reply(404, {})
        filename, mime = STATIC[path]
        self.reply(200, (ASSETS/filename).read_bytes(), mime)

    def do_POST(self):
        if not self.local():
            return self.refuse()
        host = self.headers.get("Host", "")
        if (self.headers.get("Origin") not in ("http://"+host, "https://"+host) or
                self.headers.get("X-NestUnifi") != "1" or
                self.headers.get_content_type() != "application/json"):
            return self.reply(403, {"error": "Ongeldige herkomst."})
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= (200 * 1024 if self.path == "/api/restore" else 4096):
                return self.reply(400, {})
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict):
                return self.reply(400, {})
        except (TypeError, ValueError):
            return self.reply(400, {})
        action = self.path.removeprefix("/api/")
        if self.path != "/api/"+action or action not in ACTIONS:
            return self.reply(404, {})
        token = self.session()
        if action == "logout":
            SESSIONS.pop(token, None)
            return self.reply(200, {}, cookie="; Max-Age=0")
        if action not in OPEN and not token:
            return self.reply(401, {"error": "Meld je aan om verder te gaan."})
        try:
            with socket.socket(socket.AF_UNIX) as conn:
                conn.settimeout(200)
                conn.connect(CONTROL)
                conn.sendall(json.dumps(dict(body, action=action, origin=self.headers["Origin"])).encode()+b"\n")
                conn.shutdown(socket.SHUT_WR)
                with conn.makefile("rb") as incoming:
                    response = json.loads(incoming.readline(256*1024))
        except (ValueError, OSError):
            return self.reply(503, {"error": "De beheerservice is niet bereikbaar. Vernieuw het overzicht."})
        if not response["ok"]:
            return self.reply(409, {"error": response["error"]})
        result, cookie = response["result"], None
        if action in ("claim", "login"):
            if action == "claim":
                SESSIONS.clear()  # A new password was chosen. Sessions from the previous one end here.
            token = secrets.token_urlsafe(32)
            SESSIONS[token] = time.time() + LIFETIME
            cookie = token + "; Max-Age=%d" % LIFETIME + ("; Secure" if self.headers["Origin"].startswith("https://") else "")
        if action == "auth":
            result = dict(result, signed_in=bool(token))
        self.reply(200, result, cookie=cookie)


class Web(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128


if __name__ == "__main__":
    address, port = os.environ["NESTUNIFI_LISTEN"].rsplit(":", 1)
    with Web((address, int(port)), Handler) as server:
        server.serve_forever()
