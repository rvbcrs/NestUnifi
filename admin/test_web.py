"""The web handler: who may reach it, the Google return address, live video and the origin given to the worker."""
import http.client
import http.server
import json
import socketserver
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import web


class Worker(socketserver.StreamRequestHandler):
    seen = []

    def handle(self):
        # Read to the end. Answering before the handler has finished sending fails on macOS.
        self.seen.append(json.loads(self.rfile.read()))
        self.wfile.write(b'{"ok": true, "result": {"linked": false}}\n')


class Routes(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(tmp.cleanup)
        control = str(Path(tmp.name) / "c.sock")
        self.servers = [socketserver.UnixStreamServer(control, Worker), socketserver.TCPServer(("127.0.0.1", 0), web.Handler)]
        for server in self.servers:
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
        mock = patch.object(web, "CONTROL", control)
        mock.start()
        self.addCleanup(mock.stop)
        self.host = "127.0.0.1:%d" % self.servers[1].server_address[1]
        self.browser = {"Origin": "http://" + self.host, "X-NestUnifi": "1", "Content-Type": "application/json"}
        self.cookie = ""
        self.assertEqual(self.request("POST", "/api/login", '{"password": "geheim-wachtwoord"}', **self.browser)[0], 200)
        self.cookie = self.answer.getheader("Set-Cookie").split(";")[0]
        Worker.seen.clear()

    def request(self, method, path, body=None, **headers):
        conn = http.client.HTTPConnection(self.host, timeout=5)
        self.addCleanup(conn.close)
        conn.request(method, path, body, dict({"Cookie": self.cookie}, **headers))
        self.answer = conn.getresponse()
        return self.answer.status, self.answer.read()

    def test_nothing_but_the_login_works_without_a_session(self):
        rules = self.answer.getheader("Set-Cookie")
        self.assertRegex(rules, r"^nestunifi=[A-Za-z0-9_-]{40,}; Max-Age=\d+; Path=/; HttpOnly; SameSite=Strict$")
        stranger = dict(self.browser, Cookie="nestunifi=" + "a" * 43)
        self.assertEqual(self.request("POST", "/api/list", "{}", **stranger)[0], 401)
        self.assertEqual(self.request("GET", "/live/cam-a/index.m3u8", Cookie="")[0], 401)
        self.assertEqual(self.request("GET", "/", Cookie="")[0], 200)
        self.assertEqual(Worker.seen, [])
        self.assertEqual(self.request("POST", "/api/auth", "{}", **stranger), (200, b'{"linked": false, "signed_in": false}'))
        self.assertEqual(self.request("POST", "/api/auth", "{}", **self.browser), (200, b'{"linked": false, "signed_in": true}'))
        self.assertEqual(self.request("POST", "/api/list", "{}", **self.browser)[0], 200)
        self.assertEqual(self.request("POST", "/api/logout", "{}", **self.browser)[0], 200)
        self.assertIn("Max-Age=0", self.answer.getheader("Set-Cookie"))
        self.assertEqual(self.request("POST", "/api/list", "{}", **self.browser)[0], 401)
        self.assertEqual(self.request("POST", "/api/login", "{}", **dict(self.browser, Origin="https://" + self.host))[0], 200)
        self.assertIn("; Secure;", self.answer.getheader("Set-Cookie"))

    def test_choosing_a_new_password_ends_every_earlier_session(self):
        self.assertEqual(self.request("POST", "/api/list", "{}", **self.browser)[0], 200)
        self.assertEqual(self.request("POST", "/api/claim", "{}", Cookie="", **self.browser)[0], 200)
        fresh = self.answer.getheader("Set-Cookie").split(";")[0]
        self.assertEqual(self.request("POST", "/api/list", "{}", **self.browser)[0], 401)
        self.assertEqual(self.request("POST", "/api/list", "{}", Cookie=fresh, **self.browser)[0], 200)

    def test_session_survives_cookies_of_other_apps_on_the_same_address(self):
        # Browsers send the cookies of every port of an address. Other apps on a NAS set values that
        # Python's cookie parser cannot read, and that parser then drops every cookie after them.
        for others in ('prefs={"theme":"dark"}', "greeting=hello world", "ui[sidebar]=1", 'q="a;b"', "flag", "=x"):
            header = others + "; " + self.cookie + "; last=1"
            self.assertEqual(self.request("POST", "/api/list", "{}", **dict(self.browser, Cookie=header))[0], 200, others)

    def test_google_return_address_serves_the_page(self):
        page = self.request("GET", "/")[1]
        self.assertEqual(self.request("GET", "/oauth2callback?state=s&code=4%2Fabc"), (200, page))
        for path in ("/?code=x", "/app.js?code=x", "/oauth2callback/x"):
            self.assertEqual(self.request("GET", path)[0], 404)

    def test_worker_receives_the_checked_origin(self):
        send = lambda origin: self.request("POST", "/api/code", json.dumps({"code": "c", "origin": "https://evil.example"}),
                                           **{"Origin": origin, "X-NestUnifi": "1", "Content-Type": "application/json"})
        self.assertEqual(send("https://evil.example")[0], 403)
        for scheme in ("http://", "https://"):
            self.assertEqual(send(scheme + self.host)[0], 200)
        self.assertEqual(Worker.seen, [{"code": "c", "origin": scheme + self.host, "action": "code"}
                                       for scheme in ("http://", "https://")])

    def test_only_the_home_network_by_address_or_listed_name(self):
        port = self.host.rsplit(":", 1)[1]
        rebound = {"Host": "attacker.example:" + port, "Origin": "http://attacker.example:" + port,
                   "X-NestUnifi": "1", "Content-Type": "application/json"}
        self.assertEqual(self.request("GET", "/", **rebound)[0], 403)
        self.assertEqual(self.request("GET", "/live/cam-a/index.m3u8", **rebound)[0], 403)
        self.assertEqual(self.request("POST", "/api/list", "{}", **rebound)[0], 403)
        self.assertEqual(Worker.seen, [])
        with patch.object(web, "NAMES", {"attacker.example"}):
            self.assertEqual(self.request("GET", "/", **rebound)[0], 200)
        visitor = lambda peer, host: web.Handler.local(SimpleNamespace(headers={"Host": host}, client_address=(peer, 1)))
        self.assertTrue(visitor("192.168.1.20", "192.168.1.5:8780"))
        self.assertTrue(visitor("172.18.0.1", "localhost:8780"))
        self.assertTrue(visitor("100.101.102.103", "192.168.1.5:8780"))  # a VPN peer
        self.assertFalse(visitor("8.8.8.8", "192.168.1.5:8780"))
        self.assertFalse(visitor("192.168.1.20", "nas.example"))

    def test_live_video_comes_from_the_media_server_with_its_key(self):
        class Media(http.server.BaseHTTPRequestHandler):
            asked = []

            def log_message(self, *args):
                pass

            def do_GET(self):
                self.asked.append((self.path, self.headers.get("Authorization")))
                found = self.path == "/cam-a/index.m3u8"
                self.send_response(200 if found else 404)
                self.send_header("Content-Type", "application/vnd.apple.mpegurl")
                self.end_headers()
                self.wfile.write(b"#EXTM3U\n" if found else b"")
        media = socketserver.TCPServer(("127.0.0.1", 0), Media)
        threading.Thread(target=media.serve_forever, daemon=True).start()
        self.addCleanup(media.server_close)
        self.addCleanup(media.shutdown)
        with patch.multiple(web, MEDIA="http://127.0.0.1:%d/" % media.server_address[1], MEDIA_KEY="k3y"):
            self.assertEqual(self.request("GET", "/live/cam-a/index.m3u8"), (200, b"#EXTM3U\n"))
            self.assertEqual(self.request("GET", "/live/cam-b/index.m3u8")[0], 404)
            self.assertEqual(self.request("GET", "/live/cam-a/../x.m3u8")[0], 404)
        self.assertEqual(Media.asked, [("/cam-a/index.m3u8", "Bearer k3y"), ("/cam-b/index.m3u8", "Bearer k3y")])


if __name__ == "__main__":
    unittest.main()
