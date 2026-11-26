"""Camera-management requests received over the private Unix socket."""
import base64
import hashlib
import hmac
import http.client
import ipaddress
import secrets
import socket
import socketserver
import copy
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))  # python -I leaves the script directory out
import google_api

PRIVATE = Path("private")
BRIDGE = PRIVATE / "bridge/config.yaml"
TOKENS = PRIVATE / "bridge/tokens.json"
KEY_FILE = "pubsub-sa.json"
SETUP = PRIVATE / "admin/setup.json"
AUTH = PRIVATE / "admin/auth.json"
NOTIFY = PRIVATE / "admin/notify.json"
attempts = {"failed": 0, "until": 0.0}
link_check = {"until": 0.0, "value": None}
VERSION = os.environ.get("NESTUNIFI_VERSION", "dev")
REPOSITORY = os.environ.get("NESTUNIFI_REPOSITORY", "")
RELEASE = re.compile(r"\d{4}\.\d{2}\.\d{2}")
update_check = {"until": 0.0, "value": None}
ONVIF = PRIVATE / "onvif/config.yaml"
MEDIA = PRIVATE / "media/mediamtx.yml"
PENDING = PRIVATE / "admin/pending.json"
STATE = Path("/run/suite-status.json")
EVENTS = Path("/run/bridge/events.json")  # The bridge keeps here whether it can read Google's event subscription.
LOG = Path("/run/suite-log.txt")  # The supervisor keeps the latest output of every service here.
CONTROL = Path("/run/control/control.sock")
ORIGIN = "nestunifi:"
MAX_NAME = 48
MAX_REQUEST = 256 * 1024  # A backup is the largest request.
FAMILIES = {"CAMERA": "Nest Cam", "DOORBELL": "Nest Doorbell", "DISPLAY": "Nest Hub"}
# What a restored configuration keeps of the backup.
GOOGLE_KEYS = {"client_id", "client_secret", "project_id", "redirect_uri", "pubsub_subscription", "service_account_key"}
DEVICE_KEYS = {"name", "display_name", "kind", "uuid", "mac", "serial", "ports", "info", "auth", "streams", "paused"}
# Each file in a backup, with the user the services read it as.
BACKUP = {"bridge/config.yaml": (BRIDGE, 10001), "bridge/tokens.json": (TOKENS, 10001),
          "bridge/" + KEY_FILE: (BRIDGE.parent / KEY_FILE, 10001), "media/mediamtx.yml": (MEDIA, 10002),
          "onvif/config.yaml": (ONVIF, 0), "onvif/addresses.json": (ONVIF.parent / "addresses.json", 0),
          "admin/setup.json": (SETUP, 0), "admin/notify.json": (NOTIFY, 0)}
# The detections a webhook can hear about, with the message for each.
KINDS = {"chime": "Er wordt aangebeld bij %s.", "person": "Persoon gezien bij %s.", "motion": "Beweging bij %s."}
HOLD = 300  # A problem is reported once it has lasted this many seconds.


class Problem(Exception):
    pass


def read(path):
    return json.loads(path.read_text())


def replace(path, value, uid=None):
    """Write atomically. A new file belongs to `uid` and is private to it."""
    with tempfile.NamedTemporaryFile(dir=path.parent, mode="w", delete=False) as f:
        tmp = Path(f.name)
        try:
            json.dump(value, f, ensure_ascii=False, indent=2)
            if path.exists():
                st = path.stat()
                os.fchown(f.fileno(), st.st_uid, st.st_gid)
                os.fchmod(f.fileno(), st.st_mode & 0o777)
            elif uid is not None:
                os.fchown(f.fileno(), uid, uid)
            f.flush()
            os.fsync(f.fileno())
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)


def run(*args, timeout=75, input_data=None, uid=None):
    return subprocess.run(args, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=timeout, input=input_data,
                          **({"user":uid, "group":uid, "extra_groups":[]} if uid else {})).stdout


def slug(name):
    return re.sub("[^a-z0-9]+", "-", name.lower()).strip("-")


def service(cam):
    return "nest-" + slug(cam["name"])


def key(device_id):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, ORIGIN + device_id))


def name_input(value):
    if not isinstance(value, str):
        raise Problem("Vul een naam in.")
    value = value.strip()
    if not 1 <= len(value) <= MAX_NAME or not any(c.isalnum() for c in value) or any(
            not c.isalnum() and c not in " -'()._" for c in value):
        raise Problem("Gebruik maximaal 48 letters, cijfers, spaties of de tekens - ' ( ) . _")
    return value


def camera(config, camera_id):
    for cam in config["devices"]:
        if cam["uuid"] == camera_id:
            return cam
    raise Problem("Deze camera staat niet meer in de configuratie. Vernieuw het overzicht.")


def display(cam):
    return cam.get("display_name", cam["name"])


def kind_from_name(name):
    if name.lower().startswith("nest hub"):
        return "DISPLAY"
    if name.lower().startswith("deurbel"):
        return "DOORBELL"
    return "CAMERA"


def product_info(info, kind):
    return dict(info, manufacturer="Google", model=FAMILIES[kind])


def stream_path(cam):
    uri = urlsplit(cam["streams"][0]["rtsp"])
    if uri.hostname != "mediamtx" or not re.fullmatch(r"/cam-[a-z0-9-]+", uri.path):
        raise Problem("Onbekend streampad voor deze camera.")
    return uri.path[1:]


