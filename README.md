# NestUnifi

NestUnifi zet Google Nest-camera's, -deurbellen en Nest Hubs in UniFi Protect. Elke camera verschijnt in je
netwerk als een eigen ONVIF-camera met een eigen adres, die je in Protect adopteert zoals elke andere camera.

## Wat je nodig hebt

- Een Linux-systeem dat altijd aan staat, met Docker Engine en Docker Compose v2. Een NAS, mini-pc of
  Raspberry Pi 4 of 5 met een 64-bits systeem voldoet (amd64 of arm64).
- Een **bedrade** netwerkaansluiting in hetzelfde netwerk als UniFi Protect. Via wifi werkt het niet, en ook
  niet met Docker Desktop op macOS of Windows.
- Nest-apparaten met vaste voeding in Google Home, onder een gewoon Google-account (geen Workspace).
- Eenmalig US$ 5 voor Google Device Access. Google vraagt dat bedrag zelf, bij het instellen.

## Installeren

Haal de twee bestanden op in een eigen map en start het script:

```sh
mkdir nestunifi && cd nestunifi
curl -fsSLO https://raw.githubusercontent.com/rvbcrs/NestUnifi/main/compose.yaml
curl -fsSLO https://raw.githubusercontent.com/rvbcrs/NestUnifi/main/install.sh
sh install.sh
```

Het script zoekt de netwerkinterface op, downloadt NestUnifi en start het. Aan het eind staat het adres van de
beheerpagina, bijvoorbeeld `http://192.168.1.10:8780/`. Open dat adres in je browser. De pagina leidt je in vijf
stappen door de koppeling met Google en het toevoegen van je camera's.

Liever met de hand, of via het beheerscherm van je NAS? Maak naast `compose.yaml` een bestand `.env` met de
naam van je bedrade interface en start het project:

```sh
echo "NESTUNIFI_LAN_INTERFACE=eth0" > .env
docker compose up -d
```

De naam van de interface vind je met `ip -4 route show default`, achter het woord `dev`.

## Instellingen in `.env`

| Naam | Betekenis | Standaard |
| --- | --- | --- |
| `NESTUNIFI_LAN_INTERFACE` | Bedrade netwerkinterface van dit systeem | wordt door `install.sh` ingevuld |
| `NESTUNIFI_PORT` | Poort van de beheerpagina | `8780` |
| `NESTUNIFI_HOSTNAMES` | Hostnamen waarmee je de pagina opent, gescheiden door komma's | leeg: alleen via het IP-adres |
| `NESTUNIFI_IMAGE` | Het image dat gestart wordt, bijvoorbeeld een vaste versie zoals `ghcr.io/rvbcrs/nestunifi:2026.11.25` | `ghcr.io/rvbcrs/nestunifi:latest` |

## Goed om te weten

- **De beheerpagina heeft geen wachtwoord.** Ze werkt alleen vanaf je eigen netwerk of VPN en weigert bezoekers
  vanaf internet. Zet de poort niet open in je router.
- **Vaste adressen.** Elke camera vraagt zelf een adres aan bij je router. Leg die adressen in je router vast,
  zodat Protect de camera's na een herstart terugvindt.
- **Dit systeem zelf bereikt de camera-adressen niet.** Dat hoort bij deze netwerktechniek. Andere apparaten
  in je netwerk, zoals je UniFi-recorder, bereiken ze wel.
- **Gebruik je al een macvlan-netwerk in Docker op dezelfde interface** (bijvoorbeeld voor Pi-hole)? Docker
  staat er per interface één toe. Vervang dan in `compose.yaml` onderaan het blok onder `lan:` door
  `external: true` en `name: <naam van je bestaande netwerk>`. Een ipvlan-netwerk is niet geschikt.
- **Je gegevens** staan in de map `private/`, inclusief de koppeling met Google. Neem die map mee in je back-up
  en deel haar met niemand.
- **Bijwerken:** draai `sh install.sh` opnieuw. Het haalt de nieuwste versie op; je instellingen blijven staan.

## Zelf bouwen

Wie de broncode aanpast, bouwt het image uit deze map in plaats van het te downloaden:

```sh
docker compose -f compose.yaml -f compose.build.yaml up -d --build
```
