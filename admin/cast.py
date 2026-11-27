"""Find the cast devices on the network and have one play a stream, without Google's libraries.

A device answers a name query on the local network (mDNS) and takes orders over TLS on port 8009: each message is
four bytes of length and a small protobuf with a sender, a receiver, a namespace and a JSON text.
"""
import json
import socket
import ssl
import struct
import time

PLAYER = "CC1AD845"  # Google's own Default Media Receiver. It needs no registration.
SERVICE = "_googlecast._tcp.local"
CONNECTION, HEARTBEAT, RECEIVER, MEDIA = ("urn:x-cast:com.google.cast." + part for part in (
    "tp.connection", "tp.heartbeat", "receiver", "media"))


class Refused(Exception):
    """The device answered, and the answer was no."""


def name(packet, at):
    """The DNS name at this place and the place after it. A name can continue elsewhere in the packet."""
    labels, after, jumps = [], None, 0
    while True:
        size = packet[at]
        if size >= 192:
            after = after or at + 2
            at = (size & 63) << 8 | packet[at + 1]
            jumps += 1
            if jumps > 32:
                raise ValueError("name")
        elif size:
            labels.append(packet[at + 1:at + 1 + size].decode("utf-8", "replace"))
            at += 1 + size
        else:
            return ".".join(labels), after or at + 1


def devices(packet, address):
    """The cast devices with a screen that one answer tells of. A group of speakers is none."""
    questions, *counts = struct.unpack_from(">HHHH", packet, 4)
    at, texts = 12, {}
    for _ in range(questions):
        at = name(packet, at)[1] + 4
    for _ in range(sum(counts)):
        owner, at = name(packet, at)
        kind, size = struct.unpack_from(">H6xH", packet, at)
        at += 10
        if kind == 16 and owner.endswith(SERVICE):  # TXT: texts of the form key=value, each with its length before it.
            part, text = at, {}
            while part < at + size:
                key, _, value = packet[part + 1:part + 1 + packet[part]].decode("utf-8", "replace").partition("=")
                text[key] = value
                part += 1 + packet[part]
            texts[owner] = text
        at += size
    # "ca" holds what the device can do: 1 is showing video.
    return [{"name": text["fn"], "model": text.get("md", ""), "address": address} for text in texts.values()
            if text.get("fn") and text.get("ca", "").isdigit() and int(text["ca"]) & 1]


def find(source, wait=2.5):
    """Ask from this address of ours who is a cast device. A query from another port than 5353 is answered to
    the asker alone, so no other socket on this system is needed."""
    query = struct.pack(">HHHHHH", 0, 0, 1, 0, 0, 0) + b"".join(
        bytes([len(label)]) + label.encode() for label in SERVICE.split(".")) + b"\0" + struct.pack(">HH", 12, 1)
    found, target = {}, ("224.0.0.251", 5353)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind((source, 0))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(source))
        sock.settimeout(0.25)
        start = time.monotonic()
        sock.sendto(query, target)
        again = True
        while time.monotonic() - start < wait:
            if again and time.monotonic() - start >= 1:  # A query gets lost now and then.
                sock.sendto(query, target)
                again = False
            try:
                packet, sender = sock.recvfrom(9000)
                for device in devices(packet, sender[0]):
                    found[device["address"]] = device
            except (socket.timeout, ValueError, IndexError, struct.error):
                pass  # Nothing yet, or not an answer this reads.
    return sorted(found.values(), key=lambda device: device["name"].casefold())


def varint(value):
    out = b""
    while value > 127:
        out += bytes([value & 127 | 128])
        value >>= 7
    return out + bytes([value])


def pack(sender, receiver, namespace, data):
    text = lambda field, value: bytes([field << 3 | 2]) + varint(len(value.encode())) + value.encode()
    body = b"\x08\x00" + text(2, sender) + text(3, receiver) + text(4, namespace) + b"\x28\x00" + text(6, json.dumps(data))
    return struct.pack(">I", len(body)) + body


def unpack(body):
    """The namespace and the JSON of a message."""
    def number(at):
        value = shift = 0
        while True:
            value |= (body[at] & 127) << shift
            shift += 7
            at += 1
            if body[at - 1] < 128:
                return value, at
    at, texts = 0, {}
    while at < len(body):
        field, at = number(at)
        value, at = number(at)  # The value of a number, the length of a text.
        if field & 7 == 2:
            texts[field >> 3] = body[at:at + value]
            at += value
        elif field & 7:
            raise ValueError("field")
    return texts.get(4, b"").decode(), json.loads(texts.get(6) or b"{}")


class Device:
    def __init__(self, address, timeout=30):
        # Every cast device signs its own certificate, so there is nothing to check it against.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        self.conn = context.wrap_socket(socket.create_connection((address, 8009), timeout=8))
        self.until = time.monotonic() + timeout
        self.asked = 0
        self.send("receiver-0", CONNECTION, {"type": "CONNECT"})

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.conn.close()

    def send(self, receiver, namespace, data):
        self.conn.sendall(pack("sender-0", receiver, namespace, data))

    def take(self, size):
        data = b""
        while len(data) < size:
            self.conn.settimeout(max(0.1, self.until - time.monotonic()))
            part = self.conn.recv(size - len(data))
            if not part:
                raise OSError("closed")
            data += part
        return data

    def ask(self, receiver, namespace, data):
        """Send a request and give the answer to it. What the device says in between is not for us."""
        self.asked += 1
        self.send(receiver, namespace, dict(data, requestId=self.asked))
        while True:
            size = struct.unpack(">I", self.take(4))[0]
            if size > 1 << 20:
                raise OSError("size")
            said, answer = unpack(self.take(size))
            if said == HEARTBEAT and answer.get("type") == "PING":
                self.send("receiver-0", HEARTBEAT, {"type": "PONG"})
            elif answer.get("requestId") == self.asked:
                return answer

    def players(self):
        """Our player on this device, when it runs."""
        status = self.ask("receiver-0", RECEIVER, {"type": "GET_STATUS"}).get("status", {})
        return [app for app in status.get("applications", []) if app.get("appId") == PLAYER]


def play(address, url, title):
    """Have the device show a live stream. It goes on after this connection closes."""
    with Device(address) as device:
        answer = device.ask("receiver-0", RECEIVER, {"type": "LAUNCH", "appId": PLAYER})
        app = next((app for app in answer.get("status", {}).get("applications", []) if app.get("appId") == PLAYER), None)
        if app is None:
            raise Refused(answer.get("type", ""))
        device.send(app["transportId"], CONNECTION, {"type": "CONNECT"})
        answer = device.ask(app["transportId"], MEDIA, {"type": "LOAD", "autoplay": True, "media": {
            "contentId": url, "contentType": "application/x-mpegURL", "streamType": "LIVE",
            "metadata": {"metadataType": 0, "title": title}}})
        if answer.get("type") != "MEDIA_STATUS":
            raise Refused(answer.get("type", ""))


def stop(address):
    """End our player on the device. Anything else it shows is left alone."""
    with Device(address) as device:
        for app in device.players():
            device.ask("receiver-0", RECEIVER, {"type": "STOP", "sessionId": app["sessionId"]})