def password_input(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9!$()*+.;<=>\[\]^_{}@#&-]{8,64}", value):
        raise Problem("Gebruik 8 tot 64 letters, cijfers of toegestane leestekens.")
    return value


def set_reader(cam, media, password, adding=False):
    path = stream_path(cam)
    username = cam["auth"]["username"]
    users = media["authInternalUsers"]
    read_rule = {"action": "read", "path": path}
    if not adding:
        owners = [u for u in users if read_rule in u["permissions"]]
        if len(owners) != 1 or owners[0].get("user") != username:
            raise Problem("De toegangsrechten van dit streampad zijn niet eenduidig.")
        old = owners[0]
        old["permissions"].remove(read_rule)
        if not old["permissions"]:
            users.remove(old)
    users.append({"user": username, "pass": password, "permissions": [read_rule]})
    cam["auth"] = dict(cam["auth"], password=password)
    st = cam["streams"][0]
    uri = urlsplit(st["rtsp"])
    st["rtsp"] = uri._replace(netloc=quote(username, safe="")+":"+quote(password, safe="")+"@"+uri.netloc.rsplit("@", 1)[-1]).geturl()


def reader_status(cam, password, timeout=6):
    path = stream_path(cam)
    credentials = (cam["auth"]["username"] + ":" + password).encode()
    basic = base64.b64encode(credentials).decode()
    query = (f"DESCRIBE rtsp://mediamtx:8554/{path} RTSP/1.0\r\nCSeq: 1\r\n"
             f"Accept: application/sdp\r\nAuthorization: Basic {basic}\r\n\r\n")
    with socket.create_connection(("127.0.0.1", 8554), timeout=timeout) as conn:
        conn.sendall(query.encode())
        with conn.makefile("rb") as response:
            return int(response.readline(256).split()[1])


def confirm_reader(cam, former_password):
    for _ in range(8):
        if (reader_status(cam, cam["auth"]["password"]) in (200, 404) and
                reader_status(cam, former_password) == 401):
            return
        time.sleep(0.5)
    raise Problem("De videobridge heeft de nieuwe toegangsrechten niet bevestigd.")


def source_live(cam):
    # DESCRIBE only checks the local publisher. It doesn't open a Google session.
    try:
        code = reader_status(cam, cam["auth"]["password"], timeout=1)
    except (OSError, ValueError, IndexError, Problem):
        return None
    return {200: True, 404: False}.get(code)


def camera_call(address, method="GET", path="/trigger/motion"):
    """Ask a camera's private listener. Gives the status and the body, or None when it does not answer."""
    conn = http.client.HTTPConnection(address, 80, timeout=0.5)
    try:
        conn.request(method, path)
        response = conn.getresponse()
        return response.status, response.read(2048)
    except (OSError, http.client.HTTPException):
        return None, b""
    finally:
        conn.close()


def motion_addresses(bridge):
    """The private address of each camera that hears events, by MAC. The supervisor hands out the same ones."""
    if not bridge.get("events", {}).get("onvif"):
        return {}
    return {c["onvif"]["mac"].lower(): c["onvif"]["ip"] for c in bridge["cameras"]
            if c.get("event") is not None and c["onvif"].get("mac")}


def motion(address):
    """What the camera itself knows: its motion, the latest detection of each kind and who listens. None without events."""
    if not address:
        return None
    status, raw = camera_call(address)
    try:
        answer = json.loads(raw) if status == 200 else None
    except ValueError:
        return None
    return answer if isinstance(answer, dict) else None


def motion_test(req):
    """A five-second motion of our own, to see whether the recorder hears this camera."""
    cam = camera(read(ONVIF), req.get("id"))
    address = motion_addresses(read(BRIDGE)).get(cam["mac"].lower())
    before = motion(address)
    if before is None:
        raise Problem("Bewegingsmeldingen staan voor " + display(cam) + " niet aan. Rond eerst stap 2 van de installatie af.")
    if not before.get("listeners"):
        raise Problem("Geen recorder luistert naar bewegingsmeldingen van " + display(cam) +
                      ". Adopteer de camera in UniFi Protect en probeer het daarna opnieuw.")
    status, _ = camera_call(address, "POST", "/trigger/motion?state=test")
    if status == 409:
        raise Problem("Bij " + display(cam) + " is op dit moment al beweging. Kijk in Protect of die in de tijdlijn staat.")
    if status != 200:
        raise Problem("De virtuele camera nam de testbeweging niet aan.")
    for _ in range(16):
        time.sleep(0.25)
        if (motion(address) or {}).get("delivered") != before.get("delivered"):
            return {"delivered": True}
    return {"delivered": False}


def events_state(bridge):
    """Whether the bridge can read Google's event subscription: "ok", "denied", "missing", "key" or "unreachable".
    None while events are off or the bridge has not tried yet."""
    if not bridge.get("events", {}).get("onvif"):
        return None
    try:
        state = read(EVENTS)
        if state["ok"]:
            return "ok"
        error = str(state.get("error", ""))
    except (OSError, ValueError, KeyError, TypeError):
        return None
    code = re.search(r"HTTP (\d{3})", error)
    if any(word in error for word in ("service account key", "private key", "oauth2")) or (code and code[1] == "401"):
        return "key"
    return {"403": "denied", "404": "missing"}.get(code and code[1], "unreachable")


def sessions(state):
    """What the bridge last said about each camera's video, by stream path: the state, since when, how often it stopped."""
    return {"cam-" + slug(name): entry for name, entry in state.get("streams", {}).items()}


def link_expiry():
    """When Google ends the link by itself, as it does for an OAuth app in the test status. None for a lasting link."""
    value = (read(SETUP) if SETUP.exists() else {}).get("link_expires")
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def google_link():
    """Whether Google still accepts the stored link. Asked at most every ten minutes, a failure after one."""
    if not TOKENS.exists():
        return None
    if time.monotonic() < link_check["until"]:
        return link_check["value"]
    google = read(BRIDGE)["google"]
    value = google_api.check(google["client_id"], google["client_secret"], read(TOKENS).get("refresh_token", ""))
    link_check.update(until=time.monotonic() + (600 if value == "ok" else 60), value=value)
    return value


def published_versions():
    """The date tags in the image registry. Works without an account once the image is public."""
    base = "https://ghcr.io/v2/" + REPOSITORY
    with urllib.request.urlopen("https://ghcr.io/token?scope=repository:" + REPOSITORY + ":pull", timeout=8) as r:
        token = json.load(r)["token"]
    tags, url = [], base + "/tags/list?n=1000"
    for _ in range(20):  # ponytail: 20,000 tags, decades of daily releases
        with urllib.request.urlopen(urllib.request.Request(url, headers={"Authorization": "Bearer " + token}), timeout=8) as r:
            tags += json.load(r).get("tags") or []
            follow = re.match(r"<(/v2/[^>]+)>;\s*rel=\"next\"", r.headers.get("Link", ""))
        if not follow:
            break
        url = "https://ghcr.io" + follow[1]
    return [t for t in tags if RELEASE.fullmatch(t)]


def newer_version():
    """The newest published version when it is newer than this one. Asked once a day."""
    if not RELEASE.fullmatch(VERSION) or not REPOSITORY:
        return None
    if time.monotonic() < update_check["until"]:
        return update_check["value"]
    try:
        newest = max(published_versions(), default=VERSION)
        update_check.update(until=time.monotonic() + 86400, value=newest if newest > VERSION else None)
    except (OSError, ValueError, KeyError, TypeError):
        update_check["until"] = time.monotonic() + 3600
    return update_check["value"]


def status():
    onvif = read(ONVIF)
    state = read(STATE)
    bridge = read(BRIDGE)
    motion_ips, video = motion_addresses(bridge), sessions(state)
    def summary(cam):
        current = state["cameras"].get(cam["uuid"], {})
        addr = current.get("ip", "")
        healthy = current.get("online", False)
        profile = cam["streams"][0]
        paused = bool(cam.get("paused"))
        private = None if paused else motion_ips.get(cam["mac"].lower())
        live = source_live(cam) if state["media"] and not paused else None
        seen = video.get(stream_path(cam), {})
        return {"id": cam["uuid"], "name": display(cam), "ip": addr, "online": healthy, "paused": paused,
                "username": cam["auth"]["username"], "kind": cam.get("kind", kind_from_name(cam["name"])),
                "model": cam["info"]["model"], "live": live,
                "since": seen.get("since") if live else None, "drops": seen.get("drops", 0),
                "width": profile["width"], "height": profile["height"], "motion": motion(private),
                # The web process fetches the still from the camera's private listener, which it finds by this number.
                "still": "/snapshot/" + private.rsplit(".", 1)[1] + ".jpg" if private else None}
    with ThreadPoolExecutor(max_workers=6) as pool:
        link, update = pool.submit(google_link), pool.submit(newer_version)
        cams = sorted(pool.map(summary, onvif["devices"]), key=lambda c: c["name"].casefold())
    return {"cameras": cams, "bridge": state["bridge"], "google": link.result(), "link_expires": link_expiry(),
            "events": events_state(bridge), "version": VERSION, "update": update.result()}



def inventory():
    # The existing bridge handles token refresh, Google pagination and the SDM client.
    try:
        raw = run("nest-bridge", "-config=/config/config.yaml",
                  "-tokens=/config/tokens.json", "devices-json", timeout=40, uid=10001)
    except subprocess.CalledProcessError as exc:
        if b"no devices returned" in exc.stderr:
            return []
        raise
    return [d for d in json.loads(raw) if "WEB_RTC" in (d.get("protocols") or [])
            and d["type"] in ("CAMERA", "DOORBELL", "DISPLAY")]


def available():
    configured = {c["device_id"] for c in read(BRIDGE)["cameras"]}
    result = []
    for dev in inventory():
        if dev["device_id"] not in configured:
            label = dev["name"]
            if not label or label == dev["device_id"].rsplit("/", 1)[-1]:
                label = {"DISPLAY": "Nest Hub", "DOORBELL": "Deurbel", "CAMERA": "Camera"}[dev["type"]]
            result.append({"id": key(dev["device_id"]), "name": label, "type": dev["type"]})
    return {"available": result, "configured": len(configured)}


def add_configs(bridge, onvif, media, dev, label, password):
    device_id = dev["device_id"]
    uid = uuid.UUID(key(device_id))
    internal = "device-" + uid.hex[:16]
    mac = "02:" + ":".join("%02x" % b for b in uid.bytes[:5])
    if any(c["uuid"] == str(uid) or c["mac"].lower() == mac or service(c) == "nest-"+internal
           for c in onvif["devices"]):
        raise Problem("Deze netwerkidentiteit is al in gebruik.")
    paths = media["paths"]
    path = "cam-" + internal
    if path in paths:
        raise Problem("Het streampad is al in gebruik.")
    if onvif["devices"]:
        source = onvif["devices"][0]
        template = urlsplit(source["streams"][0]["rtsp"])
        old_path = template.path.strip("/")
        if template.hostname != "mediamtx" or old_path not in paths:
            raise Problem("De oorspronkelijke videoconfiguratie kon niet worden herkend.")
        info, auth = source["info"], source["auth"]
        profile = dict(source["streams"][0], rtsp=template._replace(path="/"+path).geturl())
        route = copy.deepcopy(paths[old_path])
    else:
        # The first camera has no sibling to copy. It publishes as the media user in the bridge's own URL.
        info, auth = {"firmware": "NestUnifi"}, {"username": "nestunifi"}
        profile = {"name": "main", "rtsp": "rtsp://mediamtx:8554/"+path, "width": 1920, "height": 1080,
                   "framerate": 30, "bitrate": 2000}
        route, old_path = {}, None
    ports = {port for c in onvif["devices"] for port in c["ports"].values()}
    addresses = {c["onvif"]["ip"] for c in bridge["cameras"]}
    index = next((i for i in range(1, 255) if 18080+i not in ports and 18553+i not in ports
                  and "127.0.0."+str(i) not in addresses), None)
    if index is None:
        raise Problem("Er zijn geen vrije camera-adressen meer.")
    entry = {"device_id": device_id, "name": internal, "audio": False,
             "onvif": {"ip": "127.0.0."+str(index), "mac": mac}}
    if bridge.get("events", {}).get("onvif"):
        entry["event"] = {"linger": "60s"}
    bridge["cameras"].append(entry)
    cam = {"name": internal, "display_name": label, "kind": dev["type"], "uuid": str(uid), "mac": mac,
           "serial": mac.replace(":", ""), "ports": {"soap": 18080+index, "rtsp": 18553+index},
           "info": product_info(info, dev["type"]), "auth": dict(auth), "streams": [profile]}
    onvif["devices"].append(cam)
    paths[path] = route
    publisher = None if old_path else urlsplit(bridge["media"]["rtsp_base_url"]).username
    copied_publish = False
    for user in media["authInternalUsers"]:
        if old_path:
            rules = [dict(rule, path=path) for rule in user["permissions"] if rule.get("path") == old_path and rule["action"] == "publish"]
        else:
            rules = [{"action": "publish", "path": path}] if publisher and user.get("user") == publisher else []
        copied_publish |= bool(rules)
        user["permissions"].extend(rules)
    if not copied_publish:
        raise Problem("De bestaande streamrechten konden niet worden overgenomen.")
    set_reader(cam, media, password, adding=True)
    return cam


def bridge_entry(bridge, cam):
    # The bridge publishes each camera to the path it derives from its name.
    entries = [c for c in bridge["cameras"] if "cam-" + slug(c["name"]) == stream_path(cam)]
    if len(entries) != 1:
        raise Problem("De videobridge-instelling van deze camera is niet eenduidig.")
    return entries[0]


def remove_configs(bridge, onvif, media, cam):
    """Take one camera out of all three files. A reader left without streams goes too; publishers stay for the next camera."""
    path = stream_path(cam)
    bridge["cameras"].remove(bridge_entry(bridge, cam))
    onvif["devices"].remove(cam)
    media["paths"].pop(path, None)
    for user in list(media["authInternalUsers"]):
        reader = {"action": "read", "path": path} in user["permissions"]
        user["permissions"] = [rule for rule in user["permissions"] if rule.get("path") != path]
        if reader and not user["permissions"]:
            media["authInternalUsers"].remove(user)


def ready(cam=None):
    revision = hashlib.sha256(ONVIF.read_bytes()).hexdigest()
    for _ in range(60):
        state = read(STATE) if STATE.exists() else {}
        if state.get("revision") == revision and (cam is None or
                state["cameras"].get(cam["uuid"], {}).get("online")):
            return
        time.sleep(1)
    raise Problem("De virtuele camera kon niet starten.")


def recover():
    if not PENDING.exists():
        return
    job = read(PENDING)
    if job["action"] == "password":
        time.sleep(1.2)  # MediaMTX coalesces configuration events within one second.
    for path, data in job["before"].items():
        if data is None:
            Path(path).unlink(missing_ok=True)
        else:
            replace(Path(path), data)
    # A restored camera may have been offline before; waiting for it would keep every request failing.
    cam = None if job["action"] in ("add", "remove", "restore", "pause") else camera(read(ONVIF), job["camera"]["uuid"])
    ready(cam)
    if job["action"] == "password":
        confirm_reader(cam, job["camera"]["auth"]["password"])
    PENDING.unlink()


def change(action, req):
    onvif = read(ONVIF)
    label = name_input(req.get("name")) if action in ("rename", "add") else None
    before = {str(ONVIF): copy.deepcopy(onvif)}
    if action == "password":
        cam = camera(onvif, req.get("id"))
        password = password_input(req.get("password"))
        former = cam["auth"]["password"]
        label = display(cam)
        if password == former:
            return {"name": label}
        media = read(MEDIA)
        before[str(MEDIA)] = copy.deepcopy(media)
        set_reader(cam, media, password)
    elif action == "profile":
        cam = camera(onvif, req.get("id"))
        label = display(cam)
        live = test_stream(req)
        if not live["live"]:
            raise Problem("Geen livebeeld beschikbaar om de resolutie te meten.")
        st = cam["streams"][0]
        if (st["width"], st["height"]) == (live["width"], live["height"]):
            return {"name": label}
        st.update(width=live["width"], height=live["height"])
    elif action == "rename":
        cam = camera(onvif, req.get("id"))
        if display(cam) == label:
            return {"name": label}
        if req.get("previous") != display(cam):
            raise Problem("De naam is intussen gewijzigd. Vernieuw het overzicht.")
        cam["display_name"] = label
    elif action == "pause":
        # A paused camera keeps its place in every file. The bridge and the supervisor leave it alone, so
        # Google is not asked for its video and it is gone from the network until it is resumed.
        cam = camera(onvif, req.get("id"))
        label, paused = display(cam), req.get("paused") is True
        if bool(cam.get("paused")) == paused:
            return {"name": label, "paused": paused}
        bridge = read(BRIDGE)
        before[str(BRIDGE)] = copy.deepcopy(bridge)
        for item in (cam, bridge_entry(bridge, cam)):
            item.pop("paused", None)
            if paused:
                item["paused"] = True
    elif action == "remove":
        cam = camera(onvif, req.get("id"))
        label = display(cam)
        bridge, media = read(BRIDGE), read(MEDIA)
        before.update({str(BRIDGE): copy.deepcopy(bridge), str(MEDIA): copy.deepcopy(media)})
        remove_configs(bridge, onvif, media, cam)
    else:
        label = name_input(req.get("name"))
        bridge, media = read(BRIDGE), read(MEDIA)
        config_ids = {key(c["device_id"]) for c in bridge["cameras"]}
        if req.get("id") in config_ids:
            return {"already": True}
        dev = next((d for d in inventory() if key(d["device_id"]) == req.get("id")), None)
        if dev is None:
            raise Problem("Google biedt dit apparaat niet meer aan. Zoek opnieuw.")
        before.update({str(BRIDGE): copy.deepcopy(bridge), str(MEDIA): copy.deepcopy(media)})
        cam = add_configs(bridge, onvif, media, dev, label, password_input(req.get("password")))
    if action in ("add", "rename") and any(display(c).casefold() == label.casefold() and c is not cam
                                             for c in onvif["devices"]):
        raise Problem("Er is al een camera met deze naam.")
    replace(PENDING, {"action": action, "camera": cam, "before": before})
    try:
        if action in ("add", "remove", "pause"):
            replace(BRIDGE, bridge)
        if action in ("add", "password", "remove"):
            replace(MEDIA, media)
        replace(ONVIF, onvif)
        ready(None if action in ("remove", "pause") else cam)
        if action == "password":
            confirm_reader(cam, former)
        if action == "add":
            live = test_stream({"id": cam["uuid"]})
            profile = cam["streams"][0]
            if live["live"] and (profile["width"], profile["height"]) != (live["width"], live["height"]):
                profile.update(width=live["width"], height=live["height"])
                replace(ONVIF, onvif)
                ready(cam)
        PENDING.unlink()
    except Exception:
        recover()
        raise
    if action == "pause":
        return {"name": label, "paused": paused}
    return {"name": label, "removed": True} if action == "remove" else {"name": label}


def test_stream(req):
    cam = camera(read(ONVIF), req.get("id"))
    uri = cam["streams"][0]["rtsp"]
    if any(ch in uri for ch in "'\r\n"):
        raise Problem("Ongeldige streambron.")
    playlist = ("ffconcat version 1.0\nfile '"+uri+"'\noption rtsp_transport tcp\n").encode()
    try:
        raw = run("ffprobe", "-v", "error", "-f", "concat", "-safe", "0", "-protocol_whitelist",
                  "pipe,file,rtsp,tcp,rtp,udp", "-select_streams", "v:0", "-read_intervals", "%+1",
                  "-count_packets", "-show_entries", "stream=codec_name,width,height,nb_read_packets",
                  "-of", "json", "pipe:0", timeout=20, input_data=playlist, uid=10003)
        info = json.loads(raw)["streams"][0]
        if int(info["nb_read_packets"]) > 0:
            return {"live": True, "width": info["width"], "height": info["height"], "codec": info["codec_name"]}
    except (subprocess.SubprocessError, IndexError, KeyError, TypeError, ValueError):
        pass
    return {"live": False}


def bootstrap():
    """Write the configuration of a new installation. An existing installation is never rewritten.

    The bridge file is written last and marks the installation as complete, so an interrupted first start
    begins again with a new shared secret for the bridge and the media server.
    """
    if BRIDGE.exists():
        return
    secret = secrets.token_hex(24)
    for path, uid, value in [
            (MEDIA, 10002, {"logLevel": "info", "authMethod": "internal",
                            "authInternalUsers": [{"user": "bridge", "pass": secret, "ips": [], "permissions": []}],
                            "api": False, "metrics": False, "pprof": False, "playback": False,
                            "rtsp": True, "rtspTransports": ["tcp"], "rtmp": False, "webrtc": False,
                            "srt": False, "moq": False, "hls": True, "hlsVariant": "mpegts",
                            "hlsAlwaysRemux": False, "hlsMuxerCloseAfter": "60s", "hlsAllowOrigins": [],
                            "hlsCDNSecret": secrets.token_hex(32), "paths": {}}),
            (ONVIF, 0, None), (SETUP, 0, None),
            (BRIDGE, 10001, {"google": {}, "media": {"rtsp_base_url": "rtsp://bridge:"+secret+"@127.0.0.1:8554"},
                             "events": {"onvif": False}, "viewer": {"listen": "off"}, "cameras": []})]:
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chown(path.parent, uid, uid)
        os.chmod(path.parent, 0o700)
        if value is not None:
            path.unlink(missing_ok=True)
            replace(path, value, uid)
        elif not path.exists():
            replace(path, {"devices": []} if path == ONVIF else {}, uid)


def enable_events(config, devices, subscription, key_file):
    """Give every camera its internal motion address and switch Pub/Sub forwarding on."""
    indices = {c["mac"].lower(): c["ports"]["soap"] - 18080 for c in devices}
    cameras = config["cameras"]
    macs = {c["onvif"]["mac"].lower() for c in cameras}
    if (len(indices) != len(devices) or len(macs) != len(cameras) or macs != indices.keys()
            or len(set(indices.values())) != len(indices) or not all(1 <= i <= 254 for i in indices.values())):
        raise ValueError("Camera identities and internal ports must map one to one")
    for cam in cameras:
        cam["onvif"]["ip"] = "127.0.0." + str(indices[cam["onvif"]["mac"].lower()])
        cam.setdefault("event", {}).setdefault("linger", "60s")
    config.setdefault("events", {})["onvif"] = True
    config["google"].update(pubsub_subscription=subscription, service_account_key=key_file)


def redirect_for(origin):
    """Google only redirects to HTTPS on a public host name. Anywhere else the user pastes the address back."""
    host = urlsplit(origin).hostname or ""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if origin.startswith("https://") and "." in host:
            return origin + "/oauth2callback"
    return "https://www.google.com"


def setup_state(origin):
    bridge = read(BRIDGE)
    google = bridge["google"]
    client = bool(google.get("client_id") and google.get("client_secret"))
    redirect = client and google.get("redirect_uri") or redirect_for(origin)
    return {"client": client, "client_id": google.get("client_id", ""), "redirect": redirect,
            "register": redirect_for(origin), "manual": not redirect.startswith(origin + "/"),
            "cloud": bool(google.get("pubsub_subscription") and google.get("service_account_key")),
            "topic": (read(SETUP) if SETUP.exists() else {}).get("topic", ""),
            "project": google.get("project_id", ""), "linked": TOKENS.exists(), "cameras": len(bridge["cameras"])}


def setup(action, req):
    origin = req.get("origin") or ""
    bridge = read(BRIDGE)
    google = bridge["google"]
    progress = read(SETUP) if SETUP.exists() else {}
    if action == "client":
        client_id, secret = (str(req.get(k) or "").strip() for k in ("client_id", "client_secret"))
        if not re.fullmatch(r"\d+-[a-z0-9]+\.apps\.googleusercontent\.com", client_id):
            raise Problem("Een client-ID eindigt op .apps.googleusercontent.com. Kopieer hem opnieuw uit Google Cloud.")
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,128}", secret):
            raise Problem("Het clientgeheim is onvolledig. Kopieer het opnieuw uit Google Cloud.")
        google.update(client_id=client_id, client_secret=secret, redirect_uri=redirect_for(origin))
        replace(BRIDGE, bridge)
        link_check["until"] = 0.0
    elif action == "project":
        project = str(req.get("project") or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}", project):
            raise Problem("Een Device Access-project-ID heeft de vorm 1a2b3c4d-1234-5678-9abc-1a2b3c4d5e6f. "
                          "Het is niet de ID van je Google Cloud-project.")
        google["project_id"] = project
        replace(BRIDGE, bridge)
    elif action == "consent":
        kind = req.get("kind")
        if kind not in ("cloud", "link") or not google.get("client_id") or (kind == "link" and not google.get("project_id")):
            raise Problem("Rond eerst de vorige stap af.")
        state = secrets.token_urlsafe(16)
        replace(SETUP, dict(progress, state=state, kind=kind))
        args = (google["client_id"], setup_state(origin)["redirect"], state)
        return {"url": google_api.consent_url(*args) if kind == "cloud" else google_api.link_url(google["project_id"], *args)}
    elif action == "code":
        code, state, kind = req.get("code"), req.get("state"), progress.get("kind")
        if not kind or not isinstance(code, str) or not 8 <= len(code) <= 512:
            raise Problem("Deze koppeling is verlopen. Start de stap opnieuw.")
        # A pasted address may lack the state. A redirect to this page must always carry ours.
        redirect = setup_state(origin)["redirect"]
        pasted = not redirect.startswith(origin + "/")
        if state != progress["state"] and not (pasted and state is None):
            raise Problem("Deze koppeling hoort niet bij de laatste poging. Start de stap opnieuw.")
        kept = {k: v for k, v in progress.items() if k not in ("state", "kind")}
        replace(SETUP, kept)
        try:
            token = google_api.exchange(google["client_id"], google["client_secret"], redirect, code)
            if kind == "cloud":
                key_path = BRIDGE.parent / KEY_FILE
                # A key Google no longer accepts is replaced. Otherwise the one from an earlier run stays.
                known = (read(key_path).get("client_email")
                         if key_path.exists() and events_state(bridge) != "key" else None)
                made = google_api.provision(token["access_token"], google["client_id"], known)
                if made["key"]:
                    replace(key_path, made["key"], 10001)
                enable_events(bridge, read(ONVIF)["devices"], made["subscription"], KEY_FILE)
                replace(BRIDGE, bridge)
                replace(SETUP, dict(kept, topic=made["topic"]))
            else:
                if not token.get("refresh_token"):
                    raise Problem("Google gaf geen blijvende toegang. Start de koppeling opnieuw en geef toestemming.")
                # Google only names a lifetime for a link that ends by itself: the OAuth app is in the test status.
                lifetime = token.get("refresh_token_expires_in")
                kept.pop("link_expires", None)
                if isinstance(lifetime, int) and not isinstance(lifetime, bool) and lifetime > 0:
                    kept["link_expires"] = int(time.time()) + lifetime
                replace(SETUP, kept)
                expiry = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + int(token.get("expires_in", 0))))
                replace(TOKENS, {"access_token": token["access_token"], "token_type": "Bearer",
                                 "refresh_token": token["refresh_token"], "expiry": expiry}, 10001)
                # The bridge reads the token file only when it starts. A rewritten configuration restarts it.
                replace(BRIDGE, bridge)
                link_check["until"] = 0.0
                try:
                    inventory()  # Google completes the link, and starts events, on the first device listing.
                except subprocess.SubprocessError:
                    pass  # The camera step lists again and reports what is wrong.
        except google_api.Failure as exc:
            raise Problem(str(exc))
        except ValueError:
            raise Problem("De camera-instellingen passen niet meer bij elkaar. Meldingen zijn niet ingeschakeld.")
    return setup_state(origin)


