"""Exercise configuration changes and crash recovery without a running container or Google."""
import io
import json
import os
import socketserver
import struct
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

import cast
import mixer
import web
import worker
from suite import runtime
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
                                    "onvif": {"ip": "127.0.0.1"}}],
                       "media": {"rtsp_base_url": "rtsp://publisher:fixture@127.0.0.1:8554"}}
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

    def test_installation_from_before_the_mix_gets_sound_paths_and_a_mixer(self):
        media = json.loads(json.dumps(self.media))
        self.assertTrue(worker.prepare(media, self.bridge))
        reader, publisher, mixer = media["authInternalUsers"]
        self.assertEqual(reader, self.media["authInternalUsers"][0])
        self.assertEqual(publisher["permissions"], [{"action": "publish", "path": "cam-keuken-camera"},
                                                    {"action": "publish", "path": worker.SOUND}])
        self.assertEqual((mixer["user"], len(mixer["pass"])), (worker.MIXER, 48))
        self.assertEqual(set(media["paths"]), {"cam-keuken-camera", worker.SOUND})
        self.assertFalse(worker.prepare(media, self.bridge))

    def two_cameras(self):
        garden = dict(self.onvif["devices"][0], name="Tuin", uuid="garden-id", mac="02:ab:cd:ef:00:02",
                      ports={"soap": 18082, "rtsp": 18555}, streams=[
            dict(self.onvif["devices"][0]["streams"][0], rtsp="rtsp://test:fixture@mediamtx:8554/cam-tuin")])
        self.onvif["devices"].append(garden)
        self.bridge["cameras"].append({"device_id": "enterprises/test/devices/garden", "name": "Tuin", "onvif": {"ip": "127.0.0.2"}})
        worker.prepare(self.media, self.bridge)
        self.media["paths"]["cam-tuin"] = {}
        self.media["authInternalUsers"][0]["permissions"].append({"action": "read", "path": "cam-tuin"})
        for path, data in [(worker.ONVIF, self.onvif), (worker.BRIDGE, self.bridge), (worker.MEDIA, self.media)]:
            path.write_text(json.dumps(data))

    def test_mix_is_tried_out_with_the_sound_of_the_cameras_that_have_it(self):
        self.two_cameras()
        publishing = {"cam-keuken-camera", "cam-keuken-camera-audio", "cam-tuin", "mix"}
        asked = []
        def describe(path, username, password, timeout=6):
            asked.append((path, username))
            return 200 if path in publishing else 404
        with patch.object(worker, "describe", describe), patch.object(time, "sleep"):
            for wrong in (["fixed-id"], ["fixed-id", "fixed-id"], ["fixed-id"] * 5, "fixed-id", None):
                with self.assertRaisesRegex(worker.Problem, "twee tot vier"):
                    worker.mix({"ids": wrong})
            self.assertEqual(worker.mix({"ids": ["fixed-id", "garden-id"]}),
                             {"url": "/live/mix/index.m3u8", "silent": ["Tuin"]})
            # The media server runs the mixer while someone watches. No password is on that line.
            self.assertEqual(worker.read(worker.MEDIA)["paths"]["mix"]["runOnDemand"],
                             "python3 -IB /app/mixer.py mix cam-keuken-camera cam-tuin")
            self.assertEqual(asked[-1], ("mix", worker.MIXER))
            # The same choice again leaves the file alone, so a running mix goes on.
            written = worker.MEDIA.stat().st_mtime_ns
            worker.mix({"ids": ["fixed-id", "garden-id"]})
            self.assertEqual(worker.MEDIA.stat().st_mtime_ns, written)
            publishing.remove("mix")
            with self.assertRaisesRegex(worker.Problem, "niet gestart"):
                worker.mix({"ids": ["fixed-id", "garden-id"]})
            publishing.remove("cam-tuin")
            with self.assertRaisesRegex(worker.Problem, "geen video van Tuin"):
                worker.mix({"ids": ["fixed-id", "garden-id"]})

    def test_mixer_gives_every_camera_a_tile_and_leaves_a_missing_one_black(self):
        source = "rtsp://u:p@127.0.0.1:8554/"
        command = mixer.command("out", ["a", None, "c"], ["a-audio", "c-audio"], source)
        self.assertEqual([command[i + 1] for i, arg in enumerate(command) if arg == "-i"],
                         [source + path for path in ("a", "c", "a-audio", "c-audio")])
        graph = command[command.index("-filter_complex") + 1].split(";")
        self.assertTrue(graph[0].startswith("[0:v]fps=15,scale=640:360:") and graph[0].endswith("[v0]"))
        self.assertEqual(graph[1], "color=black:size=640x360:rate=15[v1]")
        self.assertTrue(graph[2].startswith("[1:v]") and graph[2].endswith("[v2]"))
        self.assertEqual(graph[3:], ["[v0][v1][v2]xstack=inputs=3:layout=0_0|640_0|0_360:fill=black:shortest=1[v]",
                                     "[2:a][3:a]amix=inputs=2:duration=shortest:normalize=0[a]"])
        self.assertEqual(command[-1], source + "out")
        self.assertNotIn("-c:a", mixer.command("out", ["a", "b"], [], source))
        self.assertEqual((mixer.size(2), mixer.size(3), mixer.size(4)), ((1280, 360), (1280, 720), (1280, 720)))

    def test_mixer_starts_with_what_there_is_and_starts_over_when_a_camera_is_back(self):
        class Mix:
            def __init__(self, command):
                started.append(command)
                self.waits, self.ended = 0, False
            def wait(self, timeout):
                self.waits += 1
                if not self.ended:
                    raise subprocess.TimeoutExpired("ffmpeg", timeout)
                return 0
            def terminate(self):
                self.ended = True
        started, publishing = [], {"a", "a-audio"}
        def describe(path, username, password, timeout=6):
            if started and started[-1] and path == "b":
                publishing.add("b")  # The second camera comes back while the mix runs.
            return 200 if path in publishing else 404
        config = Path("mediamtx.yml")
        config.write_text(json.dumps({"authInternalUsers": [{"user": mixer.USER, "pass": "s3cret"}]}))
        with patch.multiple(mixer, MEDIA=str(config), describe=describe), patch.object(mixer.signal, "signal"), \
                patch.object(mixer.subprocess, "Popen", Mix):
            self.assertEqual(mixer.main("out", ["a", "b"]), 0)
            self.assertEqual(len(started), 1)
            inputs = [started[0][i + 1] for i, arg in enumerate(started[0]) if arg == "-i"]
            self.assertEqual(inputs, ["rtsp://%s:s3cret@127.0.0.1:8554/%s" % (mixer.USER, path) for path in ("a", "a-audio")])
            self.assertIn("color=black", started[0][started[0].index("-filter_complex") + 1])
            # Without a single picture there is nothing to mix.
            publishing.clear()
            started.clear()
            self.assertEqual(mixer.main("out", ["x", "y"]), 1)
            self.assertEqual(started, [])

    def test_mix_becomes_a_camera_of_its_own_without_touching_the_bridge(self):
        self.two_cameras()
        bridge_written = worker.BRIDGE.stat().st_mtime_ns
        made = worker.dispatch({"action": "mixadd", "ids": ["fixed-id", "garden-id"], "name": "Voor en achter", "password": "mix-camera-1"})
        self.assertEqual(made, {"name": "Voor en achter"})
        cam = worker.read(worker.ONVIF)["devices"][-1]
        path = "cam-" + cam["name"]
        self.assertRegex(path, "^" + worker.MIXES[2:])
        self.assertEqual((cam["kind"], cam["mix"], cam["ports"]), ("MIX", ["fixed-id", "garden-id"], {"soap": 18083, "rtsp": 18556}))
        self.assertEqual(cam["streams"][0], {"name": "main", "rtsp": "rtsp://test:mix-camera-1@mediamtx:8554/" + path,
                                             "width": 1280, "height": 360, "framerate": 15, "bitrate": 1500})
        self.assertNotIn("mix", device_config(cam)["devices"][0])
        media = worker.read(worker.MEDIA)
        self.assertEqual(media["paths"][path]["runOnDemand"], "python3 -IB /app/mixer.py %s cam-keuken-camera cam-tuin" % path)
        self.assertIn({"user": "test", "pass": "mix-camera-1", "permissions": [{"action": "read", "path": path}]}, media["authInternalUsers"])
        # A new bridge file would restart every camera. A mix is nothing to the bridge.
        self.assertEqual(worker.BRIDGE.stat().st_mtime_ns, bridge_written)
        publishing = {"cam-keuken-camera"}
        asked = []
        def describe(asked_path, username, password, timeout=6):
            asked.append(asked_path)
            return 200 if asked_path in publishing else 404
        state = {"media": True, "bridge": True, "cameras": {}, "streams": {}}
        with patch.object(worker, "describe", describe), patch.multiple(worker, read=lambda p: state if p == worker.STATE else json.loads(p.read_text()),
                google_link=lambda: "ok", newer_version=lambda: None, link_expiry=lambda: None):
            shown = {c["name"]: c for c in worker.status()["cameras"]}
            # The overview does not ask for the mix itself: that would start it. It follows its cameras.
            self.assertNotIn(path, asked)
            self.assertEqual((shown["Voor en achter"]["live"], shown["Voor en achter"]["parts"], shown["Voor en achter"]["kind"]),
                             (True, ["Keuken camera", "Tuin"], "MIX"))
            self.assertIsNone(shown["Tuin"]["parts"])
            with self.assertRaisesRegex(worker.Problem, "niet gestart"):
                worker.dispatch({"action": "view", "id": cam["uuid"]})
            publishing.add(path)
            self.assertEqual(worker.dispatch({"action": "view", "id": cam["uuid"]}), {"url": "/live/" + path + "/index.m3u8"})
        for refused, wrong in (("deel van een ander", ["fixed-id", cam["uuid"]]), ("twee tot vier", ["fixed-id"])):
            with self.assertRaisesRegex(worker.Problem, refused):
                worker.dispatch({"action": "mixadd", "ids": wrong, "name": "Nog een", "password": "mix-camera-2"})
        with self.assertRaisesRegex(worker.Problem, "al een camera met deze naam"):
            worker.dispatch({"action": "mixadd", "ids": ["fixed-id", "garden-id"], "name": "voor en achter", "password": "mix-camera-2"})
        with self.assertRaisesRegex(worker.Problem, "deel van het samengestelde beeld Voor en achter"):
            worker.dispatch({"action": "remove", "id": "garden-id"})
        self.assertEqual(worker.dispatch({"action": "pause", "id": cam["uuid"], "paused": True}), {"name": "Voor en achter", "paused": True})
        self.assertIs(worker.read(worker.ONVIF)["devices"][-1]["paused"], True)
        # The next camera takes its settings from a camera, not from the mix, and gets a number of its own.
        worker.change("add", self.req)
        added = worker.read(worker.ONVIF)["devices"][-1]
        self.assertEqual((worker.read(worker.MEDIA)["paths"]["cam-" + added["name"]], added["ports"]["soap"]), ({}, 18084))
        worker.change("remove", {"id": added["uuid"]})
        bridge_written = worker.BRIDGE.stat().st_mtime_ns
        self.assertEqual(worker.dispatch({"action": "remove", "id": cam["uuid"]}), {"name": "Voor en achter", "removed": True})
        self.assertEqual((worker.read(worker.ONVIF), worker.read(worker.MEDIA)), (self.onvif, self.media))
        self.assertEqual(worker.BRIDGE.stat().st_mtime_ns, bridge_written)
        self.assertFalse(worker.PENDING.exists())

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

    def test_cameras_ask_for_their_last_address_again(self):
        with patch.object(runtime, "ADDRESSES", Path("addresses.json")):
            self.assertEqual(runtime.remembered(), {})
            runtime.remember("02:aa", "192.168.1.40")
            runtime.remember("02:bb", "192.168.1.41")
            runtime.remember("02:aa", "192.168.1.42")
            self.assertEqual(runtime.remembered(), {"02:aa": "192.168.1.42", "02:bb": "192.168.1.41"})
            runtime.ADDRESSES.write_text("[")
            self.assertEqual(runtime.remembered(), {})

    def test_interface_name_fits_the_address_check_of_the_dhcp_client(self):
        with patch.object(runtime.Endpoint, "configure"):
            self.assertEqual(runtime.Endpoint({"mac": "02:9C:9c:6e:1d:3e", "name": "x", "ports": {"soap": 18081}}, "", "eth0").dev,
                             "c029c9c6e1d3e")

    def test_service_output_is_passed_on_and_kept(self):
        with patch.object(runtime, "LOG", Path("log.txt")), patch.object(runtime.sys, "stdout", io.StringIO()) as shown:
            runtime.spawn(["sh", "-c", "echo started; echo failed >&2"]).wait()
            # A thread passes each line on, to the container log first and then to the file.
            for _ in range(100):
                if runtime.LOG.exists() and runtime.LOG.read_text().count("\n") == 2:
                    break
                time.sleep(0.02)
            self.assertEqual(sorted(shown.getvalue().split()), ["failed", "started"])
            kept = runtime.LOG.read_text().splitlines()
            self.assertEqual(sorted(line.split()[-1] for line in kept), ["failed", "started"])
            self.assertRegex(kept[0], r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d ")
            runtime.LOG.write_text("x" * (128 * 1024 + 1))
            runtime.keep("next\n")
            self.assertEqual(len(runtime.LOG.with_suffix(".old").read_text()), 128 * 1024 + 1)
            self.assertTrue(runtime.LOG.read_text().endswith(" next\n"))

    def test_pause_keeps_the_camera_in_every_file_and_out_of_the_bridge(self):
        self.assertEqual(worker.dispatch({"action": "pause", "id": "fixed-id", "paused": True}), {"name": "Keuken camera", "paused": True})
        self.assertIs(worker.read(worker.ONVIF)["devices"][0]["paused"], True)
        self.assertIs(worker.read(worker.BRIDGE)["cameras"][0]["paused"], True)
        self.assertNotIn("paused", device_config(worker.read(worker.ONVIF)["devices"][0])["devices"][0])
        self.assertEqual(worker.read(worker.MEDIA), self.media)
        before = worker.BRIDGE.stat().st_mtime_ns
        worker.dispatch({"action": "pause", "id": "fixed-id", "paused": True})  # Already paused: nothing restarts.
        self.assertEqual(worker.BRIDGE.stat().st_mtime_ns, before)
        self.assertEqual(worker.dispatch({"action": "pause", "id": "fixed-id", "paused": False}), {"name": "Keuken camera", "paused": False})
        self.assertEqual((worker.read(worker.ONVIF), worker.read(worker.BRIDGE)), (self.onvif, self.bridge))
        # A pause that cannot be applied is undone, without waiting for a camera that is meant to be offline.
        with patch.object(worker, "ready", side_effect=[worker.Problem("start mislukt"), None]) as ready:
            with self.assertRaises(worker.Problem):
                worker.dispatch({"action": "pause", "id": "fixed-id", "paused": True})
        self.assertEqual(ready.call_args_list[-1].args, (None,))
        self.assertEqual((worker.read(worker.ONVIF), worker.read(worker.BRIDGE)), (self.onvif, self.bridge))
        self.assertFalse(worker.PENDING.exists())

    def test_video_sessions_are_followed_from_what_the_bridge_says(self):
        self.addCleanup(runtime.sessions.clear)
        say = lambda name, state: runtime.session('2026/11/26 10:00:00 INFO session state changed camera=%s state=%s\n' % (name, state))
        runtime.session("2026/11/26 10:00:00 INF [RTSP] [conn 127.0.0.1:51064] opened\n")
        self.assertEqual(runtime.sessions, {})
        for state in ("connecting", "live", "renewing", "live"):  # Renewing a session keeps the video.
            say('"Keuken camera"', state)
        since = runtime.sessions["Keuken camera"]["since"]
        self.assertAlmostEqual(since, time.time(), delta=5)
        self.assertEqual(runtime.sessions["Keuken camera"], {"state": "live", "since": since, "drops": 0})
        for state in ("failed", "backoff", "connecting"):
            say('"Keuken camera"', state)
        say("device-0123", "live")
        self.assertEqual(runtime.sessions["Keuken camera"], {"state": "connecting", "since": since, "drops": 1})
        self.assertEqual(runtime.sessions["device-0123"]["state"], "live")
        # The admin page finds each camera's session by its stream path.
        self.assertEqual(worker.sessions({"streams": runtime.sessions})["cam-keuken-camera"]["drops"], 1)
        self.assertEqual(worker.sessions({}), {})

    def test_update_is_offered_only_for_a_newer_release(self):
        self.addCleanup(worker.update_check.update, until=0.0, value=None)
        tags = ["latest", "2026.11.20", "2026.12.01", "dev"]
        published = lambda: [t for t in tags if worker.RELEASE.fullmatch(t)]
        with patch.multiple(worker, VERSION="2026.11.25", REPOSITORY="owner/nestunifi", published_versions=published):
            self.assertEqual(worker.newer_version(), "2026.12.01")
            tags.append("2027.01.01")
            self.assertEqual(worker.newer_version(), "2026.12.01")  # Asked again only the next day.
            worker.update_check["until"] = 0.0
            self.assertEqual(worker.newer_version(), "2027.01.01")
        with patch.multiple(worker, VERSION="2027.01.01", REPOSITORY="owner/nestunifi", published_versions=published):
            worker.update_check["until"] = 0.0
            self.assertIsNone(worker.newer_version())
        with patch.multiple(worker, VERSION="dev", REPOSITORY="owner/nestunifi"):
            self.assertIsNone(worker.newer_version())

    def test_profile_updates_only_advertised_dimensions(self):
        worker.change("profile", {"id": "fixed-id"})
        updated = worker.read(worker.ONVIF)["devices"][0]
        before = self.onvif["devices"][0]
        expected = dict(before["streams"][0], width=960, height=1280)
        self.assertEqual(updated, dict(before, streams=[expected]))
        self.assertEqual(worker.read(worker.BRIDGE), self.bridge)

    def test_camera_with_video_gets_a_profile_of_its_own_shape_once(self):
        cam = self.onvif["devices"][0]
        state = {"cameras": {cam["uuid"]: {"online": True}}, "streams": {}}
        measured, done, read = [], set(), worker.read
        def measure(req):
            measured.append(req["id"])
            return {"live": True, "width": 1280, "height": 720}
        size = lambda: [worker.read(worker.ONVIF)["devices"][0]["streams"][0][k] for k in ("width", "height")]
        before = size()
        with patch.multiple(worker, read=lambda p: state if p == worker.STATE else read(p), test_stream=measure):
            worker.fit_profiles(done)  # No video yet.
            self.assertEqual((measured, done), ([], set()))
            state["streams"] = {"Keuken camera": {"state": "live"}}
            worker.fit_profiles(done)  # The same shape in a smaller size: left alone.
            self.assertEqual((measured, size()), ([cam["uuid"]], before))
            worker.fit_profiles(done)
            self.assertEqual(len(measured), 1)
            measure = lambda req: {"live": True, "width": 960, "height": 1280}
            with patch.object(worker, "test_stream", measure):
                worker.fit_profiles(set())
            self.assertEqual(size(), [960, 1280])

    def test_cast_gives_the_device_an_address_the_page_serves_to_it(self):
        cam = self.onvif["devices"][0]
        worker.MEDIA.write_text(json.dumps(dict(self.media, hlsCDNSecret="k3y")))
        played = []
        ask = lambda address, origin="http://192.168.1.5:8780": worker.dispatch(
            {"action": "cast", "id": cam["uuid"], "address": address, "origin": origin})
        with patch.object(worker, "reader_status", return_value=200), patch.object(cast, "play", lambda *args: played.append(args)):
            for wrong in (None, 7, "x", "8.8.8.8", "127.0.0.1"):
                with self.assertRaisesRegex(worker.Problem, "Kies een cast-apparaat"):
                    ask(wrong)
            self.assertEqual((ask("192.168.1.30"), len(played)), ({}, 1))
        address, url, title = played[0]
        self.assertEqual((address, title, url.split("/cast/")[0]), ("192.168.1.30", "Keuken camera", "http://192.168.1.5:8780"))
        path, key, part = web.CAST.fullmatch(urlsplit(url).path).groups()
        with patch.object(web, "MEDIA_KEY", "k3y"):
            self.assertEqual((path, key, part), ("cam-keuken-camera", web.cast_key(path), "index.m3u8"))
        def refuse(*args):
            raise cast.Refused("LOAD_FAILED")
        with patch.object(worker, "reader_status", return_value=200), patch.object(cast, "play", refuse):
            with self.assertRaisesRegex(worker.Problem, "speelt het beeld niet af\\.$"):
                ask("192.168.1.30")
            with self.assertRaisesRegex(worker.Problem, "via het IP-adres"):  # A cast device does not know a name of the home.
                ask("192.168.1.30", "https://nest.example")

    def test_cast_device_is_found_and_told_to_play_and_to_stop(self):
        label = lambda name: b"".join(bytes([len(part)]) + part.encode() for part in name.split("."))
        record = lambda owner, kind, data: owner + struct.pack(">HHIH", kind, 1, 120, len(data)) + data
        text = lambda *items: b"".join(bytes([len(item)]) + item.encode() for item in items)
        service = b"\xc0\x0c"  # The rest of the name is at place 12 of the packet.
        answer = (struct.pack(">HHHHHH", 0, 0x8400, 0, 1, 0, 2) + record(label(cast.SERVICE) + b"\0", 12, label("Hub") + service)
                  + record(label("Hub") + service, 16, text("fn=Keuken Display", "md=Google Nest Hub", "ca=231941"))
                  + record(label("Groep") + service, 16, text("fn=Alle speakers", "ca=199172")))  # Sound only.
        self.assertEqual(cast.devices(answer, "192.168.1.30"),
                         [{"name": "Keuken Display", "model": "Google Nest Hub", "address": "192.168.1.30"}])
        heard = []
        apps = [{"appId": cast.PLAYER, "transportId": "t-1", "sessionId": "s-1"}, {"appId": "other", "sessionId": "s-2"}]

        class Device(socketserver.BaseRequestHandler):
            def handle(self):
                say = lambda namespace, data: self.request.sendall(cast.pack("receiver-0", "*", namespace, data))
                while True:
                    head = self.request.recv(4, 0x100)  # MSG_WAITALL
                    if len(head) < 4:
                        return
                    namespace, data = cast.unpack(self.request.recv(struct.unpack(">I", head)[0], 0x100))
                    heard.append((namespace.rsplit(".", 1)[1], data))
                    if data["type"] in ("LAUNCH", "GET_STATUS", "STOP"):
                        say(cast.HEARTBEAT, {"type": "PING"})
                        say(cast.RECEIVER, {"type": "RECEIVER_STATUS", "requestId": 0, "status": {}})  # Told to everyone.
                        say(cast.RECEIVER, {"type": "RECEIVER_STATUS", "requestId": data["requestId"], "status": {"applications": apps}})
                    elif data["type"] == "LOAD":
                        fine = "bad" not in data["media"]["contentId"]
                        say(cast.MEDIA, {"type": "MEDIA_STATUS" if fine else "LOAD_FAILED", "requestId": data["requestId"]})
        device = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Device)
        device.daemon_threads = True
        threading.Thread(target=device.serve_forever, daemon=True).start()
        self.addCleanup(device.server_close)
        self.addCleanup(device.shutdown)
        reach = lambda address, timeout=None: cast.socket.socket.connect(s := cast.socket.socket(), device.server_address) or s
        # No TLS here: the certificate of a cast device is its own and is not checked.
        with patch.object(cast.socket, "create_connection", reach), \
                patch.object(cast.ssl.SSLContext, "wrap_socket", lambda self, sock: sock):
            cast.play("192.168.1.30", "http://nas/cast/a/index.m3u8", "Tuin")
            self.assertEqual([data["type"] for _, data in heard], ["CONNECT", "LAUNCH", "PONG", "CONNECT", "LOAD"])
            self.assertEqual(heard[-1][1]["media"], {
                "contentId": "http://nas/cast/a/index.m3u8", "contentType": "application/x-mpegURL", "streamType": "LIVE",
                "metadata": {"metadataType": 0, "title": "Tuin"}})
            with self.assertRaises(cast.Refused):
                cast.play("192.168.1.30", "http://nas/bad", "Tuin")
            heard.clear()
            cast.stop("192.168.1.30")
            # Only our own player is ended.
            self.assertEqual([data.get("sessionId", data["type"]) for _, data in heard], ["CONNECT", "GET_STATUS", "PONG", "s-1", "PONG"])

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

    def test_remove_clears_every_layer_and_keeps_the_publisher(self):
        worker.change("add", self.req)
        added = worker.read(worker.ONVIF)["devices"][-1]
        self.assertEqual(worker.change("remove", {"id": "fixed-id"}), {"name": "Keuken camera", "removed": True})
        self.assertEqual(worker.read(worker.ONVIF)["devices"], [added])
        self.assertEqual([c["device_id"] for c in worker.read(worker.BRIDGE)["cameras"]], [self.dev["device_id"]])
        media = worker.read(worker.MEDIA)
        self.assertEqual(list(media["paths"]), ["cam-" + added["name"]])
        self.assertFalse(any(rule["path"] == "cam-keuken-camera" for u in media["authInternalUsers"] for rule in u["permissions"]))
        worker.change("remove", {"id": added["uuid"]})
        media = worker.read(worker.MEDIA)
        # The next camera finds its publisher again, even with no camera left to copy from.
        self.assertEqual(media["authInternalUsers"], [{"user": "publisher", "pass": "fixture", "permissions": []}])
        self.assertEqual((worker.read(worker.ONVIF)["devices"], worker.read(worker.BRIDGE)["cameras"], media["paths"]), ([], [], {}))
        self.assertFalse(worker.PENDING.exists())
        worker.change("add", self.req)
        self.assertEqual(len(worker.read(worker.ONVIF)["devices"]), 1)

    def test_failed_remove_restores_an_offline_camera(self):
        waits = []
        def ready(cam=None):
            waits.append(cam)
            if len(waits) == 1:
                raise worker.Problem("start mislukt")
        with patch.object(worker, "ready", ready), self.assertRaises(worker.Problem):
            worker.change("remove", {"id": "fixed-id"})
        self.assertEqual(waits, [None, None])
        for path, expected in [(worker.BRIDGE, self.bridge), (worker.ONVIF, self.onvif), (worker.MEDIA, self.media)]:
            self.assertEqual(expected, worker.read(path))
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
