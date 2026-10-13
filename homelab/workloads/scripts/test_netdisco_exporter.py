#!/usr/bin/env python3
"""Behavior tests for the Netdisco/OPNsense REST feed."""

import importlib.util
import io
import json
import os
import subprocess
import unittest
import urllib.request
from unittest import mock


SCRIPT = os.path.join(os.path.dirname(__file__), "netdisco-exporter.py")
if os.path.exists(SCRIPT):
    spec = importlib.util.spec_from_file_location("netdisco_exporter", SCRIPT)
    exporter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(exporter)
else:
    exporter = None


def arp_row(ip, vlan):
    return {
        "mac": "10:60:4b:92:8d:b2", "ip": ip, "hostname": "ilo.example",
        "manufacturer": "Hewlett Packard", "intf_description": vlan,
        "intf": "lan", "type": "dynamic", "expired": False,
        "expires": 900, "permanent": False,
    }


def node(mac="10:60:4b:92:8d:b2", ip=None, parent="18:fd:74:d5:6d:83",
         port="Port5"):
    return {
        "mac": mac, "netdisco_ip": ip, "netdisco_dns": "ilo.example",
        "netdisco_vendor": "Hewlett Packard", "netdisco_vlan": "0",
        "switch_mac": parent, "switch_port": port,
        "first_seen": "2026-10-12T17:29:10", "last_seen": "2026-10-13T08:20:13",
    }


class MergeRows(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(exporter, "netdisco-exporter.py is missing")

    def test_duplicate_gateway_rows_keep_their_ip_vlan_and_gain_parent(self):
        rows = [arp_row("192.168.17.68", "LAN"),
                arp_row("10.100.0.68", "servers")]
        result = exporter.merge_rows(rows, [node(ip="192.168.1.4")])
        self.assertEqual([r["ip"] for r in result],
                         ["192.168.17.68", "10.100.0.68"])
        self.assertEqual([r["intf_description"] for r in result],
                         ["LAN", "servers"])
        self.assertEqual([r["switch_mac"] for r in result],
                         ["18:fd:74:d5:6d:83"] * 2)
        self.assertEqual([r["switch_port"] for r in result], ["Port5"] * 2)
        self.assertEqual(result[0]["netdisco_ip"], "192.168.1.4")
        self.assertEqual(result[0]["expires"], 900)
        self.assertEqual(rows[0]["ip"], "192.168.17.68")

    def test_netdisco_only_mac_with_no_ip_keeps_parent(self):
        result = exporter.merge_rows([], [node()])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["ip"], None)
        self.assertEqual(result[0]["hostname"], "ilo.example")
        self.assertEqual(result[0]["manufacturer"], "Hewlett Packard")
        self.assertEqual(result[0]["intf_description"], "")
        self.assertEqual(result[0]["switch_mac"], "18:fd:74:d5:6d:83")
        self.assertEqual(result[0]["switch_port"], "Port5")

    def test_netdisco_only_mac_uses_latest_active_ip(self):
        result = exporter.merge_rows([], [node(ip="192.168.17.68")])
        self.assertEqual(result[0]["ip"], "192.168.17.68")

    def test_missing_switch_identity_is_not_invented(self):
        result = exporter.merge_rows([], [node(parent=None)])
        self.assertIsNone(result[0]["switch_mac"])

    def test_duplicate_node_sightings_emit_one_mac(self):
        result = exporter.merge_rows([], [node(port="Port4"), node(port="Port5")])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["switch_port"], "Port5")


class Fetching(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(exporter, "netdisco-exporter.py is missing")

    def test_gateway_fetch_uses_https_basic_auth_without_credentials_in_url(self):
        with mock.patch.dict(os.environ, {"GATEWAY_ARP_USER": "user",
                                              "GATEWAY_ARP_PASS": "secret"}):
            with mock.patch.object(urllib.request, "urlopen",
                                   return_value=io.BytesIO(json.dumps({"rows": [arp_row("1.2.3.4", "LAN")]}).encode())) as open_url:
                self.assertEqual(exporter.fetch_arp(), [arp_row("1.2.3.4", "LAN")])
        request = open_url.call_args.args[0]
        self.assertTrue(request.full_url.startswith("https://gw.cynexia.net/"))
        self.assertNotIn("secret", request.full_url)
        self.assertTrue(request.get_header("Authorization").startswith("Basic "))
        self.assertLess(open_url.call_args.kwargs["timeout"], 30)

    def test_gateway_requires_rows_array_not_html_login_page(self):
        with mock.patch.dict(os.environ, {"GATEWAY_ARP_USER": "user",
                                              "GATEWAY_ARP_PASS": "secret"}):
            with mock.patch.object(urllib.request, "urlopen",
                                   return_value=io.BytesIO(b"<html>login</html>")):
                with self.assertRaises(ValueError):
                    exporter.fetch_arp()

    def test_psql_returns_json_without_password_in_arguments(self):
        output = json.dumps([node()])
        with mock.patch.dict(os.environ, {
            "NETDISCO_DB_HOST": "localhost", "NETDISCO_DB_NAME": "netdisco",
            "NETDISCO_DB_USER": "netdisco", "NETDISCO_DB_PASS": "secret",
        }):
            with mock.patch.object(subprocess, "run",
                                   return_value=subprocess.CompletedProcess([], 0, output, "")) as run:
                self.assertEqual(exporter.fetch_nodes(), [node()])
                args, kwargs = run.call_args
                self.assertNotIn("secret", str(args))
                self.assertEqual(kwargs["env"]["PGPASSWORD"], "secret")
                self.assertIn("default_transaction_read_only=on",
                              kwargs["env"]["PGOPTIONS"])


class Endpoint(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(exporter, "netdisco-exporter.py is missing")

    def request(self):
        handler = object.__new__(exporter.Handler)
        handler.path = "/"
        handler.wfile = io.BytesIO()
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        handler.do_GET()
        return handler.send_response.call_args.args[0], handler.wfile.getvalue()

    def test_netdisco_failure_preserves_gateway_rows(self):
        with mock.patch.object(exporter, "fetch_arp", return_value=[arp_row("1.2.3.4", "LAN")]):
            with mock.patch.object(exporter, "fetch_nodes",
                                   side_effect=subprocess.TimeoutExpired("psql", 10)):
                status, body = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), [arp_row("1.2.3.4", "LAN")])

    def test_gateway_failure_returns_503_without_partial_data(self):
        with mock.patch.object(exporter, "fetch_arp", side_effect=ValueError("bad gateway")):
            with mock.patch.object(exporter, "fetch_nodes", return_value=[node()]):
                status, body = self.request()
        self.assertEqual(status, 503)
        self.assertEqual(body, b"")


if __name__ == "__main__":
    unittest.main()