def notice_settings(data):
    """Checked webhook settings. An empty address switches the messages off."""
    url, events = data.get("url", ""), data.get("events", [])
    if not isinstance(url, str) or not isinstance(events, list) or not all(isinstance(k, str) and k in KINDS for k in events):
        raise Problem("Ongeldige instellingen voor meldingen.")
    url = url.strip()
    if url:
        try:
            parts = urlsplit(url)
            usable = parts.scheme in ("http", "https") and bool(parts.hostname)
        except ValueError:
            usable = False
        if not usable or len(url) > 500 or re.search(r"[\s\x00-\x1f]", url):
            raise Problem("Vul een webadres in dat begint met http:// of https://.")
    return {"url": url, "events": sorted(set(events)), "health": bool(data.get("health"))}


def notices():
    if not NOTIFY.exists():
        return {"url": "", "events": ["chime", "person"], "health": True}
    try:
        return notice_settings(read(NOTIFY))
    except (OSError, ValueError, AttributeError, Problem):
        return {"url": "", "events": [], "health": False}


def send_notice(url, event, title, text, camera_name=None):
    """One JSON message to the webhook. `text` and `content` repeat the message for services that read only those."""
    body = {"title": title, "message": text, "event": event, "text": text, "content": text,
            "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if camera_name:
        body["camera"] = camera_name
    request = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "User-Agent": "NestUnifi"})
    with urllib.request.urlopen(request, timeout=6) as response:
        response.read(1)


