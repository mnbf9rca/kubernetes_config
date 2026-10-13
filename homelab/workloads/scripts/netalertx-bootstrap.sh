#!/bin/sh
set -eu

feed=$(jq -nc \
    '[["", "RSTIMPRT_name", "string", "Netdisco and OPNsense"],
      ["", "RSTIMPRT_url", "string", "http://netdisco.netdisco.svc.cluster.local:5001/"],
      ["", "RSTIMPRT_method", "string", "GET"],
      ["", "RSTIMPRT_auth_type", "string", "none"],
      ["", "RSTIMPRT_scanMac", "string", "mac"],
      ["", "RSTIMPRT_scanLastIP", "string", "ip"],
      ["", "RSTIMPRT_scanName", "string", "hostname"],
      ["", "RSTIMPRT_scanVendor", "string", "manufacturer"],
      ["", "RSTIMPRT_scanVlan", "string", "intf_description"],
      ["", "RSTIMPRT_scanParentMAC", "string", "switch_mac"],
      ["", "RSTIMPRT_scanParentPort", "string", "switch_port"],
      ["", "RSTIMPRT_fake_mac", "boolean", "false"]]' |
    base64 | tr -d '\n')

[ -n "$feed" ] || exit 1
# The pod's own address changes on every restart and local_MAC (not a plugin,
# no *_RUN) adds a device for it; ignore the pod network (SQL LIKE).
APP_CONF_OVERRIDE=$(jq -nc --arg feed "$feed" '{
  LOG_LEVEL:"minimal",
  ARPSCAN_RUN:"disabled",AVAHISCAN_RUN:"disabled",DIGSCAN_RUN:"disabled",
  NSLOOKUP_RUN:"disabled",NBTSCAN_RUN:"schedule",INTRNT_RUN:"schedule",
  NMAP_RUN:"schedule",NMAP_RUN_SCHD:"0 2 * * *",
  RSTIMPRT_imports:[$feed],RSTIMPRT_RUN:"schedule",RSTIMPRT_RUN_SCHD:"*/5 * * * *",
  ICMP_RUN:"schedule",ICMP_RUN_SCHD:"*/5 * * * *",ICMP_RUN_TIMEOUT:120,
  SCAN_SUBNETS:["192.168.17.0/24","10.100.0.0/24","10.0.2.0/24"],DEV_HIST_DAYS:90,
  NEWDEV_ignored_IPs:["10.244.%"],
  UI_MY_DEVICES:["online","offline","new","down"],
  UI_columns_filters:["Device_TableHead_Type","Device_TableHead_SyncHubNodeName","Device_TableHead_SourcePlugin","Device_TableHead_Vlan"],
  UI_device_columns:["Device_TableHead_Icon","Device_TableHead_Name","Device_TableHead_Type","Device_TableHead_Vendor","Device_TableHead_FirstSession","Device_TableHead_LastSession","Device_TableHead_LastIP","Device_TableHead_Vlan","Device_TableHead_Status","Device_TableHead_MAC_full","Device_TableHead_SourcePlugin","Device_TableHead_CustomProps"]
}')
export APP_CONF_OVERRIDE
exec /root-entrypoint.sh
