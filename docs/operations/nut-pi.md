# NUT UPS Pi

`upsd` is a Raspberry Pi 4 Model B Rev 1.5 with 4 GB RAM at 10.100.0.4, outside both Kubernetes clusters.
It runs Raspberry Pi OS Lite 64-bit on Debian 12 (bookworm).
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

`upsd.cynexia.net` has no public DNS record, and the local resolver has no answer either.
The Homepage card uses the IP address.
`upsd.lan.cynexia.net` is not a usable alternative.

## What runs on it

| Component | Installed from | Updates | Notes |
|---|---|---|---|
| NUT server and client | Debian `nut-server`, `nut-client` | unattended-upgrades | `MODE=netserver`; UPS name `apcups` |
| `snmp-ups` driver | `nut-server` | unattended-upgrades | `nut-driver@apcups.service`; `apcc` MIB; polls 10.100.0.6 every second |
| `upsd` | `nut-server` | unattended-upgrades | `nut-server.service`; listens on all IPv4 interfaces, TCP 3493 |
| `upsmon` | `nut-client` | unattended-upgrades | `nut-monitor.service`; authenticates as `monuser`, with the `primary` role |
| NUT CGI and Apache | Debian `nut-cgi`, `apache2` | unattended-upgrades | Status page: `http://10.100.0.4/cgi-bin/nut/upsstats.cgi` |
| unattended-upgrades | Debian | apt | Daily package-list and upgrade timers; report mail drives the existing `upsd.cynexia.net` healthchecks.io check |
| Exim4 and bsd-mailx | Debian | unattended-upgrades | Non-healthchecks mail passes through the diverted Exim sendmail |

The other running services are SSH, NetworkManager, Avahi, Bluetooth, ModemManager, cron, systemd-timesyncd, triggerhappy and standard system services.
`/opt/pigpio` exists, but no pigpio service or listening port was found.
No PeaNUT, webNUT, container stack or additional application listener was found.
Apache serves its default page at `/`; the UPS page is under `/cgi-bin/nut/`.
The other listeners are SSH on TCP 22, loopback-only Exim on TCP 25, and Avahi on UDP 5353.

### Network access and UPS readings

No host-level allowlist limits NUT readers: `upsd` binds `0.0.0.0:3493`, and the Pi's IPv4/IPv6 firewall policies accept traffic.
Apache's CGI directory has `Require all granted`.
Reachability therefore depends on the network firewall outside this Pi; that firewall was not inspected.
The observed readers are localhost, pve3 at 10.100.0.107 and Home Assistant at 192.168.17.102.
This is a client list, not an access-control list.
The only configured UPS is `apcups`, an APC Smart-UPS RT 1000 XL with two battery packs.

Read charge, load, runtime and status with `upsc`:

```sh
upsc -l localhost
upsc apcups@localhost
systemctl status nut-server nut-monitor nut-driver@apcups
```

### Monitor authentication and shutdown

