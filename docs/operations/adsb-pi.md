# ADS-B feeder Pi

The ADS-B receiver is a Raspberry Pi 4 Model B (4 GB, board revision 1.2) with a Realtek RTL2838 USB SDR dongle, on the Home VLAN as `adsb.lan.cynexia.net` (192.168.17.136, DHCP).
It is outside both clusters, so nothing in this repository renders or applies to it.
This page records what runs on it, which files carry the feeder identities, how to back it up, and how to rebuild it on a supported release.

Log in as `pi` with the m4 default SSH key (the 1Password agent offers it): `ssh pi@adsb.lan.cynexia.net`.
The server allows public-key login only and disconnects after six rejected keys, so if the agent holds many keys, pin one with `-o IdentitiesOnly=yes -i <public key file>`.

## What runs on it

| Component | Installed from | Updates | Notes |
|---|---|---|---|
| readsb (decoder) | wiedehopf `readsb-install.sh` | re-run the script | Feeds every client on port 30005 |
| tar1090 (map) | wiedehopf script, pulled in by the above | re-run the script | Served by lighttpd on port 80; nothing else uses lighttpd |
| autogain1090 | wiedehopf `autogain-install.sh` | re-run the script | A systemd timer adjusts readsb's gain, so `/etc/default/readsb` carries `--gain -10` |
| piaware (FlightAware) | FlightAware apt repository, `piaware-repository` package | apt | MLAT on |
| fr24feed (Flightradar24) | Flightradar24 apt repository via wiedehopf `fr24-nopackage.sh` | its own cron in `/etc/cron.d/fr24feed_updater`, plus apt | MLAT off by configuration, so `fr24feed-status` always reports "MLAT: failed"; that is expected |
| ADS-B Exchange feed, MLAT and stats | `adsbexchange.com/feed.sh` and `stats.sh` | re-run the scripts | Its own tar1090 copy on port 8504 |
| pfclient (Plane Finder) | `.deb` from planefinder.net, no repository | manual | Web setup on port 30053 |
| unattended-upgrades | Debian | n/a | Its daily run is the heartbeat for the operator-owned healthchecks.io check named `adsb.cynexia.net` |

Status commands: `sudo piaware-status`, `sudo fr24feed-status`, `systemctl status adsbexchange-feed adsbexchange-mlat pfclient readsb`, and `python3 -c 'import json;print(len(json.load(open("/run/readsb/aircraft.json"))["aircraft"]))'` for the live aircraft count.

The Pi does no DNS validation of its own; it asks OPNsense, which validates DNSSEC.
Name resolution drops out for under a minute every few days and every feeder reconnects by itself, so a single "Name resolution failed" line in a feeder log is not a fault.

## Feeder identities

These files are the station identities. Losing one means a new station on that site and the loss of its history.

| Site | File | Setting |
|---|---|---|
| FlightAware | `/var/cache/piaware/feeder_id` and `/etc/piaware.conf` | `feeder-id` |
| Flightradar24 | `/etc/fr24feed.ini` | `fr24key` (radar T-EGTR38) |
| ADS-B Exchange | `/usr/local/share/adsbexchange/adsbx-uuid` and `/etc/default/adsbexchange` | UUID, user, latitude, longitude, altitude |
| Plane Finder | `/etc/pfclient-config.json` | `sharecode`, latitude, longitude |
| readsb | `/etc/default/readsb` | latitude, longitude, max range |
| heartbeat | `/etc/apt/apt.conf.d/50unattended-upgrades` on the old build; the systemd drop-in below on a rebuilt one | the healthchecks.io ping URL |

The Flightradar24 key, the Plane Finder sharecode and the ping URL are spam-target identifiers: keep them out of this repository and treat a backup folder holding them as private. They need no rotation if they appear in a transcript.

## Backup

Two archives, both pulled over SSH, nothing installed on the Pi.
The Pi compresses at about 2 MB/s, so let the Mac do the compression; the full archive then takes about 80 seconds.

