"""Exercise configuration changes and crash recovery without a running container or Google."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import worker
from suite.runtime import device_config, lan_interface


class CameraChanges(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        here = Path.cwd()
        os.chdir(tmp.name)
        self.addCleanup(os.chdir, here)
        worker.PENDING.parent.mkdir(parents=True)
        old_id = "enterprises/test/devices/old"
        self.onvif = {"devices": [{"name": "Keuken camera", "uuid": "fixed-id", "mac": "02:ab:cd:ef:00:01",
                                   "info": {"model": "Keuken camera"}, "ports": {"soap": 18081, "rtsp": 18554},
                                   "auth": {"username": "test", "password": "fixture"},
                                   "streams": [{"rtsp": "rtsp://test:fixture@mediamtx:8554/cam-keuken-camera",
                                                "width": 1920, "height": 1080, "framerate": 15, "bitrate": 1000}]}]}
        self.bridge = {"cameras": [{"device_id": old_id, "name": "Keuken camera",
                                    "onvif": {"ip": "127.0.0.1"}}]}
        self.media = {"paths": {"cam-keuken-camera": {}}, "authInternalUsers": [
            {"user": "test" if a == "read" else "publisher", "pass": "fixture",
             "permissions": [{"action": a, "path": "cam-keuken-camera"}]} for a in ("read", "publish")]}
        for path, data in [(worker.ONVIF, self.onvif), (worker.BRIDGE, self.bridge), (worker.MEDIA, self.media)]:
            path.parent.mkdir(exist_ok=True, parents=True)
            path.write_text(json.dumps(data))
        self.dev = {"device_id": "enterprises/test/devices/new", "type": "DISPLAY", "name": "Nieuw scherm"}
        self.req = {"id": worker.key(self.dev["device_id"]), "name": "Scherm Max", "password": "new-device-123"}
        for target, options in [(worker, {"ready": lambda cam=None: None, "inventory": lambda: [self.dev],
                                  "test_stream": lambda req: {"live": True, "width": 960, "height": 1280}}),
                                (os, {"fchown": lambda *args: None})]:
            mock = patch.multiple(target, **options)
            mock.start()
            self.addCleanup(mock.stop)

    def test_camera_server_config_uses_onvif_fields(self):
        cam = dict(self.onvif["devices"][0], kind="DISPLAY", display_name="Woonkamer",
                   info=worker.product_info({"firmware": "1"}, "DISPLAY"))
        dev = device_config(cam)["devices"][0]
        self.assertNotIn("kind", dev)
        self.assertNotIn("display_name", dev)
        self.assertEqual((dev["name"], dev["info"]), ("Woonkamer", {"model": "Nest Hub", "manufacturer": "Google", "firmware": "1"}))
        self.assertEqual(dev["ports"], {"soap": 80, "rtsp": 554})
        self.assertEqual(dev["uuid"], cam["uuid"])

    def test_lan_is_found_by_kind_whatever_docker_named_it(self):
        links = [{"ifname": "lo"}, {"ifname": "eth0", "linkinfo": {"info_kind": "veth"}},
                 {"ifname": "eth1", "linkinfo": {"info_kind": "macvlan"}}]
        self.assertEqual(lan_interface(links), "eth1")
        self.assertEqual(lan_interface(list(reversed(links))), "eth1")
        for wrong in (links[:2], links + [{"ifname": "eth2", "linkinfo": {"info_kind": "macvlan"}}],
                      links[:2] + [{"ifname": "eth1", "linkinfo": {"info_kind": "ipvlan"}}]):
            with self.assertRaisesRegex(SystemExit, "NESTUNIFI_LAN_INTERFACE"):
                lan_interface(wrong)

    def test_profile_updates_only_advertised_dimensions(self):
        worker.change("profile", {"id": "fixed-id"})
        updated = worker.read(worker.ONVIF)["devices"][0]
        before = self.onvif["devices"][0]
        expected = dict(before["streams"][0], width=960, height=1280)
        self.assertEqual(updated, dict(before, streams=[expected]))
        self.assertEqual(worker.read(worker.BRIDGE), self.bridge)

    def test_missing_source_is_independent_of_onvif_identity(self):
        cam = self.onvif["devices"][0]
        req = {"action": "view", "id": cam["uuid"]}
        with patch.object(worker, "reader_status", return_value=404):
            self.assertIs(worker.source_live(cam), False)
            with self.assertRaisesRegex(worker.Problem, "geen video van Keuken camera"):
                worker.dispatch(req)
        with patch.object(worker, "reader_status", return_value=200):
            self.assertEqual(worker.dispatch(req)["url"], "/live/cam-keuken-camera/index.m3u8")
        with patch.object(worker, "reader_status", side_effect=TimeoutError):
            self.assertIsNone(worker.source_live(cam))

    def test_rename_keeps_network_identity_and_service(self):
        worker.change("rename", {"id": "fixed-id", "name": "Camera keuken", "previous": "Keuken camera"})
        generated = worker.read(worker.ONVIF)["devices"][0]
        self.assertEqual(generated["display_name"], "Camera keuken")
        original = self.onvif["devices"][0]
        for field in ("uuid", "mac", "streams", "auth"):
            self.assertEqual(original[field], generated[field])
        self.assertEqual(self.bridge, worker.read(worker.BRIDGE))
        with self.assertRaises(worker.Problem):
            worker.change("rename", {"id": "fixed-id", "previous": "Keuken camera", "name": "Oude invoer"})

    def test_add_and_retry_all_layers(self):
        worker.change("add", self.req)
        self.assertEqual({"already": True}, worker.change("add", self.req))
        cam = worker.read(worker.ONVIF)["devices"][-1]
        path = "cam-"+cam["name"]
        media = worker.read(worker.MEDIA)
        self.assertIn(path, media["paths"])
        users = media["authInternalUsers"]
        self.assertEqual(users[0], self.media["authInternalUsers"][0])
        self.assertIn({"action": "publish", "path": path}, users[1]["permissions"])
        self.assertEqual(users[2], {"user": "test", "pass": self.req["password"],
                                    "permissions": [{"action": "read", "path": path}]})
        self.assertEqual(worker.read(worker.BRIDGE)["cameras"][-1]["device_id"], self.dev["device_id"])
        self.assertEqual(cam["display_name"], self.req["name"])
        self.assertEqual(cam["info"]["model"], "Nest Hub")
        self.assertEqual(cam["streams"][0]["width"], 960)
        self.assertTrue(cam["streams"][0]["rtsp"].endswith("/"+path))
        self.assertFalse(worker.PENDING.exists())

    def test_rotate_scopes_reader_and_keeps_uris_and_identity(self):
        worker.change("add", self.req)
        before = worker.read(worker.ONVIF)
        other = before["devices"][-1]
        with patch.object(worker, "confirm_reader") as confirm:
            worker.change("password", {"id": "fixed-id", "password": "nest@#1234"})
            confirm.assert_called_once()
        after = worker.read(worker.ONVIF)
        self.assertEqual(other, after["devices"][-1])
        cam = after["devices"][0]
        self.assertEqual(cam["uuid"], "fixed-id")
        self.assertIn("nest%40%231234@", cam["streams"][0]["rtsp"])
        rules = worker.read(worker.MEDIA)["authInternalUsers"]
        self.assertFalse(any(u["user"] == "test" and u["pass"] == "fixture" for u in rules))
        self.assertEqual(rules[-1]["permissions"], [{"action": "read", "path": "cam-keuken-camera"}])

    def test_password_rollback(self):
        with patch.object(worker, "confirm_reader", side_effect=[worker.Problem("test"), None]):
            with self.assertRaises(worker.Problem):
                worker.change("password", {"id": "fixed-id", "password": "new-test-123"})
        self.assertEqual(worker.read(worker.MEDIA), self.media)
        self.assertEqual(worker.read(worker.ONVIF), self.onvif)
        self.assertFalse(worker.PENDING.exists())

    def test_failed_add_restores_previous_config(self):
        with patch.object(worker, "ready", side_effect=[worker.Problem("start mislukt"), None]):
            with self.assertRaises(worker.Problem):
                worker.change("add", self.req)
        for path, expected in [(worker.BRIDGE, self.bridge), (worker.ONVIF, self.onvif), (worker.MEDIA, self.media)]:
            self.assertEqual(expected, worker.read(path))
        self.assertFalse(worker.PENDING.exists())


if __name__ == "__main__":
    unittest.main()