def notify(action, req):
    if action == "notifytest":
        settings = notices()
        if not settings["url"]:
            raise Problem("Vul eerst een webadres in en sla het op.")
        try:
            send_notice(settings["url"], "test", "NestUnifi", "Dit is een testmelding van NestUnifi.")
        except (OSError, ValueError, http.client.HTTPException) as exc:
            code = getattr(exc, "code", None)
            raise Problem("De testmelding is niet aangekomen" + (" (HTTP %d)" % code if isinstance(code, int) else "") +
                          ". Controleer het webadres.")
    elif "url" in req:
        replace(NOTIFY, notice_settings(req))
    return notices()


def watch_events(state, settings, send):
    """One look at what every camera detected. A camera's first answer only sets the baseline, so nothing old is sent."""
    names = {c["mac"].lower(): (c["uuid"], display(c)) for c in read(ONVIF)["devices"]}
    seen, quiet = state.setdefault("detected", {}), state.setdefault("quiet", {})
    for mac, address in motion_addresses(read(BRIDGE)).items():
        camera_id, name = names.get(mac, (None, None))
        current = (motion(address) or {}).get("detected")
        if camera_id is None or not isinstance(current, dict):
            continue
        known, seen[camera_id] = seen.get(camera_id), current
        for kind, stamp in current.items() if known is not None else ():
            # Google repeats a detection while it lasts. One message per half minute is enough.
            if kind in settings["events"] and known.get(kind) != stamp and time.monotonic() >= quiet.get((camera_id, kind), 0):
                quiet[camera_id, kind] = time.monotonic() + 30
                send(kind, name, KINDS[kind] % name, name)


