"""Walk a new installation through the wizard with Google replaced by a recorded conversation."""
import base64
import http.server
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import google_api
import worker

ORIGIN = "https://nestunifi.example.com"
CLIENT = "1234567890-abc123.apps.googleusercontent.com"
PROJECT = "1a2b3c4d-1234-5678-9abc-1a2b3c4d5e6f"


class Google:
    """Answers each request the way Google does, including resources that already exist."""
    def __init__(self):
        self.calls, self.made, self.policies = [], set(), {}

    def __call__(self, method, url, token=None, body=None, form=None, timeout=None):
        self.calls.append((method, url))
        if url == google_api.TOKEN:
            return 200, {"access_token": "access", "expires_in": 3599, "refresh_token": "refresh"}
        if url.endswith("services:batchEnable"):
            return 200, {"name": "operations/1", "done": False}
        if url.endswith("operations/1"):
            return 200, {"name": "operations/1", "done": True}
        if url == google_api.PROJECTS + "1234567890":
            return 200, {"projectId": "home-123"}
        if url.endswith(":getIamPolicy"):
            return 200, json.loads(json.dumps(self.policies.get(url.rsplit(":", 1)[0], {})))
        if url.endswith(":setIamPolicy"):
            self.policies[url.rsplit(":", 1)[0]] = body["policy"]
            return 200, body["policy"]
        if url.endswith("/keys"):
            key = {"type": "service_account", "client_email": google_api.NAME + "@home-123.iam.gserviceaccount.com"}
            return 200, {"privateKeyData": base64.b64encode(json.dumps(key).encode()).decode()}
        status = 409 if url in self.made else 200
        self.made.add(url)
        return status, {}


