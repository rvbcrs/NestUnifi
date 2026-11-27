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
        Worker.seen.clear()

    def request(self, method, path, body=None, **headers):
        conn = http.client.HTTPConnection(self.host, timeout=5)
        self.addCleanup(conn.close)
        conn.request(method, path, body, headers)
        self.answer = conn.getresponse()
        return self.answer.status, self.answer.read()

    def test_the_page_has_no_password_and_sets_no_cookie(self):
        self.assertEqual(self.request("POST", "/api/list", "{}", **self.browser), (200, b'{"linked": false}'))
        self.assertIsNone(self.answer.getheader("Set-Cookie"))
        for gone in ("auth", "claim", "login", "logout"):
            self.assertEqual(self.request("POST", "/api/" + gone, "{}", **self.browser)[0], 404, gone)
        self.assertEqual(Worker.seen, [{"action": "list", "origin": "http://" + self.host}])

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

    def test_camera_still_comes_from_its_private_listener(self):
        class Camera(http.server.BaseHTTPRequestHandler):
            asked = []

            def log_message(self, *args):
                pass

            def do_GET(self):
                self.asked.append(self.path)
                self.send_response(200 if self.path == "/7" else 502)
                self.end_headers()
                self.wfile.write(b"JPEG" if self.path == "/7" else b"")
        camera = socketserver.TCPServer(("127.0.0.1", 0), Camera)
        threading.Thread(target=camera.serve_forever, daemon=True).start()
        self.addCleanup(camera.server_close)
        self.addCleanup(camera.shutdown)
        with patch.object(web, "STILL_SOURCE", "http://127.0.0.1:%d/%%d" % camera.server_address[1]):
            self.assertEqual(self.request("GET", "/snapshot/7.jpg"), (200, b"JPEG"))
            self.assertEqual(self.answer.getheader("Content-Type"), "image/jpeg")
            self.assertEqual(self.request("GET", "/snapshot/7.jpg?1764150000000")[0], 200)
            self.assertEqual(self.request("GET", "/snapshot/8.jpg")[0], 502)
            for outside in ("/snapshot/0.jpg", "/snapshot/255.jpg", "/snapshot/7.jpg?x=1", "/snapshot/1.1.jpg", "/snapshot/7"):
                self.assertEqual(self.request("GET", outside)[0], 404, outside)
        self.assertEqual(Camera.asked, ["/7", "/7", "/8"])

    def test_cast_device_plays_far_enough_behind_the_newest_piece(self):
        piece = lambda n, length: "#EXT-X-PROGRAM-DATE-TIME:2026-11-27T21:42:%02d.000Z\n#EXTINF:%.5f,\nseg%d.ts\n" % (n, length, n)
        playlist = lambda length, count: ("#EXTM3U\n#EXT-X-TARGETDURATION:%d\n" % length + "".join(piece(n, length) for n in range(count))).encode()
        kept = lambda data: [line for line in web.held_back(data).decode().split("\n") if line.endswith(".ts")]
        self.assertEqual(kept(playlist(2, 7)), ["seg0.ts", "seg1.ts", "seg2.ts", "seg3.ts"])
        self.assertEqual(kept(playlist(8, 7)), ["seg%d.ts" % n for n in range(6)])
        self.assertEqual(kept(playlist(2, 2)), ["seg0.ts"])  # A stream that has just started.
        streams = b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nmain_stream.m3u8\n"
        self.assertEqual(web.held_back(streams), streams)

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
            self.assertEqual(self.request("GET", "/live/mix/index.m3u8")[0], 404)  # The camera mix, not running here.
            self.assertEqual(self.request("GET", "/live/mixer/index.m3u8")[0], 404)
            # A cast device reads the same stream at an address with a key. Only there may any website read it.
            self.assertIsNone(self.answer.getheader("Access-Control-Allow-Origin"))
            self.assertEqual(self.request("GET", "/cast/cam-a/%s/index.m3u8" % web.cast_key("cam-a")), (200, b"#EXTM3U\n"))
            self.assertEqual(self.answer.getheader("Access-Control-Allow-Origin"), "*")
            self.assertEqual(self.request("GET", "/cast/cam-a/%s/index.m3u8" % web.cast_key("cam-b"))[0], 404)
            with patch.object(web, "MEDIA_KEY", ""):
                self.assertEqual(self.request("GET", "/cast/cam-a/%s/index.m3u8" % web.cast_key("cam-a"))[0], 404)
        self.assertEqual(Media.asked, [("/cam-a/index.m3u8", "Bearer k3y"), ("/cam-b/index.m3u8", "Bearer k3y"),
                                       ("/mix/index.m3u8", "Bearer k3y"), ("/cam-a/index.m3u8", "Bearer k3y")])


if __name__ == "__main__":
    unittest.main()
