"""Exercise NetAlertX's startup override for the merged inventory feed."""

import base64
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).with_name("netalertx-bootstrap.sh")
def decode_import(encoded):
    # Match upstream server/plugins/plugin_helper.py decode_settings_base64.
    settings_list = json.loads(base64.b64decode(encoded).decode("utf-8"))
    settings = {}
    for _, key, kind, value in settings_list:
        if kind.lower() == "boolean":
            settings[key] = value.lower() == "true"
        elif kind.lower() == "integer":
            settings[key] = int(value)
        elif kind.lower() == "float":
            settings[key] = float(value)
        else:
            settings[key] = value
    return settings


class BootstrapTest(unittest.TestCase):
    def run_bootstrap(self):
        self.assertTrue(SCRIPT.exists(), "bootstrap script missing")
        source = SCRIPT.read_text()
        self.assertEqual(source.count("exec /root-entrypoint.sh"), 1)
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            stub = folder / "entrypoint"
            stub.write_text('#!/bin/sh\nprintf %s "$APP_CONF_OVERRIDE" > "$NETALERTX_TEST_OUTPUT"\n')
            stub.chmod(0o700)
            test_script = folder / SCRIPT.name
            test_script.write_text(source.replace("exec /root-entrypoint.sh", f'exec "{stub}"'))
            output = folder / "override.json"
            env = dict(os.environ, NETALERTX_TEST_OUTPUT=str(output))
            env.pop("OPNSENSE_API_KEY", None)
            env.pop("OPNSENSE_API_SECRET", None)
            result = subprocess.run(["sh", str(test_script)], env=env, capture_output=True, text=True)
            return result, output.read_text() if output.exists() else None

    def test_one_no_auth_merged_import_preserves_other_settings(self):
        result, output = self.run_bootstrap()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        override = json.loads(output)
        self.assertEqual(override["LOG_LEVEL"], "minimal")
        # Upstream also loads plugins with non-disabled RUN defaults.
        for plugin in ("ARPSCAN", "AVAHISCAN", "DIGSCAN", "NSLOOKUP"):
            self.assertEqual(override[f"{plugin}_RUN"], "disabled")
        for plugin in ("NBTSCAN", "INTRNT", "NMAP"):
            self.assertEqual(override[f"{plugin}_RUN"], "schedule")
        self.assertEqual(override["NMAP_RUN_SCHD"], "0 2 * * *")
        self.assertNotIn("NMAP_ARGS", override)
        self.assertEqual(override["SCAN_SUBNETS"], ["192.168.17.0/24", "10.100.0.0/24", "10.0.2.0/24"])
        self.assertEqual(str(override["DEV_HIST_DAYS"]), "90")
        self.assertEqual(str(override["ICMP_RUN_TIMEOUT"]), "120")
        for prefix in ("RSTIMPRT", "ICMP"):
            self.assertEqual(override[f"{prefix}_RUN"], "schedule")
            self.assertEqual(override[f"{prefix}_RUN_SCHD"], "*/5 * * * *")
        self.assertEqual(len(override["RSTIMPRT_imports"]), 1)
        merged = decode_import(override["RSTIMPRT_imports"][0])
        for field, want in {
            "RSTIMPRT_name": "Netdisco and OPNsense",
            "RSTIMPRT_url": "http://netdisco.netdisco.svc.cluster.local:5001/",
            "RSTIMPRT_method": "GET",
            "RSTIMPRT_auth_type": "none",
            "RSTIMPRT_scanMac": "mac",
            "RSTIMPRT_scanLastIP": "ip",
            "RSTIMPRT_scanName": "hostname",
            "RSTIMPRT_scanVendor": "manufacturer",
            "RSTIMPRT_scanVlan": "intf_description",
            "RSTIMPRT_scanParentMAC": "switch_mac",
            "RSTIMPRT_scanParentPort": "switch_port",
            "RSTIMPRT_fake_mac": False,
        }.items():
            self.assertEqual(merged[field], want)
        for field in ("RSTIMPRT_username", "RSTIMPRT_password", "RSTIMPRT_device_path"):
            self.assertNotIn(field, merged)
        self.assertEqual(override["UI_MY_DEVICES"], ["online", "offline", "new", "down"])
        self.assertIn("Device_TableHead_Vlan", override["UI_device_columns"])
        self.assertIn("Device_TableHead_Vlan", override["UI_columns_filters"])
        self.assertNotIn("kea", SCRIPT.read_text().lower())
        self.assertNotIn("NETALERTX_IMPORT_COMMAND", SCRIPT.read_text())


if __name__ == "__main__":
    unittest.main()
