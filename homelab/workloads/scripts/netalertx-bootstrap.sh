#!/bin/sh
set -eu

: "${OPNSENSE_API_KEY:?OPNSENSE_API_KEY required}"
: "${OPNSENSE_API_SECRET:?OPNSENSE_API_SECRET required}"

encode_import() {
  jq -nc \
    --arg name "$1" --arg url "$2" --arg mac "$3" --arg ip "$4" \
    --arg vendor "$5" --arg api_key "$OPNSENSE_API_KEY" --arg api_secret "$OPNSENSE_API_SECRET" \
    '[["", "RSTIMPRT_name", "string", $name],
      ["", "RSTIMPRT_url", "string", $url],
      ["", "RSTIMPRT_method", "string", "GET"],
      ["", "RSTIMPRT_verify_ssl", "boolean", "true"],
      ["", "RSTIMPRT_auth_type", "string", "basic"],
      ["", "RSTIMPRT_username", "string", $api_key],
      ["", "RSTIMPRT_password", "string", $api_secret],
      ["", "RSTIMPRT_device_path", "string", "rows"],
      ["", "RSTIMPRT_scanMac", "string", $mac],
      ["", "RSTIMPRT_scanLastIP", "string", $ip],
      ["", "RSTIMPRT_scanName", "string", "hostname"],
      ["", "RSTIMPRT_scanVendor", "string", $vendor],
      ["", "RSTIMPRT_fake_mac", "boolean", "false"]]' |
    base64 | tr -d '\n'
}

arp=$(encode_import 'OPNsense ARP' 'https://gw.cynexia.net/api/diagnostics/interface/search_arp?resolve=yes' mac ip manufacturer)
[ -n "$arp" ] || exit 1
APP_CONF_OVERRIDE=$(jq -nc --arg arp "$arp" '{
  LOG_LEVEL:"minimal",
  ARPSCAN_RUN:"disabled",AVAHISCAN_RUN:"disabled",DIGSCAN_RUN:"disabled",
  NSLOOKUP_RUN:"disabled",NBTSCAN_RUN:"schedule",INTRNT_RUN:"schedule",
  NMAP_RUN:"schedule",NMAP_RUN_SCHD:"0 2 * * *",
  RSTIMPRT_imports:[$arp],RSTIMPRT_RUN:"schedule",RSTIMPRT_RUN_SCHD:"*/5 * * * *",
  ICMP_RUN:"schedule",ICMP_RUN_SCHD:"*/5 * * * *",ICMP_RUN_TIMEOUT:120,
  SCAN_SUBNETS:["192.168.17.0/24","10.100.0.0/24","10.0.2.0/24"],DEV_HIST_DAYS:90
}')
export APP_CONF_OVERRIDE
exec /root-entrypoint.sh
