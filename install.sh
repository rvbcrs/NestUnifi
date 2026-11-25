#!/bin/sh
# Start NestUnifi on this Linux system. Safe to run again: it keeps .env and all settings.
set -eu
cd "$(dirname "$0")"

stop() { echo "$1" >&2; exit 1; }
field() { awk -v key="$1" '{for (i = 1; i < NF; i++) if ($i == key) {print $(i + 1); exit}}'; }

command -v docker >/dev/null 2>&1 || stop "Docker ontbreekt. Installeer Docker Engine en start dit script opnieuw."
docker compose version >/dev/null 2>&1 || stop "Docker Compose v2 ontbreekt (het commando 'docker compose')."
docker info >/dev/null 2>&1 || stop "Docker is niet bereikbaar. Start Docker, of draai dit script met sudo."

if ! grep -q '^NESTUNIFI_LAN_INTERFACE=' .env 2>/dev/null; then
    lan=$(ip -4 route show default | field dev)
    [ -n "$lan" ] || stop "Geen netwerkinterface met internetroute gevonden. Zet NESTUNIFI_LAN_INTERFACE zelf in .env."
    # An access point only accepts the one MAC address it knows, so cameras cannot get their own over Wi-Fi.
    if [ -e "/sys/class/net/$lan/wireless" ]; then
        stop "Dit systeem gebruikt wifi ($lan). NestUnifi heeft een bedrade netwerkaansluiting nodig."
    fi
    echo "NESTUNIFI_LAN_INTERFACE=$lan" >> .env
    echo "Netwerkinterface: $lan (vastgelegd in .env)"
fi

# Running this script again also updates NestUnifi to the newest published version.
docker compose pull || echo "Downloaden is niet gelukt. Staat er al een versie op dit systeem, dan start NestUnifi daarmee." >&2
docker compose up -d

port=$(sed -n 's/^NESTUNIFI_PORT=//p' .env | tail -n 1)
address=$(ip -4 route get 1.1.1.1 | field src)
echo "NestUnifi draait. Open http://${address:-<IP-adres van dit systeem>}:${port:-8780}/ om het in te stellen."
