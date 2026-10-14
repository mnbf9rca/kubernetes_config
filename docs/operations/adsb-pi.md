# ADS-B feeder Pi

The ADS-B receiver is a Raspberry Pi 4 Model B (4 GB, board revision 1.2) with a Realtek RTL2838 USB SDR dongle, on the Home VLAN as `adsb.lan.cynexia.net` (192.168.17.136, DHCP, wired only; the Wi-Fi radio is disabled).
It is outside both clusters, so nothing in this repository renders or applies to it.
This page records what runs on it, which files carry the feeder identities, how to back it up, and how to rebuild it on a supported release.

Log in as `pi` with the m4 default SSH key (the 1Password agent offers it): `ssh pi@adsb.lan.cynexia.net`.
The server allows public-key login only and disconnects after six rejected keys, so if the agent holds many keys, pin one with `-o IdentitiesOnly=yes -i <public key file>`.
`pi` has passwordless sudo through `/etc/sudoers.d/010_pi-nopasswd`.

## What runs on it

| Component | Installed from | Updates | Notes |
|---|---|---|---|
| readsb (decoder) | wiedehopf `readsb-install.sh`, built from source | re-run the script | Feeds every client on port 30005; gain is readsb's built-in `--gain auto` |
| tar1090 (map) | wiedehopf script, pulled in by the above | re-run the script | Served by lighttpd at `http://adsb.lan.cynexia.net/tar1090/`; the site root is only lighttpd's placeholder page, and nothing else uses lighttpd |
| piaware (FlightAware) | FlightAware apt repository, `flightaware-apt-repository` package | apt | MLAT on |
| fr24feed (Flightradar24) | Flightradar24 apt repository with its 2026 signing key | apt | MLAT off by configuration, so `fr24feed-status` always reports "MLAT: failed"; that is expected |
| ADS-B Exchange feed, MLAT, stats and map | `adsbexchange.com` scripts: `feed-update.sh`, `stats.sh`, `install-or-update-interface.sh` | re-run the scripts | Its own tar1090 copy on port 8504; the feed installer also installs chrony in place of systemd-timesyncd |
| pfclient (Plane Finder) | `.deb` from `client.planefinder.net`, no repository | manual | Web setup at `http://adsb.lan.cynexia.net:30053` |
| unattended-upgrades | Debian | n/a | Its daily run drives the heartbeat for the operator-owned healthchecks.io check named `adsb.cynexia.net`, through the systemd drop-in in step 10 |

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
| heartbeat | `/etc/systemd/system/apt-daily-upgrade.service.d/heartbeat.conf` (on the retired bullseye build it was the `Mail` line of `/etc/apt/apt.conf.d/50unattended-upgrades`) | the healthchecks.io ping URL |

The Flightradar24 key, the Plane Finder sharecode and the ping URL are spam-target identifiers: keep them out of this repository and treat a backup folder holding them as private. They need no rotation if they appear in a transcript.

## Backup

Two archives, both pulled over SSH, nothing installed on the Pi.
The Pi compresses at about 2 MB/s, so let the Mac do the compression; the full archive then takes about 80 seconds.

```
B=~/adsb-pi-backup-$(date +%F) && mkdir -p "$B"
ssh pi@adsb.lan.cynexia.net 'sudo tar czf - --ignore-failed-read /var/cache/piaware/feeder_id /etc/piaware.conf /etc/fr24feed.ini /usr/local/share/adsbexchange/adsbx-uuid /etc/default/adsbexchange /etc/pfclient-config.json /etc/default/readsb /etc/default/tar1090 /etc/hostname /home/pi/.ssh/authorized_keys /boot/firmware/config.txt /etc/systemd/system/apt-daily-upgrade.service.d/heartbeat.conf' > "$B/adsb-identities.tar.gz"
ssh pi@adsb.lan.cynexia.net 'sudo tar cf - --one-file-system --warning=no-file-changed --ignore-failed-read --exclude=/var/cache/apt/archives --exclude=/var/log/journal / /boot/firmware' | gzip -1 > "$B/adsb-full-fs.tar.gz"
gzip -t "$B/adsb-full-fs.tar.gz" && tar -tzf "$B/adsb-full-fs.tar.gz" | wc -l
```

After running it, list the identities archive and check every file named above is in it; the 2026-10-14 identities archive was taken before the heartbeat file was on the list, so its ping URL is only in the full archive, at `etc/apt/apt.conf.d/50unattended-upgrades`.
The full archive is a file archive, not a block image: it is readable on the Mac with `tar -xzf <archive> etc/fr24feed.ini`, and it cannot boot a Pi.
The 2026-10-14 backup of the bullseye build is in `~/adsb-pi-backup-2026-10-14/` on the MacBook Pro.

## Rebuild on a supported release

Raspberry Pi OS does not support an in-place jump between Debian releases, so a new release is a fresh card and a reinstall.
The build retired in October 2026 ran Raspbian 11 (bullseye), whose security support ended on 2026-08-31; its replacement runs Raspberry Pi OS on Debian 13 (trixie), 64-bit, and every component below installs on it.
Use a new card and keep the old one until step 12 passes.
Install each vendor package before restoring its config file: `fr24feed` and `pfclient` ship their config as a conffile, and a file restored first makes dpkg stop and ask.