def health_problems():
    """What is wrong right now: for each problem the message, and the message for when it is over."""
    problems, bridge, link = {}, read(BRIDGE), google_link()
    if link in ("revoked", "client"):
        problems["google"] = ("Google accepteert de koppeling van NestUnifi niet meer. De camera's krijgen geen beeld. "
                              "Open de beheerpagina om de koppeling te herstellen.", "De koppeling met Google werkt weer.")
    expires = link_expiry()
    if link == "ok" and expires and 0 < expires - time.time() < 2 * 86400:
        problems["expiring"] = ("De koppeling met Google verloopt binnen twee dagen, omdat de inlog-app in Google Cloud nog "
                                "op Testing staat. Kies daar Publish app en koppel daarna opnieuw op de beheerpagina.", None)
    if events_state(bridge) in ("denied", "missing", "key"):
        problems["events"] = ("NestUnifi kan het meldingskanaal van Google niet lezen. Bewegingen komen niet aan in Protect. "
                              "Open de beheerpagina om het te herstellen.", "Het meldingskanaal van Google werkt weer.")
    state = read(STATE)
    video = sessions(state)
    for cam in read(ONVIF)["devices"]:
        if cam.get("paused"):
            continue
        # The bridge names a state for every camera it runs. Anything but video counts once it lasts.
        if (not state["cameras"].get(cam["uuid"], {}).get("online")
                or video.get(stream_path(cam), {}).get("state") not in (None, "live", "renewing")):
            problems["camera:" + cam["uuid"]] = (display(cam) + " geeft geen beeld.", display(cam) + " geeft weer beeld.")
    update = newer_version()
    if update:
        problems["update:" + update] = ("NestUnifi %s is beschikbaar. Draai sh install.sh opnieuw om bij te werken." % update, None)
    return problems


