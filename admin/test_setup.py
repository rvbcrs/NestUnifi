"""Walk a new installation through the wizard with Google replaced by a recorded conversation."""
import base64
import json
import os
import tempfile
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

    def __call__(self, method, url, token=None, body=None, form=None):
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
