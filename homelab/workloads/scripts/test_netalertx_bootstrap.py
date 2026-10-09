"""Exercise NetAlertX's startup override with dummy API credentials."""

import base64
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).with_name("netalertx-bootstrap.sh")
KEY = 'key"\\$with spaces'
SECRET = "secret'\\$with spaces"


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
    def run_bootstrap(self, key=KEY, secret=SECRET):
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
            if key is None:
                env.pop("OPNSENSE_API_KEY", None)
            else:
                env["OPNSENSE_API_KEY"] = key
            if secret is None:
                env.pop("OPNSENSE_API_SECRET", None)
            else:
                env["OPNSENSE_API_SECRET"] = secret
            result = subprocess.run(["sh", str(test_script)], env=env, capture_output=True, text=True)
            return result, output.read_text() if output.exists() else None

    def test_two_tls_verified_imports_preserve_dummy_credentials(self):
        result, output = self.run_bootstrap()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        override = json.loads(output)
        self.assertEqual(override["LOG_LEVEL"], "minimal")
        # Upstream also loads plugins with non-disabled RUN defaults.
        for plugin in ("ARPSCAN", "AVAHISCAN", "DIGSCAN", "NBTSCAN", "NSLOOKUP"):
            self.assertEqual(override[f"{plugin}_RUN"], "disabled")
        self.assertEqual(override["SCAN_SUBNETS"], ["192.168.17.0/24", "10.100.0.0/24", "10.0.2.0/24"])
        self.assertEqual(str(override["DEV_HIST_DAYS"]), "90")
        self.assertEqual(str(override["ICMP_RUN_TIMEOUT"]), "120")
        for prefix in ("RSTIMPRT", "ICMP"):
            self.assertEqual(override[f"{prefix}_RUN"], "schedule")
            self.assertEqual(override[f"{prefix}_RUN_SCHD"], "*/5 * * * *")
        imports = [decode_import(item) for item in override["RSTIMPRT_imports"]]
        self.assertEqual(len(imports), 2)
        by_url = {item["RSTIMPRT_url"]: item for item in imports}
        self.assertEqual(set(by_url), {
            "https://gw.cynexia.net/api/diagnostics/interface/search_arp",
            "https://gw.cynexia.net/api/kea/leases4/search",
        })
        for item in imports:
            for field, want in {
                "RSTIMPRT_method": "GET",
                "RSTIMPRT_verify_ssl": True,
                "RSTIMPRT_auth_type": "basic",
                "RSTIMPRT_username": KEY,
                "RSTIMPRT_password": SECRET,
                "RSTIMPRT_device_path": "rows",
            }.items():
                self.assertEqual(item[field], want)
            self.assertFalse(item.get("RSTIMPRT_fake_mac", False))
        arp = by_url["https://gw.cynexia.net/api/diagnostics/interface/search_arp"]
        kea = by_url["https://gw.cynexia.net/api/kea/leases4/search"]
        for item, mapping in (
            (arp, {"scanMac": "mac", "scanLastIP": "ip", "scanName": "hostname", "scanVendor": "manufacturer"}),
            (kea, {"scanMac": "hwaddr", "scanLastIP": "address", "scanName": "hostname", "scanVendor": "mac_info"}),
        ):
            for field, want in mapping.items():
                self.assertEqual(item[f"RSTIMPRT_{field}"], want)

    def test_missing_credential_fails_without_printing_values(self):
        for key, secret in ((None, SECRET), (KEY, None), ("", SECRET), (KEY, "")):
            with self.subTest(key=bool(key), secret=bool(secret)):
                result, output = self.run_bootstrap(key, secret)
                self.assertNotEqual(result.returncode, 0)
                self.assertIsNone(output)
                self.assertNotIn(KEY, result.stdout + result.stderr)
                self.assertNotIn(SECRET, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