def watch_health(state, problems, send, now):
    """Report each problem once it has lasted, and report its end."""
    seen = state.setdefault("problems", {})
    for key, (text, _) in problems.items():
        entry = seen.setdefault(key, {"since": now, "reported": False})
        entry["over"] = problems[key][1]
        if not entry["reported"] and now - entry["since"] >= HOLD:
            entry["reported"] = True
            send("problem", "NestUnifi", text)
    for key in seen.keys() - problems.keys():
        entry = seen.pop(key)
        if entry["reported"] and entry["over"]:
            send("resolved", "NestUnifi", entry["over"])


def watch():
    """Runs beside the request handler and turns detections and problems into webhook messages."""
    state, checked, failure = {}, 0.0, None
    while True:
        time.sleep(2)
        try:
            settings = notices()
            def send(event, title, text, camera_name=None):
                try:
                    if settings["url"]:
                        send_notice(settings["url"], event, title, text, camera_name)
                except (OSError, ValueError, http.client.HTTPException):
                    print("NestUnifi: de webhook nam een melding niet aan", file=sys.stderr)
            watch_events(state, settings, send)
            if not (settings["url"] and settings["health"]):
                state.pop("problems", None)
            elif STATE.exists() and time.monotonic() - checked >= 60:
                checked = time.monotonic()
                watch_health(state, health_problems(), send, checked)
            failure = None
        except Exception as exc:
            if type(exc) is not failure:  # Once per kind of failure, not every two seconds.
                print("NestUnifi meldingen:", type(exc).__name__, file=sys.stderr)
            failure = type(exc)


