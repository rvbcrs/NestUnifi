"""Supervise the bridge, media server, admin page and independently addressed LAN cameras."""
import hashlib
import json
import os
import re
import signal
import socket
import subprocess
import time
from pathlib import Path

APP = Path("/app")
SOURCE = Path("private/onvif/config.yaml")
# The last address each camera had. After a restart it asks the router for the same one, so Protect finds it again.
ADDRESSES = Path("private/onvif/addresses.json")
BRIDGE = Path("/config/config.yaml")
TOKENS = Path("/config/tokens.json")
MEDIA = Path("/media-config/mediamtx.yml")
STATUS = Path("/run/suite-status.json")
LEASES = Path("/run/leases")
GROUP = "239.255.255.250"
stopping = False


def atomic(path, data, mode=0o600, group=None):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(data)
    if group is not None:
        os.chown(tmp, 0, group)
    tmp.chmod(mode)
    tmp.replace(path)


def stop(proc):
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=4)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def spawn(args, uid, **opts):
    return subprocess.Popen(args, user=uid, group=uid, extra_groups=[], **opts)


def ip(*args):
    subprocess.run(["ip", *args], check=True, stdout=subprocess.DEVNULL)


def lan_interface(links):
    """Docker numbers interfaces in the order it connects networks. Find the LAN by what it is: the one macvlan."""
    found = [link["ifname"] for link in links if link.get("linkinfo", {}).get("info_kind") == "macvlan"]
    if len(found) != 1:
        raise SystemExit("NestUnifi needs one macvlan network on the LAN and found %d. "
                         "Check NESTUNIFI_LAN_INTERFACE and the networks in compose.yaml." % len(found))
    return found[0]


def networks():
    """The address the admin page listens on, and the interface every camera gets its own MAC on."""
    read = lambda *args: json.loads(subprocess.check_output(["ip", "-j", *args]))
    routes = read("-4", "route", "show", "default")
    if not routes:
        raise SystemExit("NestUnifi has no default network. Keep the default network in compose.yaml next to the LAN.")
    lan = lan_interface(read("-d", "link", "show"))
    if lan != routes[0]["dev"]:
        ip("-4", "addr", "flush", "dev", lan)  # Docker's placeholder address. Cameras get theirs from DHCP.
    return read("-4", "addr", "show", "dev", routes[0]["dev"])[0]["addr_info"][0]["local"], lan


def discovery_socket(interface, address):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, interface.encode()+b"\0")
        sock.bind(("", 3702))
        addr = socket.inet_aton(address)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, socket.inet_aton(GROUP)+addr)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, addr)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)
        return sock
    except Exception:
        sock.close()
        raise


def remembered():
    try:
        known = json.loads(ADDRESSES.read_text())
        return known if isinstance(known, dict) else {}
    except (OSError, ValueError):
        return {}


def remember(mac, address):
    known = remembered()
    if known.get(mac) != address:
        atomic(ADDRESSES, json.dumps(dict(known, **{mac: address})))


def device_config(cam):
    device = cam.copy()
    device.pop("kind", None)
    name = device.pop("display_name", cam["name"])
    device.update(name=name, ports={"soap": 80, "rtsp": 554})
    return {"server": {"discovery": True}, "web": {"enabled": False}, "devices": [device]}


class Endpoint:
    def __init__(self, cam, motion_ip, lan):
        self.cam = cam
        self.lan = lan
        self.dev = "cam" + cam["mac"].lower().replace(":", "")
        self.uid = 20000 + cam["ports"]["soap"]
        self.path = Path("/run/endpoints") / (self.dev + ".json")
        self.lease = LEASES / self.dev
        self.hostname = "nest-" + re.sub("[^a-z0-9]+", "-", cam["name"].lower()).strip("-")
        self.dhcp = self.server = None
        self.address = ""
        self.online = False
        self.retry = 0
        self.configure(cam, motion_ip)

    def configure(self, cam, motion_ip):
        stop(self.server)
        self.server = None
        self.online = False
        self.retry = 0
        self.cam = cam
        self.motion_ip = motion_ip
        atomic(self.path, json.dumps(device_config(cam)), 0o440, self.uid)

    def tick(self):
        if not (Path("/sys/class/net") / self.dev).exists():
            ip("link", "add", "link", self.lan, "name", self.dev, "address", self.cam["mac"], "type", "macvlan", "mode", "bridge")
            ip("link", "set", self.dev, "up")
        mac = self.cam["mac"].lower()
        if self.dhcp is None or self.dhcp.poll() is not None:
            known = remembered().get(mac)
            request = ["-r", known] if isinstance(known, str) and re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", known) else []
            self.dhcp = subprocess.Popen(["udhcpc", "-f", "-i", self.dev, "-p", "/run/"+self.dev+".pid",
                                          "-s", "/app/lease.py", "-x", "hostname:"+self.hostname, *request],
                                         stdout=subprocess.DEVNULL)
        address = self.lease.read_text() if self.lease.exists() else ""
        if address and address != self.address:
            remember(mac, address)
        if address != self.address or self.server and self.server.poll() is not None:
            stop(self.server)
            self.server = None
            self.online = False
        self.address = address
        if not address or (not self.server and time.monotonic() < self.retry):
            return
        if not self.server:
            self.retry = time.monotonic() + 3
            with discovery_socket(self.dev, address) as sock:
                env = dict(os.environ, ONVIF_ADVERTISE_IP=address, DISCOVERY_FD=str(sock.fileno()),
                           NEST_MOTION_IP=self.motion_ip)
                self.server = spawn(["nest-camera", "-config", self.path], self.uid,
                                    env=env, pass_fds=(sock.fileno(),))
        if not self.online:
            try:
                with socket.create_connection((address, 80), timeout=.3):
                    self.online = True
            except OSError:
                pass

    def close(self):
        stop(self.server)
        stop(self.dhcp)
        if (Path("/sys/class/net") / self.dev).exists():
            subprocess.run(["/app/lease.py", "deconfig"], env=dict(os.environ, interface=self.dev), check=False)
            ip("link", "del", self.dev)
        self.path.unlink(missing_ok=True)