1. Take the backup above.
2. Flash Raspberry Pi OS Lite 64-bit (trixie) with Raspberry Pi Imager: hostname `adsb`, user `pi`, SSH with the m4 public key, locale and timezone set. Do not configure Wi-Fi.
3. Swap the card, boot, and confirm `ssh pi@adsb.lan.cynexia.net` works and the address is still 192.168.17.136. If Wi-Fi was configured anyway, run `sudo nmcli radio wifi off` from the wired address; two interfaces on one VLAN give NetAlertX a spurious second device and can flip the address to the Wi-Fi MAC.
4. `sudo apt update && sudo apt full-upgrade -y && sudo apt install -y unattended-upgrades curl git wget gnupg`, write `/etc/apt/apt.conf.d/20auto-upgrades` with `APT::Periodic::Update-Package-Lists "1";` and `APT::Periodic::Unattended-Upgrade "1";`, confirm `apt-config dump APT::Periodic::Unattended-Upgrade` prints `"1"`, then `sudo reboot`. The Lite image ships without unattended-upgrades, and without this step the Pi never patches itself while the heartbeat stays green.
5. readsb: `sudo bash -c "$(wget -O - https://github.com/wiedehopf/adsb-scripts/raw/master/readsb-install.sh)"`, which installs tar1090 and lighttpd as well. Restore latitude, longitude and `--max-range 450` in `/etc/default/readsb`, keep `--gain auto`, restart readsb, then check the map at `/tar1090/` on port 80 shows aircraft.
6. piaware: `wget https://www.flightaware.com/adsb/piaware/files/packages/pool/piaware/f/flightaware-apt-repository/flightaware-apt-repository_1.3_all.deb && sudo dpkg -i flightaware-apt-repository_1.3_all.deb && sudo apt update && sudo apt install -y piaware`, then `sudo piaware-config feeder-id <saved id>`, `sudo piaware-config allow-mlat yes`, `sudo systemctl restart piaware`, and confirm `sudo piaware-status` says connected and shows the saved feeder id.
7. fr24feed: the vendor's own install script still installs its SHA-1 signing key, which trixie's apt rejects, so add the repository by hand with the 2026 key. `sudo install -d -m 0755 /etc/apt/keyrings && wget -qO- https://repo-feed.flightradar24.com/flightradar24.2026.pub | sudo gpg --dearmor -o /etc/apt/keyrings/flightradar24.gpg`, write `/etc/apt/sources.list.d/fr24feed.list` with `deb [signed-by=/etc/apt/keyrings/flightradar24.gpg] https://repo-feed.flightradar24.com flightradar24 raspberrypi-stable`, then `sudo apt update && sudo apt install -y fr24feed`. Then copy the saved `fr24key` into `/etc/fr24feed.ini` with `receiver="beast-tcp"`, `host="127.0.0.1:30005"` and `mlat="no"`, restart it, and confirm `fr24feed-status` says connected with radar T-EGTR38.
8. ADS-B Exchange: restore `/usr/local/share/adsbexchange/adsbx-uuid` and `/etc/default/adsbexchange` from the backup first; the installer keeps an existing valid UUID and skips its dialogs when that defaults file is complete. Then `curl -L -o /tmp/axupdate.sh https://adsbexchange.com/feed-update.sh && sudo bash /tmp/axupdate.sh`, `curl -L -o /tmp/axstats.sh https://adsbexchange.com/stats.sh && sudo bash /tmp/axstats.sh`, and `sudo bash /usr/local/share/adsbexchange/git/install-or-update-interface.sh` (left on disk by the feed installer) for the port 8504 map. Confirm `adsbexchange-feed` logs "Connection established". Do not run `feed.sh`; it asks its questions in dialogs.
9. Plane Finder: `wget https://client.planefinder.net/pfclient_<current>_arm64.deb && sudo dpkg -i pfclient_<current>_arm64.deb`, then restore `/etc/pfclient-config.json` and `sudo systemctl restart pfclient`. Confirm `http://adsb.lan.cynexia.net:30053` shows bytes going to the master server.
10. Heartbeat: one systemd drop-in, no mail system. Write `/etc/systemd/system/apt-daily-upgrade.service.d/heartbeat.conf` with the ping URL, then `sudo systemctl daemon-reload`:

    ```
    [Service]
    ExecStopPost=/usr/bin/curl -fsS -m 10 --retry 3 -o /dev/null https://hc-ping.com/<uuid>/${EXIT_STATUS}
    ```

    The ping shows that the daily upgrade job ran and the Pi reached the internet. It does not show that the upgrade succeeded, because `apt.systemd.daily` exits 0 when `unattended-upgrade` fails; read `/var/log/unattended-upgrades/` for that. Confirm with `sudo systemctl start apt-daily-upgrade.service` and a fresh ping on the check's page.
11. Add `"site=apt.svc.flightaware.com";` and `"site=repo-feed.flightradar24.com";` inside the `Unattended-Upgrade::Origins-Pattern` block of `/etc/apt/apt.conf.d/50unattended-upgrades` so piaware and fr24feed update with everything else. Match on site, not origin: the Flightradar24 release file carries no Origin or Suite field. Every entry in that block is a quoted string ending in a semicolon; an unquoted line breaks every apt command on the box. Check with `sudo unattended-upgrade --dry-run -d 2>&1 | grep -i "allowed origins"`.
12. Check each site's station page shows the same station receiving, NetAlertX shows the Pi with its old name and address, and the healthchecks.io check is green the next morning. Then put the old card in a drawer.

Expect the healthchecks.io check to miss one run on the day of the swap.