def diagnose():
    """What someone who helps needs to see: versions, states and the latest log lines. Passwords, keys, tokens and the
    webhook address stay out, also where a service printed one."""
    bridge, media, state = read(BRIDGE), read(MEDIA), status()
    hidden = {bridge["google"].get("client_secret"), media.get("hlsCDNSecret"), notices()["url"],
              urlsplit(bridge["media"]["rtsp_base_url"]).password}
    hidden.update(u.get("pass") for u in media["authInternalUsers"])
    hidden.update(c["auth"].get("password") for c in read(ONVIF)["devices"])
    for path in (TOKENS, BRIDGE.parent / KEY_FILE):
        if path.exists():
            hidden.update(v for k, v in read(path).items() if k in ("access_token", "refresh_token", "private_key", "private_key_id"))
    hidden = sorted((s for s in hidden if isinstance(s, str) and len(s) >= 6), key=len, reverse=True)
    lines = []
    for path in (LOG.with_suffix(".old"), LOG):
        if path.exists():
            lines += path.read_text(errors="replace").splitlines()
    log = []
    for line in lines[-300:]:
        for secret in hidden:
            line = line.replace(secret, "***")
        log.append(re.sub(r"://[^/@\s]+@", "://***@", line)[:300])
    setup_now = setup_state("")
    supervisor = read(STATE)
    return {"nestunifi": "diagnose", "version": VERSION, "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "services": {k: supervisor.get(k) for k in ("bridge", "idle", "media", "web")},
            "google": state["google"], "link_expires": state["link_expires"], "events": state["events"],
            "update": state["update"], "setup": {k: setup_now[k] for k in ("client", "cloud", "linked", "cameras")},
            "notify": {"on": bool(notices()["url"]), "events": notices()["events"], "health": notices()["health"]},
            "cameras": [{k: v for k, v in cam.items() if k not in ("id", "username", "still")} for cam in state["cameras"]],
            "log": log}


def backup():
    """Everything needed to move to another system, Google link included. The admin password stays behind."""
    return {"nestunifi": 1, "version": VERSION,
            "files": {name: read(path) for name, (path, _) in BACKUP.items() if path.exists()}}


def restored(files):
    """The three configuration files of a backup, rebuilt from the fields NestUnifi writes itself. Anything else in
    the file, such as a media server hook that runs a command, stays out. A malformed file raises."""
    bridge, media, onvif = files["bridge/config.yaml"], files["media/mediamtx.yml"], files["onvif/config.yaml"]
    google = {k: v for k, v in bridge["google"].items() if k in GOOGLE_KEYS}
    base = urlsplit(bridge["media"]["rtsp_base_url"])
    cameras = [dict({"device_id": c["device_id"], "name": c["name"], "audio": bool(c.get("audio")),
                     "onvif": {"ip": c["onvif"]["ip"], "mac": c["onvif"]["mac"]}},
                    **({"event": {"linger": c["event"].get("linger", "60s")}} if c.get("event") is not None else {}),
                    **({"paused": True} if c.get("paused") is True else {}))
               for c in bridge["cameras"]]
    users = [dict({"user": u["user"], "pass": u["pass"],
                   "permissions": [{"action": r["action"], "path": r["path"]} for r in u["permissions"]]},
                  **({"ips": list(u["ips"])} if "ips" in u else {}))
             for u in media["authInternalUsers"]]
    paths = {path: {} for path in media["paths"]}
    devices = [{k: v for k, v in c.items() if k in DEVICE_KEYS} for c in onvif["devices"]]
    text = lambda *values: all(isinstance(v, str) for v in values)
    published = {"cam-" + slug(c["name"]) for c in cameras}
    if (not text(*google.values()) or google.get("service_account_key", KEY_FILE) != KEY_FILE
            or (base.scheme, base.hostname, base.port) != ("rtsp", "127.0.0.1", 8554)
            or not all(text(c["device_id"], c["name"], c["onvif"]["mac"], c.get("event", {}).get("linger", ""))
                       and re.fullmatch(r"127\.0\.0\.\d{1,3}", c["onvif"]["ip"]) for c in cameras)
            or not all(text(u["user"], u["pass"], *u.get("ips", [])) and all(r["action"] in ("read", "publish") and text(r["path"])
                                                         for r in u["permissions"]) for u in users)
            or not all(re.fullmatch(r"cam-[a-z0-9-]+", path) for path in paths)
            or not all(c.get("paused", True) is True for c in devices)
            or {stream_path(c) for c in devices} != published or not published <= paths.keys()):
        raise TypeError
    return (dict(read(BRIDGE), google=google, media={"rtsp_base_url": base.geturl()}, cameras=cameras,
                 events={"onvif": bool(bridge.get("events", {}).get("onvif"))}),
            dict(read(MEDIA), authInternalUsers=users, paths=paths), {"devices": devices})