Before repair, `/etc/nut/upsmon.conf` used username `admin`, while `/etc/nut/upsd.users` defined `[monuser]` with an invalid `admin master` directive.
Both login tests returned `ERR ACCESS-DENIED`, and `GET NUMLOGINS apcups` returned zero.
The local monitor now uses `monuser` and its existing password, with the [documented `upsmon primary` role](https://networkupstools.org/historic/v2.8.0/docs/man/upsd.users.html) on the server.
All existing server passwords were preserved; neither remote client configuration changed.
After reloading both services, the same login assertion passed and `upsd` logged the local monitor login.
All three TCP clients remained connected and telemetry stayed fresh.

On battery plus low battery, primary `upsmon` sets forced shutdown (FSD), waits up to `HOSTSYNC 15` seconds for logged-in secondary monitors, writes `/etc/killpower`, and halts the Pi with `/sbin/shutdown -h +0`.
The shutdown hook then runs `upsdrvctl shutdown`; [snmp-ups first attempts `shutdown.return`](https://github.com/networkupstools/nut/blob/v2.8.0/drivers/snmp-ups.c#L332-L367), requesting an output cut after the reported `ups.delay.shutdown` of 60 seconds and restoration when mains power returns.
The upstream driver warns that networking may already be unavailable at that stage, so the command may not reach the UPS.
NetAlertX identifies 10.100.0.107 as `pve3`; it and Home Assistant hold reader connections without monitor logins, so the primary does not wait for their shutdown, and pve3's physical power feed was not verified.
An output cut removes power from every device supplied by this UPS, potentially including a still-running pve3; the APC card's SNMP write access and actual load-off behavior were not tested.

A normal reboot briefly interrupts the NUT readers at 10.100.0.107 and 192.168.17.102, as well as the local monitor.
Automatic reboot is enabled for 04:45 local time after upgrades require it.

## Canonical configuration and secrets

`nut-pi/` contains the machine's canonical configuration and heartbeat helper.
The NUT files preserve active settings and omit vendor comment boilerplate.
The CGI HTML templates and Apache default site come from their Debian packages; `cgid` is the enabled Apache module.
The canonical local apt override consolidates settings that the existing Pi distributes across `20auto-upgrades`, `50unattended-upgrades` and `52unattended-upgrades-local`.

| Repository path | Installed path | Owner and mode |
|---|---|---|
| `nut-pi/etc/nut/{nut,ups,upsd,upsmon,upssched}.conf`, `nut-pi/etc/nut/upsd.users` | Same paths below `/etc/nut/` | `root:nut`, `0640` |
| `nut-pi/etc/nut/{hosts,upsset}.conf` | Same paths below `/etc/nut/` | `root:root`, `0644` for CGI readers |
| `nut-pi/etc/apt/apt.conf.d/52unattended-upgrades-local` | `/etc/apt/apt.conf.d/52unattended-upgrades-local` | `root:root`, `0644` |
| `nut-pi/etc/systemd/system/apt-daily.timer.d/override.conf` | `/etc/systemd/system/apt-daily.timer.d/override.conf` | `root:root`, `0644` |
| `nut-pi/etc/systemd/system/apt-daily-upgrade.timer.d/override.conf` | `/etc/systemd/system/apt-daily-upgrade.timer.d/override.conf` | `root:root`, `0644` |
| `nut-pi/scripts/hc-mail.sh` | `/usr/local/sbin/hc-mail` | `root:root`, `0755` |

`nut-pi/.env.tpl` contains only these 1Password references:

| Variable | 1Password reference | Field type |
|---|---|---|
| `NUT_MONUSER_PASSWORD` | `op://Homelab/nut-pi/monuser-password` | concealed |
| `NUT_SNMP_COMMUNITY` | `op://Homelab/nut-pi/snmp-community` | concealed |
| `NUT_HEALTHCHECKS_EMAIL` | `op://Homelab/nut-pi/healthchecks-email` | text |

The password and SNMP community grant access; the heartbeat address is a spam-target identifier.
Their values belong in 1Password and the installed files, never in Git or a rendered local file.
These variables are for this machine only; they are not added to the clusters' root `.env.tpl`.
The intended `Homelab/nut-pi` item did not exist on 2026-10-15 because the service account could not create it.
The operator has a private JSON handoff for import; vault-equality verification remains pending that import.
The `nut-driver-enumerator` generates `/etc/systemd/system/nut-driver@*.service.d/` from `ups.conf`; those drop-ins are not copied into Git.

## Rebuild

Use a fresh Raspberry Pi OS Lite 64-bit card on a supported release.
Keep the original card until NUT readings, clients, the web page and the heartbeat work.

1. Create hostname `upsd`, user `rob`, and SSH access with the m3 public key in Raspberry Pi Imager.
2. Restore the reserved address 10.100.0.4 in the network configuration.
3. Install `nut-server`, `nut-client`, `nut-cgi`, `apache2`, `unattended-upgrades`, `curl` and `exim4` with apt.
4. Create the intended `Homelab/nut-pi` item from the operator's typed JSON before resolving the references.
5. Run `make check-vm-scripts` from the repository root on the Mac.
6. Install the canonical files with the commands below from the repository root.
7. Enable Apache's CGI module with `sudo a2enmod cgid` on the Pi.
8. Reload systemd with `sudo systemctl daemon-reload` on the Pi.
9. Enable `nut-server`, `nut-monitor`, `apache2`, `unattended-upgrades`, `apt-daily.timer` and `apt-daily-upgrade.timer` with systemctl.
10. Restart the restored services and timers.
11. Check `upsc apcups@localhost` returns fresh readings.
12. Check `http://10.100.0.4/cgi-bin/nut/upsstats.cgi` displays `apcups`.
13. Verify client readings and monitor authentication before relying on automatic shutdown.

Prepare the pinned SSH key with the access commands before installing the files.
`op run` resolves values inside one child process; the stream passes directly into SSH so its stdout masking cannot corrupt files.
The envsubst allowlists are explicit and single-quoted.

```sh
op run --env-file=nut-pi/.env.tpl -- sh <<'SH'
set -e
for name in nut.conf ups.conf upsd.conf upsd.users upsmon.conf upssched.conf; do
    test -r "nut-pi/etc/nut/$name"
    envsubst '${NUT_MONUSER_PASSWORD} ${NUT_SNMP_COMMUNITY}' < "nut-pi/etc/nut/$name" |
        ssh -o IdentitiesOnly=yes -i /private/tmp/nut-pi-m3.pub rob@10.100.0.4 "sudo install -o root -g nut -m 0640 /dev/stdin /etc/nut/$name"
done
for name in hosts.conf upsset.conf; do
    ssh -o IdentitiesOnly=yes -i /private/tmp/nut-pi-m3.pub rob@10.100.0.4 "sudo install -o root -g root -m 0644 /dev/stdin /etc/nut/$name" < "nut-pi/etc/nut/$name"
done
envsubst '${NUT_HEALTHCHECKS_EMAIL}' < nut-pi/etc/apt/apt.conf.d/52unattended-upgrades-local |
    ssh -o IdentitiesOnly=yes -i /private/tmp/nut-pi-m3.pub rob@10.100.0.4 'sudo install -o root -g root -m 0644 /dev/stdin /etc/apt/apt.conf.d/52unattended-upgrades-local'
SH
for timer in apt-daily apt-daily-upgrade; do
    ssh -o IdentitiesOnly=yes -i /private/tmp/nut-pi-m3.pub rob@10.100.0.4 "sudo install -D -o root -g root -m 0644 /dev/stdin /etc/systemd/system/$timer.timer.d/override.conf" < "nut-pi/etc/systemd/system/$timer.timer.d/override.conf"
done
ssh -o IdentitiesOnly=yes -i /private/tmp/nut-pi-m3.pub rob@10.100.0.4 'sudo install -o root -g root -m 0755 /dev/stdin /usr/local/sbin/hc-mail' < nut-pi/scripts/hc-mail.sh
ssh -o IdentitiesOnly=yes -i /private/tmp/nut-pi-m3.pub rob@10.100.0.4 'sudo dpkg-divert --local --rename --add --divert /usr/sbin/sendmail.exim4 /usr/sbin/sendmail && sudo ln -s /usr/local/sbin/hc-mail /usr/sbin/sendmail'
```

The sendmail diversion must be created before its symlink on a new installation.
An existing installation already has both; replacing its configuration does not require creating them again.
The UPS uses SNMP over the network, so there is no USB-device identity to recreate.

## Updates and heartbeat

The local apt override enables daily package-list refresh and unattended upgrades.
Its `origin=*` entry keeps Raspberry Pi repository packages eligible too.
It enables automatic reboot at 04:45 Europe/London time when `/var/run/reboot-required` exists.
The timer overrides schedule package-list refresh at 03:30 and upgrades at 04:00, each with up to ten minutes fixed jitter.
They clear the vendor `OnCalendar` entries before assigning the new schedule.
The earlier schedule matters: handing a reboot time that has already passed to `shutdown` schedules it for tomorrow.

`Unattended-Upgrade::Mail` selects the operator-owned `upsd.cynexia.net` check.
`Unattended-Upgrade::MailReport "always";` requests a report even when no packages change.
Installed unattended-upgrades prefers `/usr/sbin/sendmail` over `/usr/bin/mail`.
`hc-mail` converts the report to an HTTP ping, appending `/fail` when the subject contains `FAILURE`.
It follows the [ADS-B Pi recipe](adsb-pi.md), including its report-body behavior, with one addition: other recipients pass through to Exim.
Root already has a local mailbox, so replacing unrelated mail delivery would lose existing behavior.
The diversion keeps Exim package upgrades from overwriting the helper link.
The check allows 24 hours between pings plus one hour grace.
It detects missing or failed upgrade reports; it does not test UPS battery health, NUT authentication or shutdown coordination.

Run `sudo unattended-upgrade --dry-run -v` to check update eligibility without installing packages.
This version deliberately suppresses summary mail during a dry run.
Use the existing check's API status to verify report delivery after a normal scheduled run.
