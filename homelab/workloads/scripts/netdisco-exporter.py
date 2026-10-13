#!/usr/bin/env python3
"""Serve one NetAlertX import feed from OPNsense ARP and Netdisco access ports."""

import base64
import json
import os
import subprocess
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer


NODE_SQL = """
WITH access_nodes AS (
  SELECT DISTINCT ON (n.mac)
    n.mac, n.switch, n.port, n.vlan, n.time_first, n.time_last
  FROM node n
  JOIN device_port p ON p.ip = n.switch AND p.port = n.port
  WHERE n.active
    AND NOT coalesce(p.is_uplink, false)
    AND p.remote_ip IS NULL
    AND nullif(btrim(p.remote_port), '') IS NULL
    AND nullif(btrim(p.remote_type), '') IS NULL
    AND nullif(btrim(p.remote_id), '') IS NULL
  ORDER BY n.mac, n.time_recent DESC NULLS LAST,
           n.time_last DESC NULLS LAST, n.switch, n.port
)
SELECT coalesce(json_agg(row_to_json(inventory)), '[]'::json)
FROM (
  SELECT a.mac::text AS mac,
         host(client_ip.ip) AS netdisco_ip,
         client_ip.dns AS netdisco_dns,
         vendor.company AS netdisco_vendor,
         a.vlan AS netdisco_vlan,
         coalesce(switch_ip.mac, d.mac)::text AS switch_mac,
         a.port AS switch_port,
         a.time_first AS first_seen,
         a.time_last AS last_seen
  FROM access_nodes a
  LEFT JOIN LATERAL (
    SELECT ni.ip, ni.dns FROM node_ip ni
    WHERE ni.mac = a.mac AND ni.active
    ORDER BY ni.time_last DESC NULLS LAST, ni.ip
    LIMIT 1
  ) client_ip ON true
  LEFT JOIN LATERAL (
    SELECT m.company FROM manufacturer m
    WHERE a.mac BETWEEN m.first AND m.last
    ORDER BY m.bits DESC LIMIT 1
  ) vendor ON true
  LEFT JOIN LATERAL (
    SELECT ni.mac FROM node_ip ni
    WHERE ni.ip = a.switch AND ni.active
    ORDER BY ni.time_last DESC NULLS LAST, ni.mac
    LIMIT 1
  ) switch_ip ON true
  LEFT JOIN device d ON d.ip = a.switch
  ORDER BY a.mac
) inventory;
"""


def fetch_arp():
    credentials = "%s:%s" % (os.environ["GATEWAY_ARP_USER"],
                             os.environ["GATEWAY_ARP_PASS"])
    encoded = base64.b64encode(credentials.encode()).decode()
    request = urllib.request.Request(
        "https://gw.cynexia.net/api/diagnostics/interface/search_arp?resolve=yes",
        headers={"Authorization": "Basic " + encoded})
    with urllib.request.urlopen(request, timeout=8) as response:
        data = json.load(response)
    if not isinstance(data, dict) or not isinstance(data.get("rows"), list):
        raise ValueError("Gateway ARP response has no rows array")
    if any(not isinstance(row, dict) for row in data["rows"]):
        raise ValueError("Gateway ARP response contains an invalid row")
    return data["rows"]


def fetch_nodes():
    env = os.environ.copy()
    env["PGPASSWORD"] = env["NETDISCO_DB_PASS"]
    env["PGOPTIONS"] = "-c default_transaction_read_only=on"
    result = subprocess.run([
        "psql", "-X", "-v", "ON_ERROR_STOP=1", "-tA",
        "-h", env["NETDISCO_DB_HOST"], "-U", env["NETDISCO_DB_USER"],
        "-d", env["NETDISCO_DB_NAME"], "-c", NODE_SQL,
    ], env=env, capture_output=True, text=True, check=True, timeout=8)
    nodes = json.loads(result.stdout)
    if not isinstance(nodes, list) or any(not isinstance(n, dict) for n in nodes):
        raise ValueError("Netdisco query returned invalid rows")
    return nodes


def merge_rows(arp_rows, nodes):
    by_mac = {n["mac"].lower(): n for n in nodes if n.get("mac")}
    result = []
    seen = set()
    for row in arp_rows:
        merged = row.copy()
        mac = (row.get("mac") or "").lower()
        if mac in by_mac:
            seen.add(mac)
            merged.update({key: value for key, value in by_mac[mac].items()
                           if key != "mac"})
        result.append(merged)
    for mac, n in by_mac.items():
        if mac in seen:
            continue
        result.append({
            "mac": n["mac"], "ip": n.get("netdisco_ip"),
            "hostname": n.get("netdisco_dns"),
            "manufacturer": n.get("netdisco_vendor"),
            "intf_description": "",
            **{key: value for key, value in n.items() if key != "mac"},
        })
    return result


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/":
            self.send_error(404)
            return
        try:
            rows = fetch_arp()
        except (OSError, ValueError, KeyError):
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        try:
            nodes = fetch_nodes()
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            nodes = []
        body = json.dumps(merge_rows(rows, nodes)).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", 5001), Handler).serve_forever()