```
B=~/adsb-pi-backup-$(date +%F) && mkdir -p "$B"
ssh pi@adsb.lan.cynexia.net 'sudo tar czf - --ignore-failed-read /var/cache/piaware/feeder_id /etc/piaware.conf /etc/fr24feed.ini /usr/local/share/adsbexchange/adsbx-uuid /etc/default/adsbexchange /etc/pfclient-config.json /etc/default/readsb /etc/default/tar1090 /etc/hostname /home/pi/.ssh/authorized_keys /boot/config.txt /etc/apt/apt.conf.d/50unattended-upgrades' > "$B/adsb-identities.tar.gz"
ssh pi@adsb.lan.cynexia.net 'sudo tar cf - --one-file-system --warning=no-file-changed --ignore-failed-read --exclude=/var/cache/apt/archives --exclude=/var/log/journal / /boot' | gzip -1 > "$B/adsb-full-fs.tar.gz"
gzip -t "$B/adsb-full-fs.tar.gz" && tar -tzf "$B/adsb-full-fs.tar.gz" | wc -l
```

The full archive is a file archive, not a block image: it is readable on the Mac with `tar -xzf <archive> etc/fr24feed.ini`, and it cannot boot a Pi.
The 2026-10-14 backup of the bullseye build is in `~/adsb-pi-backup-2026-10-14/` on the MacBook Pro.

## Rebuild on a supported release

Raspberry Pi OS does not support an in-place jump between Debian releases, so a new release is a fresh card and a reinstall.
The build retired in October 2026 ran Raspbian 11 (bullseye), whose security support ended on 2026-08-31.
Use a new card and keep the old one until step 9 passes.

1. Take the backup above.
2. Flash Raspberry Pi OS Lite 64-bit (bookworm) with Raspberry Pi Imager: hostname `adsb`, user `pi`, SSH with the m4 public key, locale and timezone set.
3. Swap the card, boot, and confirm `ssh pi@adsb.lan.cynexia.net` works and the address is still 192.168.17.136.
4. `sudo apt update && sudo apt full-upgrade -y && sudo reboot`.
5. readsb then autogain, each as `sudo bash -c "$(wget -O - https://github.com/wiedehopf/adsb-scripts/raw/master/<script>)"` with `readsb-install.sh` (which installs tar1090 and lighttpd as well) and then `autogain-install.sh`. Restore latitude, longitude and `--max-range 450` in `/etc/default/readsb`, then check the map on port 80 shows aircraft.
6. piaware: install FlightAware's `piaware-repository` package for bookworm, `sudo apt install piaware`, then `sudo piaware-config feeder-id <saved id>`, `sudo piaware-config allow-mlat yes`, `sudo systemctl restart piaware`, and confirm `sudo piaware-status` says connected.
7. fr24feed: run `fr24-nopackage.sh` from the same wiedehopf repository, copy the saved `fr24key` into `/etc/fr24feed.ini` with `receiver="beast-tcp"`, `host="127.0.0.1:30005"` and `mlat="no"`, restart it, and confirm `fr24feed-status` says connected with radar T-EGTR38.
8. ADS-B Exchange: write the saved UUID to `/usr/local/share/adsbexchange/adsbx-uuid` first, then run `feed.sh` and `stats.sh` from adsbexchange.com with the saved user, latitude, longitude and altitude, and confirm `adsbexchange-feed` logs "Connection established".
9. Plane Finder: install the current `pfclient` arm64 `.deb` from planefinder.net, open port 30053, and enter the saved sharecode and coordinates. Confirm its stats page shows bytes going to the master server.
10. Heartbeat: replace the exim-and-mail arrangement with one systemd drop-in, so no mail system is needed. Write `/etc/systemd/system/apt-daily-upgrade.service.d/heartbeat.conf` with the ping URL from the backup, then `sudo systemctl daemon-reload`:

    ```
    [Service]
    ExecStopPost=/usr/bin/curl -fsS -m 10 --retry 3 -o /dev/null https://hc-ping.com/<uuid>/${EXIT_STATUS}
    ```

    healthchecks.io reads the trailing exit status, so a successful run pings `up` and a failed one pings `down`. Confirm with `sudo systemctl start apt-daily-upgrade.service` and a fresh ping on the check's page.
11. Add the two vendor repositories to `Unattended-Upgrade::Origins-Pattern` in `/etc/apt/apt.conf.d/50unattended-upgrades` so piaware and fr24feed update with everything else; the origin strings are in `apt-cache policy`.
12. Check each site's station page shows the same station receiving, NetAlertX shows the Pi with its old name and address, and the healthchecks.io check is green the next morning. Then put the old card in a drawer.

Expect the healthchecks.io check to miss one run on the day of the swap.