def loop():
    global stopping
    def finish(*_):
        global stopping
        stopping = True
    signal.signal(signal.SIGTERM, finish)
    signal.signal(signal.SIGINT, finish)
    LEASES.mkdir(mode=0o700, exist_ok=True)
    Path("/run/endpoints").mkdir(mode=0o711, exist_ok=True)
    Path("/run/endpoints").chmod(0o711)
    bridge = media = web = worker = None
    cams = {}
    prior_status = prior_source = bridge_version = None
    cameras = 0
    try:
        management, lan = networks()
        while not stopping:
            # The worker writes the configuration of a new installation before anything else can start.
            if worker is None or worker.poll() is not None:
                worker = subprocess.Popen(["python3", "-IB", "/app/worker.py"])
            if not (BRIDGE.exists() and MEDIA.exists() and SOURCE.exists()):
                time.sleep(1)
                continue
            if media is None or media.poll() is not None:
                media = spawn(["mediamtx", "/media-config/mediamtx.yml"], 10002,
                              env=dict(os.environ, MTX_RTSPADDRESS="127.0.0.1:8554", MTX_HLSADDRESS="127.0.0.1:8888"))
            version = BRIDGE.stat().st_mtime_ns
            bridge_changed = version != bridge_version
            if bridge_changed:
                cameras = len(json.loads(BRIDGE.read_bytes())["cameras"])
                bridge_version = version
            # The bridge exits without a Google link or a camera. Until the wizard is done there is nothing to run.
            idle = not cameras or not TOKENS.exists()
            if idle or bridge_changed or bridge is None or bridge.poll() is not None:
                stop(bridge)
                bridge = None if idle else spawn(["nest-bridge", "-config=/config/config.yaml", "-tokens=/config/tokens.json", "serve"], 10001)
            if web is None or web.poll() is not None:
                # Only the admin page is reachable from outside, on the network Docker publishes the port from.
                web = spawn(["python3","-IB","/app/web.py"], 10004, env=dict(
                    os.environ, NESTUNIFI_LISTEN=management+":8080",
                    NESTUNIFI_HLS_KEY=json.loads(MEDIA.read_bytes()).get("hlsCDNSecret", "")))
            raw = SOURCE.read_bytes()
            if raw != prior_source or bridge_changed:
                devices = {c["uuid"]:c for c in json.loads(raw)["devices"]}
                config = json.loads(BRIDGE.read_bytes())
                motion_ips = {c["onvif"]["mac"].lower(): c["onvif"]["ip"] for c in config["cameras"]
                              if config.get("events", {}).get("onvif") and c.get("event") is not None}
                for key in cams.keys() - devices.keys():
                    cams.pop(key).close()
                for key, cam in devices.items():
                    motion_ip = motion_ips.get(cam["mac"].lower(), "")
                    if key not in cams:
                        cams[key] = Endpoint(cam, motion_ip, lan)
                    elif cams[key].cam != cam or cams[key].motion_ip != motion_ip:
                        cams[key].configure(cam, motion_ip)
                prior_source = raw
            for endpoint in cams.values():
                try:
                    endpoint.tick()
                except (OSError, subprocess.SubprocessError) as e:
                    endpoint.online = False
                    print("Camera", endpoint.hostname, type(e).__name__, flush=True)
            status = {"revision":hashlib.sha256(raw).hexdigest(), "bridge":bridge is not None and bridge.poll() is None,
                      "idle":idle, "media":media.poll() is None, "web":web.poll() is None, "bridge_version":bridge_version,
                      "cameras":{k:{"online":c.online, "ip":c.address} for k,c in cams.items()}}
            if status != prior_status:
                atomic(STATUS, json.dumps(status))
                prior_status = status
            time.sleep(1)
    finally:
        for endpoint in cams.values():
            endpoint.close()
        for proc in (web, worker, bridge, media):
            stop(proc)


if __name__ == "__main__":
    loop()
