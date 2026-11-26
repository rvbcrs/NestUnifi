"""Camera management for browsers on the local network."""
import ipaddress
import json
import os
import re
import socket
import socketserver
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
ACTIONS = {"list", "discover", "test", "rename", "add", "profile", "password", "remove", "pause", "view", "motiontest", "mix",
           "setup", "client", "project", "consent", "code", "backup", "restore", "notify", "notifytest", "diagnose"}
# Google returns the browser to /oauth2callback with the code in the query. The page itself hands it in.
STATIC = {"/": ("index.html", "text/html; charset=utf-8"), "/oauth2callback": ("index.html", "text/html; charset=utf-8"),
          "/app.css": ("app.css", "text/css"), "/app.js": ("app.js", "text/javascript")}
for asset in (ASSETS / "assets").glob("*.svg"):
    STATIC["/assets/" + asset.name] = ("assets/" + asset.name, "image/svg+xml")
STATIC["/assets/hls.light.min.js"] = ("assets/hls.light.min.js", "text/javascript")
LIVE = re.compile(r"/live/(cam-[a-z0-9-]+|mix)/([A-Za-z0-9_-]+\.(?:m3u8|ts))")
# Every camera has a private listener inside the container, numbered 1 to 254, that gives its latest image.
STILL = re.compile(r"/snapshot/([1-9]\d{0,2})\.jpg(?:\?\d{1,16})?")
STILL_SOURCE = "http://127.0.0.%d/trigger/snapshot"
direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # The media server is in this container.


class Handler(BaseHTTPRequestHandler):
    server_version = "NestUnifi"

    def log_message(self, *args):
        pass

    def reply(self, status, data=b"", mime="application/json"):
        if not isinstance(data, bytes):
            data = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; media-src 'self' blob:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(data)

    def local(self):
        """The page has no password of its own. It answers visitors that are not on the public internet (the home
        network, a VPN, this system itself) and that address the page by IP or by a listed name. Whoever puts it
        behind a reverse proxy puts the sign-in there, for example Cloudflare Access.

        A page elsewhere on the web can point a host name of its own at this address and reach it through the
        visitor's browser. Such a request carries that name, so an unknown name is refused.
        """
        host = urlsplit("//" + self.headers.get("Host", "")).hostname or ""
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if host != "localhost" and host not in NAMES:
                return False
        return not ipaddress.ip_address(self.client_address[0]).is_global

    def refuse(self):
        self.reply(403, "Open NestUnifi vanaf je thuisnetwerk via het IP-adres van dit systeem. Gebruik je een eigen "
                        "hostnaam, zet die dan in NESTUNIFI_HOSTNAMES.".encode(), "text/plain; charset=utf-8")

    def video(self, camera, part):
        request = urllib.request.Request(MEDIA + camera + "/" + part, headers={"Authorization": "Bearer " + MEDIA_KEY})
        try:
            with direct.open(request, timeout=20) as source:
                self.reply(200, source.read(), source.headers.get_content_type())
        except urllib.error.HTTPError as exc:
            exc.close()
            self.reply(exc.code, {})
        except OSError:
            self.reply(502, {})

    def still(self, number):
        try:
            with direct.open(STILL_SOURCE % number, timeout=20) as source:
                self.reply(200, source.read(4 << 20), "image/jpeg")
        except urllib.error.HTTPError as exc:  # The camera has no image now.
            exc.close()
            self.reply(502, {})
        except OSError:  # The camera is not running.
            self.reply(502, {})

    def do_GET(self):
        if not self.local():
            return self.refuse()
        match = LIVE.fullmatch(self.path)
        if match:
            return self.video(match[1], match[2])
        match = STILL.fullmatch(self.path)
        if match and int(match[1]) <= 254:
            return self.still(int(match[1]))
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
        self.reply(200, response["result"])


class Web(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128


if __name__ == "__main__":
    address, port = os.environ["NESTUNIFI_LISTEN"].rsplit(":", 1)
    with Web((address, int(port)), Handler) as server:
        server.serve_forever()
