#!/bin/sh
set -eu

: "${OPNSENSE_API_KEY:?OPNSENSE_API_KEY required}"
: "${OPNSENSE_API_SECRET:?OPNSENSE_API_SECRET required}"

encode_import() {
  jq -nc \
    --arg name "$1" --arg url "$2" --arg mac "$3" --arg ip "$4" \
    --arg vendor "$5" --arg api_key "$OPNSENSE_API_KEY" --arg api_secret "$OPNSENSE_API_SECRET" \
    '{RSTIMPRT_name:$name,RSTIMPRT_url:$url,RSTIMPRT_method:"GET",RSTIMPRT_verify_ssl:true,
      RSTIMPRT_auth_type:"basic",RSTIMPRT_username:$api_key,RSTIMPRT_password:$api_secret,
      RSTIMPRT_device_path:"rows",RSTIMPRT_scanMac:$mac,RSTIMPRT_scanLastIP:$ip,
      RSTIMPRT_scanName:"hostname",RSTIMPRT_scanVendor:$vendor,RSTIMPRT_fake_mac:false}' |
    base64 | tr -d '\n'
}

arp=$(encode_import 'OPNsense ARP' 'https://gw.cynexia.net/api/diagnostics/interface/search_arp' mac ip manufacturer)
kea=$(encode_import 'OPNsense Kea leases' 'https://gw.cynexia.net/api/kea/leases4/search' hwaddr address mac_info)
APP_CONF_OVERRIDE=$(jq -nc --arg arp "$arp" --arg kea "$kea" '{
  RSTIMPRT_imports:[$arp,$kea],RSTIMPRT_RUN:"schedule",RSTIMPRT_RUN_SCHD:"*/5 * * * *",
  ICMP_RUN:"schedule",ICMP_RUN_SCHD:"*/5 * * * *",
  SCAN_SUBNETS:["192.168.17.0/24","10.100.0.0/24","10.0.2.0/24"],DEV_HIST_DAYS:90
}')
export APP_CONF_OVERRIDE
exec /root-entrypoint.sh
