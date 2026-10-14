# NUT UPS Pi

`upsd` is a Raspberry Pi 4 Model B Rev 1.5 with 4 GB RAM at 10.100.0.4, outside both Kubernetes clusters.
It runs Raspberry Pi OS Lite 64-bit on Debian 12.15 (bookworm), installed from the 2023-12-11 image.
Debian bookworm receives [LTS support until 2028-06-30](https://www.debian.org/News/2026/20260712); that date does not promise support for Raspberry Pi's separate kernel packages.
The Pi polls the APC UPS at 10.100.0.6 over SNMP and serves its readings through Network UPS Tools (NUT).

## Access

Log in as `rob` with the `m3` key from the 1Password SSH agent.
The SSH server disconnects after six rejected keys, so pin the public key rather than offering the whole agent.
`rob` has passwordless sudo; user `pi` is refused.

```sh
export SSH_AUTH_SOCK="$HOME/Library/Group Containers/2BUA8C4S2C.com.1password/t/agent.sock"
ssh-add -L | awk '$NF == "m3"' > /private/tmp/nut-pi-m3.pub
ssh -o IdentitiesOnly=yes -i /private/tmp/nut-pi-m3.pub rob@10.100.0.4
```

`upsd.cynexia.net` did not resolve from the Mac during the 2026-10-14 check-in.
The Homepage card therefore uses the IP address.
`upsd.lan.cynexia.net` is not a usable alternative.

## What runs on it

| Component | Installed from | Updates | Notes |
|---|---|---|---|
| NUT server and client 2.8.0-7 | Debian `nut-server`, `nut-client` | unattended-upgrades | `MODE=netserver`; UPS name `apcups` |
| `snmp-ups` driver | `nut-server` | unattended-upgrades | `nut-driver@apcups.service`; `apcc` MIB; polls 10.100.0.6 every second |
| `upsd` | `nut-server` | unattended-upgrades | `nut-server.service`; listens on all IPv4 interfaces, TCP 3493 |
| `upsmon` | `nut-client` | unattended-upgrades | `nut-monitor.service`; authenticates as `monuser`, with the `primary` role |
| NUT CGI and Apache 2.4.68 | Debian `nut-cgi`, `apache2` | unattended-upgrades | Status page: `http://10.100.0.4/cgi-bin/nut/upsstats.cgi` |
| unattended-upgrades 2.9.1+nmu3 | Debian | apt | Daily package-list and upgrade timers; report mail drives the existing `upsd.cynexia.net` healthchecks.io check |
| Exim4 and bsd-mailx | Debian | unattended-upgrades | Local mail is retained through the diverted sendmail; no SMTP daemon was removed |

The other running services are SSH, NetworkManager, Avahi, Bluetooth, ModemManager, cron, systemd-timesyncd, triggerhappy and standard system services.
`/opt/pigpio` exists, but no pigpio service or listening port was found.
No PeaNUT, webNUT, container stack or additional application listener was found.
Apache serves its default page at `/`; the UPS page is under `/cgi-bin/nut/`.
The other listeners are SSH on TCP 22, loopback-only Exim on TCP 25, and Avahi on UDP 5353.

### Network access and UPS state

No host-level allowlist limits NUT readers: `upsd` binds `0.0.0.0:3493`, and the Pi's IPv4/IPv6 firewall policies accept traffic.
Apache's CGI directory has `Require all granted`.
Reachability therefore depends on the network firewall outside this Pi; that firewall was not inspected.
Connected NUT readers during check-in were localhost, 10.100.0.107 and Home Assistant at 192.168.17.102.
This is an observed client list, not an access-control list.

The only configured UPS is `apcups`, an APC Smart-UPS RT 1000 XL with two battery packs.
At check-in it reported `OL` (on line), 100% battery, 20% load, 16080 seconds runtime (4 hours 28 minutes), and 23.9°C.
Read-only telemetry and the CGI status page both worked.

```sh
upsc -l localhost
upsc apcups@localhost
systemctl status nut-server nut-monitor nut-driver@apcups
```

### Monitor authentication and kernel maintenance

Before repair, `/etc/nut/upsmon.conf` used username `admin`, while `/etc/nut/upsd.users` defined `[monuser]` with an invalid `admin master` directive.
Both login tests returned `ERR ACCESS-DENIED`, and `GET NUMLOGINS apcups` returned zero.
The operator approved correcting this on 2026-10-14.
The local monitor now uses `monuser` and its existing password, with the [documented `upsmon primary` role](https://networkupstools.org/historic/v2.8.0/docs/man/upsd.users.html) on the server.
All existing server passwords were preserved; neither remote client configuration changed.
After reloading both services, the same login assertion passed and `upsd` logged the local monitor login.
All three TCP clients remained connected and telemetry stayed fresh.
Working login and readings verify monitoring; no forced shutdown or UPS power-off test was performed.

On battery plus low battery, primary `upsmon` sets forced shutdown (FSD), waits up to `HOSTSYNC 15` seconds for logged-in secondary monitors, writes `/etc/killpower`, and halts the Pi with `/sbin/shutdown -h +0`.
The shutdown hook then runs `upsdrvctl shutdown`; [snmp-ups first attempts `shutdown.return`](https://github.com/networkupstools/nut/blob/v2.8.0/drivers/snmp-ups.c#L308-L340), requesting an output cut after the reported `ups.delay.shutdown` of 60 seconds and restoration when mains power returns.
NetAlertX identifies 10.100.0.107 as `pve3`; it and Home Assistant hold reader connections without monitor logins, so the primary does not wait for their shutdown, and pve3's physical power feed was not verified.
An output cut removes power from every device supplied by this UPS, potentially including a still-running pve3; the APC card's SNMP write access and actual load-off behavior were not tested.

At check-in, the Pi had run kernel `6.12.75+rpt-rpi-v8` for 180 days while `linux-image-rpi-v8` was `1:6.12.109-1+rpt1`.
It had 9.6 GB free on its 15 GB root filesystem and 3.5 GiB available RAM.
The operator approved one reboot after the authentication repair and heartbeat test.
After that reboot, the running kernel was `6.12.109+rpt-rpi-v8` and `/var/run/reboot-required` was absent.
NUT and Apache started successfully, `upsc` returned `OL`, and all three clients reconnected.
The CGI page answered with live UPS data, and current-boot logs contained no `ACCESS-DENIED`.
A reboot briefly interrupts the NUT readers at 10.100.0.107 and 192.168.17.102, as well as the local monitor.
Automatic reboot is enabled for 04:45 local time after future upgrades require it; the timer schedule is below.

## Identity and configuration files

These files preserve the UPS connection, client authentication, web interface and heartbeat wiring.
The SNMP community and NUT passwords grant access: keep their values in private backups, never in this repository or a transcript.
The heartbeat address is a spam-target identifier; keep its value out of this public repository too.

| Purpose | Files | Notes |
|---|---|---|
| NUT mode and UPS connection | `/etc/nut/nut.conf`, `/etc/nut/ups.conf` | `netserver`, `apcups`, SNMP target, community and poll interval |
| NUT server and monitor | `/etc/nut/upsd.conf`, `/etc/nut/upsd.users`, `/etc/nut/upsmon.conf`, `/etc/nut/upssched.conf` | Preserve credentials, matching `monuser` configuration and primary role |
| CGI | `/etc/nut/hosts.conf`, `/etc/nut/upsset.conf`, `/etc/nut/upsstats.html`, `/etc/nut/upsstats-single.html` | UPS display name, templates and CGI configuration |
| Web server | `/etc/apache2/` | Enabled site, CGI alias, modules and access settings |
| Host access | `/etc/hostname`, `/home/rob/.ssh/authorized_keys` | Hostname `upsd` and accepted public keys |
| Updates | `/etc/apt/apt.conf.d/20auto-upgrades`, `/etc/apt/apt.conf.d/50unattended-upgrades`, `/etc/apt/apt.conf.d/52unattended-upgrades-local` | Daily enablement, allowed origins, heartbeat recipient and automatic reboot |
| Update schedule | `/etc/systemd/system/apt-daily.timer.d/override.conf`, `/etc/systemd/system/apt-daily-upgrade.timer.d/override.conf` | Refresh and upgrade before the reboot window |
| Heartbeat transport | `/usr/local/sbin/hc-mail` | Local sendmail replacement; delegates other mail to `/usr/sbin/sendmail.exim4` |

The `nut-driver-enumerator` generates the drop-ins under `/etc/systemd/system/nut-driver@*.service.d/` from `ups.conf`.
Those files are generated state, not a second hand-maintained driver definition.

## Backup

Run these commands on the Mac after preparing the pinned public key above.
The archive contains real credentials; its private destination is deliberate.

```sh
umask 077
B="$HOME/nut-pi-backup-$(date +%F)"
mkdir -p "$B"
ssh -o IdentitiesOnly=yes -i /private/tmp/nut-pi-m3.pub rob@10.100.0.4 'sudo tar czf - /etc/nut /etc/apache2 /etc/hostname /home/rob/.ssh/authorized_keys /etc/apt/apt.conf.d/20auto-upgrades /etc/apt/apt.conf.d/50unattended-upgrades /etc/apt/apt.conf.d/52unattended-upgrades-local /etc/systemd/system/apt-daily.timer.d /etc/systemd/system/apt-daily-upgrade.timer.d /usr/local/sbin/hc-mail /boot/firmware/config.txt /boot/firmware/cmdline.txt' > "$B/nut-identities.tar.gz"
gzip -t "$B/nut-identities.tar.gz"
tar -tzf "$B/nut-identities.tar.gz"
```

Check that every file in the identity table is included.
The archive is configuration, not a bootable SD-card image.
Recreate the sendmail diversion during rebuild; do not restore another machine's entire dpkg diversion database.
No backup archive was created during the 2026-10-14 check-in.

## Rebuild

Use a fresh Raspberry Pi OS Lite 64-bit card on a supported release.
Keep the original card until NUT readings, clients, the web page and the heartbeat work.

1. Take the configuration backup above.
2. Create user `rob`, hostname `upsd`, and SSH access with the saved authorized keys.
3. Restore the reserved address 10.100.0.4 in the network configuration.
4. Install `nut-server`, `nut-client`, `nut-cgi`, `apache2`, `unattended-upgrades`, `curl` and `exim4` with apt.
5. Restore `/etc/nut/`, `/etc/apache2/`, the three apt configuration files and both timer overrides from the private backup.
6. Preserve owner `root:nut` and mode `0640` on the NUT files that contain credentials.
7. Enable Apache's CGI module with `sudo a2enmod cgid`.
8. Reload systemd with `sudo systemctl daemon-reload`.
9. Enable `nut-server`, `nut-monitor`, `apache2`, `unattended-upgrades`, `apt-daily.timer` and `apt-daily-upgrade.timer` with systemctl.
10. Restart the restored services and check `upsc apcups@localhost` returns fresh readings.
11. Check `http://10.100.0.4/cgi-bin/nut/upsstats.cgi` displays `apcups`.
12. Restore the heartbeat helper and diversion as described below.
13. Verify client readings and monitor authentication before relying on automatic shutdown.

Use a backup taken after the authentication repair; an older backup restores the broken monitor configuration.
The UPS uses SNMP over the network, so there is no USB-device identity to recreate.

## Updates and heartbeat

`20auto-upgrades` enables daily package-list refresh and unattended upgrades.
Both apt timers are enabled and active; the last scheduled upgrade before the check-in completed successfully at 06:20 BST.
The package indexes were current that day, and `apt list --upgradable` reported no pending updates.
The existing origins include `origin=*`, so Raspberry Pi repository packages are eligible too.
`52unattended-upgrades-local` enables automatic reboot at 04:45 Europe/London time when `/var/run/reboot-required` exists.
The timer overrides schedule package-list refresh at 03:30 and upgrades at 04:00, each with up to ten minutes fixed jitter.
They clear the vendor `OnCalendar` entries before assigning the new schedule.
The observed next runs were 03:35:49 and 04:04:08 BST.
The earlier schedule matters: handing a reboot time that has already passed to `shutdown` schedules it for tomorrow.

The existing `Unattended-Upgrade::Mail` recipient selects the operator-owned `upsd.cynexia.net` check.
`Unattended-Upgrade::MailReport "always";` requests a report even when no packages change.
Installed unattended-upgrades prefers `/usr/sbin/sendmail` over `/usr/bin/mail`.
`hc-mail` converts the report to an HTTP ping, appending `/fail` when the subject contains `FAILURE`.
It follows the [ADS-B Pi recipe](adsb-pi.md), including its report-body behavior, with one addition: other recipients pass through to Exim.
Root already has a local mailbox, so replacing unrelated mail delivery would lose existing behavior.

Restore `/usr/local/sbin/hc-mail` from the backup before running these commands on a rebuilt Pi.

```sh
sudo chmod 0755 /usr/local/sbin/hc-mail
sudo dpkg-divert --local --rename --add --divert /usr/sbin/sendmail.exim4 /usr/sbin/sendmail
sudo ln -s /usr/local/sbin/hc-mail /usr/sbin/sendmail
```

The diversion keeps Exim package upgrades from overwriting the helper link.
Exim itself remains installed and running, and non-healthchecks recipients retain their body, command arguments and exit status.
The check allows 24 hours between pings plus one hour grace.
It detects missing or failed upgrade reports; it does not test UPS battery health, NUT authentication or shutdown coordination.

Run `sudo unattended-upgrade --dry-run -v` to check update eligibility without installing packages.
This version deliberately suppresses summary mail during a dry run.
The check-in therefore tested the installed `send_summary_mail` function separately, without running package-changing code.
The read-only API then showed `n_pings=1`, status `up`, at 2026-10-14 22:33:34 UTC; before wiring it was zero.
Local tests also verified success/failure routing and preservation of other mail.
The next scheduled daily report remains the first automatic delivery after this change.
