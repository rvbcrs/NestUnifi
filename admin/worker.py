"""Camera-management requests received over the private Unix socket."""
import base64
import hashlib
import hmac
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
import time
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
attempts = {"failed": 0, "until": 0.0}
ONVIF = PRIVATE / "onvif/config.yaml"
MEDIA = PRIVATE / "media/mediamtx.yml"
PENDING = PRIVATE / "admin/pending.json"
STATE = Path("/run/suite-status.json")
CONTROL = Path("/run/control/control.sock")
ORIGIN = "nestunifi:"
MAX_NAME = 48
FAMILIES = {"CAMERA": "Nest Cam", "DOORBELL": "Nest Doorbell", "DISPLAY": "Nest Hub"}


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


def status():
    onvif = read(ONVIF)
    state = read(STATE)
    def summary(cam):
        current = state["cameras"].get(cam["uuid"], {})
        addr = current.get("ip", "")
        healthy = current.get("online", False)
        profile = cam["streams"][0]
        return {"id": cam["uuid"], "name": display(cam), "ip": addr, "online": healthy,
                "username": cam["auth"]["username"], "kind": cam.get("kind", kind_from_name(cam["name"])),
                "model": cam["info"]["model"], "live": source_live(cam) if state["media"] else None,
                "width": profile["width"], "height": profile["height"]}
    with ThreadPoolExecutor(max_workers=6) as pool:
        cams = sorted(pool.map(summary, onvif["devices"]), key=lambda c: c["name"].casefold())
    return {"cameras": cams, "bridge": state["bridge"]}



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
        replace(Path(path), data)
    cam = None if job["action"] == "add" else camera(read(ONVIF), job["camera"]["uuid"])
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
        if action == "add":
            replace(BRIDGE, bridge)
        if action in ("add", "password"):
            replace(MEDIA, media)
        replace(ONVIF, onvif)
        ready(cam)
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
    return {"name": label}


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
        replace(SETUP, {k: v for k, v in progress.items() if k not in ("state", "kind")})
        try:
            token = google_api.exchange(google["client_id"], google["client_secret"], redirect, code)
            if kind == "cloud":
                key_path = BRIDGE.parent / KEY_FILE
                known = read(key_path).get("client_email") if key_path.exists() else None
                made = google_api.provision(token["access_token"], google["client_id"], known)
                if made["key"]:
                    replace(key_path, made["key"], 10001)
                enable_events(bridge, read(ONVIF)["devices"], made["subscription"], KEY_FILE)
                replace(BRIDGE, bridge)
                replace(SETUP, {"topic": made["topic"]})
            else:
                if not token.get("refresh_token"):
                    raise Problem("Google gaf geen blijvende toegang. Start de koppeling opnieuw en geef toestemming.")
                expiry = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + int(token.get("expires_in", 0))))
                replace(TOKENS, {"access_token": token["access_token"], "token_type": "Bearer",
                                 "refresh_token": token["refresh_token"], "expiry": expiry}, 10001)
                try:
                    inventory()  # Google completes the link, and starts events, on the first device listing.
                except subprocess.SubprocessError:
                    pass  # The camera step lists again and reports what is wrong.
        except google_api.Failure as exc:
            raise Problem(str(exc))
        except ValueError:
            raise Problem("De camera-instellingen passen niet meer bij elkaar. Meldingen zijn niet ingeschakeld.")
    return setup_state(origin)


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
    if action in ("add", "rename", "profile", "password"):
        return change(action, req)
    if action in ("setup", "client", "project", "consent", "code"):
        return setup(action, req)
    raise Problem("Onbekende actie.")


class Request(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            self.request.settimeout(8)
            line = self.rfile.readline(8193)
            if len(line) > 8192:
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
    with socketserver.UnixStreamServer(str(CONTROL), Request) as server:
        os.chown(CONTROL, 10004, 10004)
        os.chmod(CONTROL, 0o600)
        server.serve_forever()
