#!/bin/sh
# Convert unattended-upgrades report mail to a heartbeat; preserve other mail.
set -u
msg=$(cat)
to=$(printf '%s\n' "$msg" | sed -n 's/^To: *//p' | head -1 | tr -d '<> ')
case "$to" in
  *@hc-ping.com) ;;
  *) printf '%s\n' "$msg" | /usr/sbin/sendmail.exim4 "$@"; exit "$?" ;;
esac
url="https://hc-ping.com/${to%@hc-ping.com}"
case "$(printf '%s\n' "$msg" | sed -n 's/^Subject: *//p' | head -1)" in
  *FAILURE*) url="$url/fail" ;;
esac
if [ -n "${HC_MAIL_DRY:-}" ]; then printf '%s\n' "$url"; exit 0; fi
printf '%s\n' "$msg" | head -c 100000 | curl -fsS -m 10 --retry 3 -o /dev/null --data-binary @- "$url"
