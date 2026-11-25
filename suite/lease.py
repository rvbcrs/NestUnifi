#!/usr/bin/python3
"""BusyBox DHCP hook. Each LAN identity has its own source route, including VPN replies."""
import ipaddress
import os
import subprocess
import sys
from pathlib import Path

dev = os.environ["interface"]
runtime = Path("/run/leases") / dev
table = str(10000 + int((Path("/sys/class/net") / dev / "ifindex").read_text()))
priority = table

def ip(*args, check=True):
    subprocess.run(["ip", *args], check=check, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def clear():
    runtime.unlink(missing_ok=True)
    while subprocess.run(["ip", "rule", "del", "priority", priority], capture_output=True).returncode == 0:
        pass
    ip("route", "flush", "table", table, check=False)
    ip("-4", "addr", "flush", "dev", dev)

action = sys.argv[1]
if action == "deconfig":
    clear()
elif action in ("bound", "renew"):
    address = ipaddress.IPv4Interface(os.environ["ip"] + "/" + os.environ["mask"])
    gateway = (os.environ.get("router") or "").split()
    data = str(address.ip)
    previous = runtime.read_text() if runtime.exists() else ""
    if data != previous:
        clear()
    ip("addr", "replace", str(address), "dev", dev, "noprefixroute")
    ip("route", "replace", str(address.network), "dev", dev, "src", data, "table", table)
    ip("route", "replace", "239.255.255.250/32", "dev", dev, "src", data, "table", table)
    if gateway:
        ip("route", "replace", "default", "via", str(ipaddress.IPv4Address(gateway[0])), "dev", dev, "table", table)
    if data != previous:
        ip("rule", "add", "priority", priority, "from", data, "lookup", table)
        ip("rule", "add", "priority", priority, "oif", dev, "lookup", table)
    tmp = runtime.with_suffix(".tmp")
    tmp.write_text(data)
    tmp.replace(runtime)