class Wizard(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        here = Path.cwd()
        os.chdir(tmp.name)
        self.addCleanup(os.chdir, here)
        self.google = Google()
        self.devices = [{"device_id": "enterprises/p/devices/a", "type": "CAMERA", "name": "Tuin",
                         "protocols": ["WEB_RTC"]}]
        for target, options in [(worker, {"ready": lambda cam=None: None, "inventory": lambda: self.devices,
                                          "test_stream": lambda req: {"live": False}}),
                                (os, {"fchown": lambda *args: None, "chown": lambda *args: None}),
                                (google_api, {"send": self.google}), (google_api.time, {"sleep": lambda s: None})]:
            mock = patch.multiple(target, **options)
            mock.start()
            self.addCleanup(mock.stop)
        worker.bootstrap()
        self.addCleanup(worker.link_check.update, until=0.0, value=None)

    def ask(self, action, **fields):
        return worker.dispatch(dict(fields, action=action, origin=ORIGIN))

    def consent(self, kind):
        url = self.ask("consent", kind=kind)["url"]
        return url, worker.read(worker.SETUP)["state"]

    def test_new_installation_to_first_camera(self):
        state = self.ask("setup")
        self.assertEqual((state["client"], state["cloud"], state["linked"], state["cameras"]), (False, False, False, 0))
        self.assertEqual((state["register"], state["manual"]), (ORIGIN + "/oauth2callback", False))
        with self.assertRaises(worker.Problem):
            self.ask("client", client_id="home-123", client_secret="GOCSPX-0123456789abcdef")
        self.ask("client", client_id=" " + CLIENT, client_secret="GOCSPX-0123456789abcdef ")

        url, state = self.consent("cloud")
        self.assertTrue(url.startswith("https://accounts.google.com/o/oauth2/v2/auth?"))
        self.assertIn("cloud-platform", url)
        with self.assertRaisesRegex(worker.Problem, "laatste poging"):
            self.ask("code", code="4/abcdefgh", state="forged")
        done = self.ask("code", code="4/abcdefgh", state=state)
        self.assertEqual((done["cloud"], done["topic"]), (True, "projects/home-123/topics/" + google_api.NAME))
        bridge = worker.read(worker.BRIDGE)
        self.assertEqual(bridge["google"]["pubsub_subscription"], "projects/home-123/subscriptions/" + google_api.NAME)
        self.assertTrue(bridge["events"]["onvif"])
        self.assertEqual(worker.read(worker.BRIDGE.parent / worker.KEY_FILE)["type"], "service_account")
        topic = google_api.PUBSUB + "projects/home-123/topics/" + google_api.NAME
        self.assertEqual(self.google.policies[topic]["bindings"],
                         [{"role": "roles/pubsub.publisher", "members": [google_api.PUBLISHER]}])
        with self.assertRaisesRegex(worker.Problem, "verlopen"):
            self.ask("code", code="4/abcdefgh", state=state)

        with self.assertRaises(worker.Problem):
            self.ask("consent", kind="link")
        with self.assertRaisesRegex(worker.Problem, "Google Cloud-project"):
            self.ask("project", project="home-123")
        self.ask("project", project=PROJECT.upper())
        url, state = self.consent("link")
        self.assertTrue(url.startswith("https://nestservices.google.com/partnerconnections/" + PROJECT + "/auth?"))
        self.assertTrue(self.ask("code", code="4/ijklmnop", state=state)["linked"])
        self.assertEqual(worker.read(worker.TOKENS)["refresh_token"], "refresh")

        found = self.ask("discover")["available"]
        self.ask("add", id=found[0]["id"], name="Tuin", password="tuin-camera-1")
        cam = worker.read(worker.ONVIF)["devices"][0]
        path = "cam-" + cam["name"]
        self.assertEqual(cam["streams"][0]["rtsp"], "rtsp://nestunifi:tuin-camera-1@mediamtx:8554/" + path)
        self.assertEqual(cam["info"], {"firmware": "NestUnifi", "manufacturer": "Google", "model": "Nest Cam"})
        bridge, media = worker.read(worker.BRIDGE), worker.read(worker.MEDIA)
        self.assertEqual(bridge["cameras"][0]["event"], {"linger": "60s"})
        publisher, reader = media["authInternalUsers"]
        self.assertIn(":" + publisher["pass"] + "@", bridge["media"]["rtsp_base_url"])
        self.assertEqual(publisher["permissions"], [{"action": "publish", "path": path}])
        self.assertEqual(reader["permissions"], [{"action": "read", "path": path}])
        self.assertEqual(self.ask("setup")["cameras"], 1)

    def test_link_check_follows_google_and_relinking_restarts_the_bridge(self):
        self.assertIsNone(worker.google_link())
        self.ask("client", client_id=CLIENT, client_secret="GOCSPX-0123456789abcdef")
        self.ask("project", project=PROJECT)
        self.ask("code", code="4/ijklmnop", state=self.consent("link")[1])
        previous = worker.google_link()
        self.assertEqual(previous, "ok")
        for answer, verdict in [((400, {"error": "invalid_grant"}), "revoked"), ((401, {"error": "invalid_client"}), "client"),
                                ((503, {}), "unreachable")]:
            with patch.object(google_api, "send", lambda *a, **k: answer):
                self.assertEqual(worker.google_link(), previous)  # Google is asked again only after a while.
                worker.link_check["until"] = 0.0
                previous = worker.google_link()
                self.assertEqual(previous, verdict)
        with patch.object(google_api, "send", side_effect=google_api.Failure("offline")):
            worker.link_check["until"] = 0.0
            self.assertEqual(worker.google_link(), "unreachable")
        # A new link replaces the token the running bridge holds, and the verdict follows at once.
        before = worker.BRIDGE.stat().st_mtime_ns
        self.ask("code", code="4/ijklmnop", state=self.consent("link")[1])
        self.assertNotEqual(worker.BRIDGE.stat().st_mtime_ns, before)
        self.assertEqual(worker.google_link(), "ok")

    def linked_with_camera(self):
        self.ask("client", client_id=CLIENT, client_secret="GOCSPX-0123456789abcdef")
        self.ask("code", code="4/abcdefgh", state=self.consent("cloud")[1])
        self.ask("project", project=PROJECT)
        self.ask("code", code="4/ijklmnop", state=self.consent("link")[1])
        self.ask("add", id=self.ask("discover")["available"][0]["id"], name="Tuin", password="tuin-camera-1")
        worker.replace(worker.ONVIF.parent / "addresses.json", {"02:aa:bb:cc:dd:ee": "192.168.1.40"})

    def fresh_installation(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        os.chdir(tmp.name)
        worker.bootstrap()

    def test_backup_moves_to_a_new_installation(self):
        self.linked_with_camera()
        self.ask("notify", url="https://ntfy.example.com/deur", events=["chime"], health=True)
        worker.replace(worker.AUTH, {"salt": "00", "hash": "geheim"})
        saved = json.loads(json.dumps(self.ask("backup")))
        self.assertNotIn("admin/auth.json", saved["files"])
        old = {name: worker.read(path) for name, (path, _) in worker.BACKUP.items()}
        self.fresh_installation()
        key = worker.read(worker.MEDIA)["hlsCDNSecret"]
        for broken in ({}, {"nestunifi": 1, "files": {"bridge/config.yaml": {}}},
                       {"nestunifi": 1, "files": dict(saved["files"], **{"admin/auth.json": {}})},
                       {"nestunifi": 1, "files": dict(saved["files"], **{"onvif/config.yaml": {"devices": []}})},
                       {"nestunifi": 1, "files": dict(saved["files"], **{"bridge/config.yaml": dict(
                           saved["files"]["bridge/config.yaml"], media={"rtsp_base_url": "rtsp://bridge:x@192.0.2.1:8554"})})}):
            with self.assertRaisesRegex(worker.Problem, "geen back-up|onvolledig"):
                self.ask("restore", backup=broken)
        with self.assertRaisesRegex(worker.Problem, "onvolledig"):
            self.ask("restore", backup={"nestunifi": 1, "files": dict(saved["files"], **{"admin/notify.json": {"url": "file:///etc/passwd"}})})
        self.assertFalse(worker.TOKENS.exists())
        # A hook in the media configuration would run a command. Only NestUnifi's own fields come back.
        media = saved["files"]["media/mediamtx.yml"]
        media.update(runOnInit="touch /tmp/owned", paths={p: {"runOnReady": "sh"} for p in media["paths"]})
        state = self.ask("restore", backup=saved)
        self.assertEqual((state["linked"], state["cameras"], state["cloud"]), (True, 1, True))
        for name, (path, _) in worker.BACKUP.items():
            expected = dict(old[name], hlsCDNSecret=key) if path == worker.MEDIA else old[name]
            self.assertEqual(worker.read(path), expected, name)
        self.assertNotIn("runOnInit", worker.read(worker.MEDIA))
        self.assertFalse(worker.AUTH.exists() or worker.PENDING.exists())
        with self.assertRaisesRegex(worker.Problem, "zonder camera"):
            self.ask("restore", backup=saved)

    def test_failed_restore_leaves_the_new_installation_as_it_was(self):
        self.linked_with_camera()
        saved = self.ask("backup")
        self.fresh_installation()
        before = {path: path.read_text() for path, _ in worker.BACKUP.values() if path.exists()}
        with patch.object(worker, "ready", side_effect=[worker.Problem("start mislukt"), None]):
            with self.assertRaises(worker.Problem):
                self.ask("restore", backup=saved)
        self.assertEqual({path: path.read_text() for path, _ in worker.BACKUP.values() if path.exists()},
                         {path: json.dumps(json.loads(text), ensure_ascii=False, indent=2) for path, text in before.items()})
        self.assertFalse(worker.TOKENS.exists() or worker.PENDING.exists())

    def test_link_of_an_app_in_the_test_status_is_known_to_end(self):
        self.ask("client", client_id=CLIENT, client_secret="GOCSPX-0123456789abcdef")
        self.ask("project", project=PROJECT)
        temporary = {"access_token": "access", "expires_in": 3599, "refresh_token": "refresh", "refresh_token_expires_in": 604799}
        with patch.object(google_api, "send", lambda *a, **k: (200, temporary)):
            self.ask("code", code="4/ijklmnop", state=self.consent("link")[1])
        ends = worker.link_expiry()
        self.assertAlmostEqual(ends, time.time() + 604799, delta=5)
        # Setting up Google Cloud again keeps what is known about the link.
        self.ask("code", code="4/abcdefgh", state=self.consent("cloud")[1])
        self.assertEqual((worker.link_expiry(), self.ask("setup")["topic"]), (ends, "projects/home-123/topics/" + google_api.NAME))
        with patch.multiple(worker, STATE=Path("status.json"), source_live=lambda cam: None, newer_version=lambda: None):
            worker.replace(worker.STATE, {"cameras": {}, "bridge": True, "media": True})
            self.assertEqual(worker.status()["link_expires"], ends)
            self.assertNotIn("expiring", worker.health_problems())
            worker.replace(worker.SETUP, dict(worker.read(worker.SETUP), link_expires=time.time() + 3600))
            self.assertIn("expiring", worker.health_problems())
        # A link made after the app was published lasts.
        self.ask("code", code="4/ijklmnop", state=self.consent("link")[1])
        self.assertIsNone(worker.link_expiry())

    def test_event_subscription_state_comes_from_the_bridge(self):
        self.ask("client", client_id=CLIENT, client_secret="GOCSPX-0123456789abcdef")
        with patch.object(worker, "EVENTS", Path("events.json")):
            worker.replace(worker.EVENTS, {"ok": False, "error": "pubsub: HTTP 403 (PERMISSION_DENIED)"})
            self.assertIsNone(worker.events_state(worker.read(worker.BRIDGE)))  # Events are not switched on yet.
            self.ask("code", code="4/abcdefgh", state=self.consent("cloud")[1])
            bridge = worker.read(worker.BRIDGE)
            for report, verdict in [({"ok": True}, "ok"), ({"ok": False, "error": "pubsub: HTTP 403 (PERMISSION_DENIED)"}, "denied"),
                                    ({"ok": False, "error": "pubsub: HTTP 404 (NOT_FOUND)"}, "missing"),
                                    ({"ok": False, "error": "pubsub: HTTP 503"}, "unreachable"),
                                    ({"ok": False, "error": "call pubsub: Post \"https://pubsub\": dial tcp: no route"}, "unreachable"),
                                    ({"ok": False, "error": "parse service account key: invalid JSON key file"}, "key"),
                                    ({"ok": False, "error": "call pubsub: Post \"x\": oauth2: cannot fetch token: 400"}, "key"),
                                    ([], None)]:
                worker.replace(worker.EVENTS, report)
                self.assertEqual(worker.events_state(bridge), verdict, report)
            worker.EVENTS.unlink()
            self.assertIsNone(worker.events_state(bridge))
            # Google no longer accepts the key: connecting Google Cloud again fetches a new one.
            keys = lambda: sum(url.endswith("/keys") for _, url in self.google.calls)
            made = keys()
            self.ask("code", code="4/abcdefgh", state=self.consent("cloud")[1])
            self.assertEqual(keys(), made)
            worker.replace(worker.EVENTS, {"ok": False, "error": "call pubsub: oauth2: cannot fetch token"})
            self.ask("code", code="4/abcdefgh", state=self.consent("cloud")[1])
            self.assertEqual(keys(), made + 1)

    def test_motion_test_tells_whether_a_recorder_hears_the_camera(self):
        self.linked_with_camera()
        cam = worker.read(worker.ONVIF)["devices"][0]
        camera = {"listeners": 0, "delivered": "", "active": False, "hears": True}
        def call(address, method="GET", path="/trigger/motion"):
            self.assertEqual(address, worker.read(worker.BRIDGE)["cameras"][0]["onvif"]["ip"])
            if method == "GET":
                return 200, json.dumps(camera).encode()
            self.assertEqual(path, "/trigger/motion?state=test")
            if camera["active"]:
                return 409, b""
            if camera["hears"]:
                camera["delivered"] = "2026-11-26T10:00:00Z"
            return 200, b""
        with patch.object(worker, "camera_call", call), patch.object(worker.time, "sleep", lambda s: None):
            with self.assertRaisesRegex(worker.Problem, "Geen recorder luistert"):
                self.ask("motiontest", id=cam["uuid"])
            camera.update(listeners=1, active=True)
            with self.assertRaisesRegex(worker.Problem, "al beweging"):
                self.ask("motiontest", id=cam["uuid"])
            camera.update(active=False)
            self.assertEqual(self.ask("motiontest", id=cam["uuid"]), {"delivered": True})
            camera.update(hears=False)
            self.assertEqual(self.ask("motiontest", id=cam["uuid"]), {"delivered": False})
        with patch.object(worker, "camera_call", lambda *a, **k: (None, b"")):
            with self.assertRaisesRegex(worker.Problem, "niet aan"):
                self.ask("motiontest", id=cam["uuid"])

    def test_webhook_hears_each_new_detection_once(self):
        self.linked_with_camera()
        for bad in ("ftp://example.com/x", "file:///etc/passwd", "http://", "https://example.com/a b", "x" * 600):
            with self.assertRaisesRegex(worker.Problem, "webadres"):
                self.ask("notify", url=bad, events=[], health=False)
        with self.assertRaises(worker.Problem):
            self.ask("notify", url="", events=["earthquake"])
        self.assertEqual(self.ask("notify"), {"url": "", "events": ["chime", "person"], "health": True})
        settings = self.ask("notify", url=" https://ntfy.example.com/deur ", events=["chime"], health=False)
        self.assertEqual(settings, {"url": "https://ntfy.example.com/deur", "events": ["chime"], "health": False})
        self.assertEqual(self.ask("backup")["files"]["admin/notify.json"], settings)
        detected, sent, state = {}, [], {}
        look = lambda: worker.watch_events(state, settings, lambda *message: sent.append(message))
        with patch.object(worker, "motion", lambda address: {"detected": dict(detected)}):
            detected.update(chime="t1", person="t1")
            look()  # What the camera already knew when the watcher started is old.
            self.assertEqual(sent, [])
            detected.update(chime="t2", person="t2")
            look()
            look()
            self.assertEqual(sent, [("chime", "Tuin", "Er wordt aangebeld bij Tuin.", "Tuin")])
            detected.update(chime="t3")
            look()  # Within half a minute of the last message.
            self.assertEqual(len(sent), 1)
            state["quiet"].clear()
            detected.update(chime="t4")
            look()
            self.assertEqual(len(sent), 2)
        with patch.object(worker, "motion", lambda address: None):
            look()  # A camera that does not answer changes nothing.
        self.assertEqual(len(sent), 2)

    def test_webhook_message_and_its_test(self):
        got = []
        class Receiver(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                got.append((self.path, self.headers["Content-Type"], json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                self.send_response(404 if self.path == "/gone" else 200)
                self.end_headers()
            def log_message(self, *args):
                pass
        with http.server.HTTPServer(("127.0.0.1", 0), Receiver) as server:
            threading.Thread(target=server.serve_forever, daemon=True).start()
            base = "http://127.0.0.1:%d" % server.server_port
            with self.assertRaisesRegex(worker.Problem, "Vul eerst"):
                self.ask("notifytest")
            self.ask("notify", url=base + "/hook", events=[], health=True)
            self.ask("notifytest")
            worker.send_notice(base + "/hook", "chime", "Voordeur", "Er wordt aangebeld bij Voordeur.", "Voordeur")
            self.ask("notify", url=base + "/gone", events=[], health=True)
            with self.assertRaisesRegex(worker.Problem, r"niet aangekomen \(HTTP 404\)"):
                self.ask("notifytest")
            server.shutdown()
        (path, kind, test), (_, _, chime) = got[:2]
        self.assertEqual((path, kind, test["event"], test["title"]), ("/hook", "application/json", "test", "NestUnifi"))
        self.assertEqual({k: chime[k] for k in ("title", "message", "text", "content", "event", "camera")},
                         {"title": "Voordeur", "message": "Er wordt aangebeld bij Voordeur.", "event": "chime", "camera": "Voordeur",
                          "text": "Er wordt aangebeld bij Voordeur.", "content": "Er wordt aangebeld bij Voordeur."})
        self.assertRegex(chime["time"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

    def test_problems_are_reported_once_they_last_and_when_they_end(self):
        sent, state = [], {}
        check = lambda problems, now: worker.watch_health(state, problems, lambda *message: sent.append(message), now)
        down = {"camera:1": ("Tuin geeft geen beeld.", "Tuin geeft weer beeld."), "update:2026.12.01": ("Nieuwe versie.", None)}
        check(down, 0)
        check(down, worker.HOLD - 1)
        self.assertEqual(sent, [])
        check(down, worker.HOLD)
        check(down, worker.HOLD + 60)
        self.assertEqual(sent, [("problem", "NestUnifi", "Tuin geeft geen beeld."), ("problem", "NestUnifi", "Nieuwe versie.")])
        check({}, worker.HOLD + 120)
        self.assertEqual(sent[2:], [("resolved", "NestUnifi", "Tuin geeft weer beeld.")])
        check({"camera:1": down["camera:1"]}, 1000)
        check({}, 1060)  # Over before it lasted: never mentioned.
        self.assertEqual(len(sent), 3)

    def test_what_counts_as_a_problem(self):
        self.linked_with_camera()
        cam = worker.read(worker.ONVIF)["devices"][0]
        seen = lambda state: worker.replace(worker.STATE, {
            "cameras": {cam["uuid"]: {"online": True, "ip": "192.168.1.40"}}, "bridge": True, "media": True,
            "streams": {cam["name"]: {"state": state, "since": 1, "drops": 0}} if state else {}})
        with patch.multiple(worker, STATE=Path("status.json"), EVENTS=Path("events.json"), newer_version=lambda: "2026.12.01"):
            for fine in (None, "live", "renewing"):
                seen(fine)
                self.assertEqual(set(worker.health_problems()), {"update:2026.12.01"}, fine)
            seen("backoff")
            worker.replace(worker.EVENTS, {"ok": False, "error": "pubsub: HTTP 403 (PERMISSION_DENIED)"})
            worker.link_check.update(until=time.monotonic() + 60, value="revoked")
            problems = worker.health_problems()
            self.assertEqual(set(problems), {"update:2026.12.01", "camera:" + cam["uuid"], "events", "google"})
            self.assertEqual(problems["camera:" + cam["uuid"]], ("Tuin geeft geen beeld.", "Tuin geeft weer beeld."))

    def test_diagnostics_show_states_and_log_without_secrets(self):
        self.linked_with_camera()
        self.ask("notify", url="https://ntfy.example.com/geheim-onderwerp", events=["chime"], health=True)
        cam, media = worker.read(worker.ONVIF)["devices"][0], worker.read(worker.MEDIA)
        with patch.multiple(worker, STATE=Path("status.json"), LOG=Path("log.txt"), source_live=lambda cam: True,
                            newer_version=lambda: None, motion=lambda address: None):
            worker.replace(worker.STATE, {"cameras": {cam["uuid"]: {"online": True, "ip": "192.168.1.40"}},
                                          "bridge": True, "idle": False, "media": True, "web": True})
            worker.LOG.with_suffix(".old").write_text("oud\n")
            worker.LOG.write_text("publishing to rtsp://bridge:%s@127.0.0.1:8554/cam-x\nreader tuin-camera-1 accepted\n"
                                  "token refresh\nrtsp://other:pass@host/x\n" % media["authInternalUsers"][0]["pass"])
            report = self.ask("diagnose")
        text = json.dumps(report)
        for secret in ("tuin-camera-1", media["authInternalUsers"][0]["pass"], media["hlsCDNSecret"], "GOCSPX-0123456789abcdef",
                       "geheim-onderwerp", "other:pass", '"refresh"', CLIENT):
            self.assertNotIn(secret, text, secret)
        self.assertEqual(report["log"], ["oud", "publishing to rtsp://***@127.0.0.1:8554/cam-x", "reader *** accepted",
                                         "token ***", "rtsp://***@host/x"])  # the stand-in refresh token is "refresh"
        self.assertEqual((report["services"], report["google"], report["setup"]),
                         ({"bridge": True, "idle": False, "media": True, "web": True}, "ok",
                          {"client": True, "cloud": True, "linked": True, "cameras": 1}))
        self.assertEqual(report["notify"], {"on": True, "events": ["chime"], "health": True})
        self.assertEqual((report["cameras"][0]["name"], report["cameras"][0]["ip"], report["cameras"][0]["live"]), ("Tuin", "192.168.1.40", True))
        self.assertFalse({"id", "username", "still"} & report["cameras"][0].keys())

    def test_first_start_is_written_once_and_ready_for_the_media_server(self):
        media, bridge = worker.read(worker.MEDIA), worker.read(worker.BRIDGE)
        self.assertEqual(len(media["hlsCDNSecret"]), 64)
        self.assertEqual(bridge["media"]["rtsp_base_url"], "rtsp://bridge:" + media["authInternalUsers"][0]["pass"] + "@127.0.0.1:8554")
        self.assertEqual([oct(p.parent.stat().st_mode & 0o777) for p in (worker.BRIDGE, worker.MEDIA, worker.ONVIF, worker.SETUP)], ["0o700"] * 4)
        worker.bootstrap()
        self.assertEqual((worker.read(worker.MEDIA), worker.read(worker.BRIDGE)), (media, bridge))
        # An interrupted first start left no bridge file. Both sides get one new secret.
        worker.BRIDGE.unlink()
        worker.bootstrap()
        fresh = worker.read(worker.MEDIA)["authInternalUsers"][0]["pass"]
        self.assertNotEqual(fresh, media["authInternalUsers"][0]["pass"])
        self.assertIn(":" + fresh + "@", worker.read(worker.BRIDGE)["media"]["rtsp_base_url"])

    def test_admin_password_is_chosen_once_and_guessing_is_slowed(self):
        self.addCleanup(worker.attempts.update, failed=0, until=0.0)
        self.assertEqual(self.ask("auth"), {"claimed": False})
        with self.assertRaisesRegex(worker.Problem, "10 tot 128"):
            self.ask("claim", password="te kort")
        with self.assertRaisesRegex(worker.Problem, "nog geen"):
            self.ask("login", password="geheim-wachtwoord")
        self.ask("claim", password="geheim-wachtwoord")
        self.assertEqual(self.ask("auth"), {"claimed": True})
        self.assertNotIn("geheim", worker.AUTH.read_text())
        with self.assertRaisesRegex(worker.Problem, "al een beheerwachtwoord"):
            self.ask("claim", password="ander-wachtwoord")
        self.assertEqual(self.ask("login", password="geheim-wachtwoord"), {})
        for _ in range(5):
            with self.assertRaisesRegex(worker.Problem, "Onjuist"):
                self.ask("login", password="verkeerd-wachtwoord")
        with self.assertRaisesRegex(worker.Problem, "Te veel pogingen"):
            self.ask("login", password="geheim-wachtwoord")
        worker.attempts["until"] = 0.0
        self.assertEqual(self.ask("login", password="geheim-wachtwoord"), {})
        self.assertEqual(worker.attempts["failed"], 0)

    def test_second_cloud_run_reuses_what_exists(self):
        self.ask("client", client_id=CLIENT, client_secret="GOCSPX-0123456789abcdef")
        for _ in range(2):
            self.ask("code", code="4/abcdefgh", state=self.consent("cloud")[1])
        self.assertEqual(sum(url.endswith("/keys") for _, url in self.google.calls), 1)
        self.assertEqual(sum(url.endswith(":setIamPolicy") for _, url in self.google.calls), 2)

    def test_address_without_public_https_needs_pasting(self):
        lan = "http://192.168.1.5:8780"
        self.assertEqual(worker.redirect_for(lan), "https://www.google.com")
        worker.dispatch({"action": "client", "origin": lan, "client_id": CLIENT, "client_secret": "GOCSPX-0123456789abcdef"})
        worker.dispatch({"action": "consent", "origin": lan, "kind": "cloud"})
        state = worker.dispatch({"action": "code", "origin": lan, "code": "4/abcdefgh"})
        self.assertEqual((state["manual"], state["cloud"]), (True, True))

    def test_google_refusal_is_reported_in_its_own_words(self):
        self.ask("client", client_id=CLIENT, client_secret="GOCSPX-0123456789abcdef")
        state = self.consent("cloud")[1]
        refuse = lambda *a, **k: (400, {"error": "invalid_grant", "error_description": "Bad Request"})
        with patch.object(google_api, "send", refuse), self.assertRaisesRegex(worker.Problem, "Bad Request"):
            self.ask("code", code="4/abcdefgh", state=state)
        self.assertFalse(self.ask("setup")["cloud"])


if __name__ == "__main__":
    unittest.main()