def restore(req):
    """Put a backup on an installation that has no cameras yet."""
    data = req.get("backup")
    files = data.get("files") if isinstance(data, dict) and data.get("nestunifi") == 1 else None
    required = {"bridge/config.yaml", "media/mediamtx.yml", "onvif/config.yaml"}
    if not isinstance(files, dict) or not required <= files.keys() <= BACKUP.keys():
        raise Problem("Dit bestand is geen back-up van NestUnifi.")
    if read(BRIDGE)["cameras"] or read(ONVIF)["devices"]:
        raise Problem("Terugzetten kan alleen op een installatie zonder camera's.")
    try:
        if not all(isinstance(f, dict) for f in files.values()):
            raise TypeError
        files = dict(files, **dict(zip(("bridge/config.yaml", "media/mediamtx.yml", "onvif/config.yaml"), restored(files))))
        if "admin/notify.json" in files:
            files["admin/notify.json"] = notice_settings(files["admin/notify.json"])
    except (KeyError, TypeError, AttributeError, ValueError, Problem):
        raise Problem("Deze back-up is onvolledig of beschadigd.")
    replace(PENDING, {"action": "restore", "camera": None,
                      "before": {str(path): read(path) if path.exists() else None for path, _ in BACKUP.values()}})
    try:
        for name, (path, uid) in BACKUP.items():
            if name in files:
                replace(path, files[name], uid)
            else:
                path.unlink(missing_ok=True)
        ready()
        PENDING.unlink()
    except Exception:
        recover()
        raise
    link_check["until"] = 0.0
    return setup_state(req.get("origin") or "")


def password_hash(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 600000).hex()


def access(action, req):
    """The admin password. The web process keeps the sessions; only this process can read the stored hash.

    Deleting the file lets the next visitor choose a new password.
    """
    known = read(AUTH) if AUTH.exists() else None
    if action == "auth":
        return {"claimed": known is not None}
    password = req.get("password")
    usable = isinstance(password, str) and 10 <= len(password) <= 128
    if action == "claim":
        if known:
            raise Problem("Er is al een beheerwachtwoord ingesteld. Meld je daarmee aan.")
        if not usable:
            raise Problem("Kies een wachtwoord van 10 tot 128 tekens.")
        salt = secrets.token_hex(16)
        replace(AUTH, {"salt": salt, "hash": password_hash(password, salt)})
        return {}
    if known is None:
        raise Problem("Er is nog geen beheerwachtwoord ingesteld. Vernieuw de pagina.")
    wait = attempts["until"] - time.monotonic()
    if wait > 0:
        raise Problem("Te veel pogingen. Probeer het over %d seconden opnieuw." % (wait + 1))
    if not usable or not hmac.compare_digest(password_hash(password, known["salt"]), known["hash"]):
        # After five misses every further miss costs a minute, whoever is guessing.
        attempts["failed"] += 1
        if attempts["failed"] >= 5:
            attempts["until"] = time.monotonic() + 60
        raise Problem("Onjuist wachtwoord.")
    attempts["failed"] = 0
    return {}


def dispatch(req):
    action = req.get("action")
    if action in ("auth", "claim", "login"):
        return access(action, req)
    if action == "list":
        return status()
    if action == "discover":
        return available()
    if action == "test":
        return test_stream(req)
    if action == "view":
        cam = camera(read(ONVIF), req.get("id"))
        if source_live(cam) is False:
            raise Problem("De bridge ontvangt geen video van " + display(cam) +
                          ". Controleer het livebeeld in Google Home en probeer het opnieuw.")
        return {"url": "/live/" + stream_path(cam) + "/index.m3u8"}
    if action in ("add", "rename", "profile", "password", "remove", "pause"):
        return change(action, req)
    if action == "diagnose":
        return diagnose()
    if action in ("setup", "client", "project", "consent", "code"):
        return setup(action, req)
    if action == "motiontest":
        return motion_test(req)
    if action in ("notify", "notifytest"):
        return notify(action, req)
    if action == "backup":
        return backup()
    if action == "restore":
        return restore(req)
    raise Problem("Onbekende actie.")


class Request(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            self.request.settimeout(8)
            line = self.rfile.readline(MAX_REQUEST + 1)
            if len(line) > MAX_REQUEST:
                raise Problem("Verzoek is te groot.")
            with (PENDING.parent / "admin.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                recover()
                result = dispatch(json.loads(line))
            answer = {"ok": True, "result": result}
        except Problem as exc:
            answer = {"ok": False, "error": str(exc)}
        except Exception as exc:
            print("NestUnifi beheer:", type(exc).__name__, file=sys.stderr)
            answer = {"ok": False, "error": "Actie mislukt. Controleer de Google-koppeling en probeer het opnieuw."}
        self.wfile.write(json.dumps(answer).encode()+b"\n")


if __name__ == "__main__":
    os.umask(0o077)
    bootstrap()
    recover()
    CONTROL.parent.mkdir(mode=0o711, exist_ok=True)
    CONTROL.parent.chmod(0o711)
    CONTROL.unlink(missing_ok=True)
    threading.Thread(target=watch, daemon=True).start()
    with socketserver.UnixStreamServer(str(CONTROL), Request) as server:
        os.chown(CONTROL, 10004, 10004)
        os.chmod(CONTROL, 0o600)
        server.serve_forever()
