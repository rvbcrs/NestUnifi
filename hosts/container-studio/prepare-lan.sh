#!/bin/sh
set -eu
engine=container-studio-engine
parent=nest-camera-lan
export DOCKER_HOST=unix:///run/container-studio/docker.sock
docker=/volume1/@container-studio-engine/bin/docker

until ip netns exec "$engine" true 2>/dev/null; do sleep 1; done
if ! ip -n "$engine" link show dev "$parent" >/dev/null 2>&1; then
    ip link add link eth0 name "$parent" type macvlan mode bridge
    trap 'ip link del dev "$parent" 2>/dev/null || true' EXIT
    ip link set dev "$parent" netns "$engine"
    trap - EXIT
fi
ip -n "$engine" link set dev "$parent" up
until "$docker" info >/dev/null 2>&1; do sleep 1; done
# Restore the suite if Docker tried to attach its LAN before this interface existed.
id=$("$docker" ps -aq --filter label=com.docker.compose.project=nestunifi --filter label=com.docker.compose.service=suite --filter status=exited --filter status=created)
if [ -n "$id" ]; then "$docker" start "$id" >/dev/null; fi
