# Container Studio op Synology

Alleen nodig op een NAS waar Docker via Container Studio draait. Die Docker-engine heeft een eigen
netwerkomgeving en ziet de netwerkpoorten van de NAS niet. Deze service maakt bij het opstarten een
interface `nest-camera-lan` op `eth0` aan en geeft die aan de engine.

1. Kopieer `prepare-lan.sh` naar `/usr/local/lib/nestunifi-discovery/` en
   `nestunifi-camera-lan.service` naar `/etc/systemd/system/`.
2. Zet de service aan: `systemctl enable --now nestunifi-camera-lan.service`.
3. Zet in `.env` naast `compose.yaml`: `NESTUNIFI_LAN_INTERFACE=nest-camera-lan`.
4. Start NestUnifi met de `docker compose` van Container Studio, zoals in de README in de hoofdmap.
