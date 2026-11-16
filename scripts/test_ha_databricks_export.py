#!/usr/bin/env python3
"""Focused privacy, UTC boundary, file-completion and upload-loss checks.

Run with the export venv's Python for the Parquet checks as well.
"""
import importlib.util
from pathlib import Path
import tempfile
import unittest

path = Path(__file__).with_name('ha-databricks-export.py')
spec = importlib.util.spec_from_file_location('ha_export', path)
export = importlib.util.module_from_spec(spec) if path.exists() else None
if export:
    spec.loader.exec_module(export)


class ExportChecks(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(export, 'export script is not implemented')

    def test_presence_families_and_retired_names_are_excluded(self):
        for entity in (
            {'entity_id': 'person.example'},
            {'entity_id': 'sensor.example_battery', 'platform': 'mobile_app'},
            {'entity_id': 'sensor.example_iphone16_battery'},
            {'entity_id': 'sensor.ab12cde_battery'},
            {'entity_id': 'sensor.example_charge', 'platform': 'ohme'},
            {'entity_id': 'sensor.ohme_example_energy', 'platform': 'integration'},
            {'entity_id': 'sensor.claude_usage_example'},
            {'entity_id': 'sensor.codex_usage_example'},
            {'entity_id': 'sensor.roomba_last_mission_start_time'},
            {'entity_id': 'sensor.lenovo_tb328fu_battery'},
            {'entity_id': 'sensor.example', 'platform': 'sun'},
            {'entity_id': 'sensor.example_geocoded_location'},
            {'entity_id': 'sensor.example_bssid'},
            {'entity_id': 'sensor.example', 'attribute_keys': ['latitude']},
            {'entity_id': 'sensor.example', 'device_name': 'Window contact'},
            {'entity_id': 'sensor.example', 'friendly_name': 'Side motion battery'},
            {'entity_id': 'sensor.unknown_gateway'},
        ):
            with self.subTest(entity=entity):
                self.assertIsNotNone(export.exclusion(entity))

    def test_whole_words_keep_benign_measurements_and_identifiers(self):
        for entity in (
            {'entity_id': 'sensor.outdoor_temperature'},
            {'entity_id': 'sensor.retired_power'},
            {'entity_id': 'sensor.persons', 'platform': 'spook'},
            {'entity_id': 'sensor.device_trackers', 'platform': 'spook'},
            {'entity_id': 'sensor.water_use', 'platform': 'water_allocation'},
            {'entity_id': 'sensor.lamp_ip', 'platform': 'wled'},
            {'entity_id': 'sensor.indoor_temperature', 'device_name': 'Outdoor station'},
        ):
            with self.subTest(entity=entity):
                self.assertIsNone(export.exclusion(entity))

    def test_metadata_drops_arbitrary_and_nested_attributes(self):
        self.assertEqual(export.labels({'friendly_name': 'Temperature', 'device_class': 'temperature',
                                        'latitude': 51, 'UserId': 'private',
                                        'unit_of_measurement': {'address': 'private'}}),
                         {'friendly_name': 'Temperature', 'device_class': 'temperature',
                          'unit_of_measurement': None, 'state_class': None})

    def test_utc_chunks_are_half_open_at_midnight_and_leap_day(self):
        a = export.utc_date('2024-02-28') + 23 * 3600 + 1800
        b = export.utc_date('2024-03-01') + 1800
        days = list(export.chunks(a, b, 86400))
        self.assertEqual(days, [(a, export.utc_date('2024-02-29')),
                                (export.utc_date('2024-02-29'), export.utc_date('2024-03-01')),
                                (export.utc_date('2024-03-01'), b)])
        self.assertEqual(list(export.chunks(a, a + 3600, 3600)),
                         [(a, a + 1800), (a + 1800, a + 3600)])
        with self.assertRaises(ValueError):
            list(export.chunks(b, a, 86400))
        with self.assertRaises(ValueError):
            export.utc_date('2024-02-30')
        with self.assertRaises(ValueError):
            list(export.chunks(a, a, 86400))

    def test_parquet_has_fixed_nullable_schema_and_no_partial_final_file(self):
        import pyarrow.parquet as pq
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'part.parquet'
            row = {'state_id': 1, 'metadata_id': 2, 'entity_id': 'sensor.example',
                   'state': 'unavailable', 'last_updated_ts': 123.5,
                   'last_changed_ts': None, 'last_reported_ts': None}
            self.assertEqual(export.write_parquet(p, 'states', [[row]]), 1)
            table = pq.read_table(p)
            self.assertEqual(table.to_pylist(), [row])
            q = Path(tmp) / 'empty.parquet'
            self.assertEqual(export.write_parquet(q, 'states', []), 0)
            self.assertEqual(table.schema, pq.read_table(q).schema)
            def broken():
                yield [row]
                raise ValueError('source failed')
            r = Path(tmp) / 'failed.parquet'
            with self.assertRaises(ValueError):
                export.write_parquet(r, 'states', broken())
            self.assertFalse(r.exists())

    def test_upload_skips_matching_size_and_refuses_collision(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'part.parquet'
            p.write_bytes(b'parquet-example')
            class API:
                def file_size(self, remote):
                    return self.size
                def put_file(self, remote, local):
                    self.uploaded = local.read_bytes()
            api = API()
            api.size = p.stat().st_size
            self.assertEqual(export.upload_file(api, p, '/Volumes/example'), 'skipped')
            self.assertFalse(hasattr(api, 'uploaded'))
            api.size = 1
            with self.assertRaises(ValueError):
                export.upload_file(api, p, '/Volumes/example')
            self.assertFalse(hasattr(api, 'uploaded'))
            api.size = None
            self.assertEqual(export.upload_file(api, p, '/Volumes/example'), 'uploaded')
            self.assertEqual(api.uploaded, b'parquet-example')

    def test_registry_reads_close_plain_websocket_without_context_protocol(self):
        import json
        from unittest.mock import patch
        class Socket:
            replies = iter([{'type': 'auth_required'}, {'type': 'auth_ok'},
                            {'id': 1, 'success': True, 'result': ['entity']},
                            {'id': 2, 'success': True, 'result': ['device']},
                            {'id': 3, 'success': True, 'result': ['area']}])
            closed = False
            def recv(self):
                return json.dumps(next(self.replies))
            def send(self, message):
                pass
            def close(self):
                self.closed = True
        ws = Socket()
        with patch('websocket.create_connection', return_value=ws), patch.object(export, 'op', return_value='test-token'):
            self.assertEqual(export.registries('http://example.invalid'),
                             {'entity': ['entity'], 'device': ['device'], 'area': ['area']})
        self.assertTrue(ws.closed)

    def test_upstream_value_errors_never_print_raw_details(self):
        from contextlib import redirect_stderr
        from io import StringIO
        from unittest.mock import patch
        output = StringIO()
        with patch.object(export, 'export_history', side_effect=ValueError('private upstream value')):
            with patch('sys.argv', ['ha-export', 'export']), redirect_stderr(output):
                self.assertEqual(export.main(), 1)
        self.assertNotIn('private upstream value', output.getvalue())
        self.assertIn('ValueError', output.getvalue())

    def test_verification_refuses_missing_duplicate_or_wrong_counts(self):
        expected = {'states': 42, 'statistics': 7, 'entities': 3}
        self.assertEqual(export.verify_counts(expected, [['states', '42'], ['statistics', '7'], ['entities', '3']]), expected)
        for rows in ([['states', '41'], ['statistics', '7'], ['entities', '3']],
                     [['states', '42']],
                     [['states', '42'], ['statistics', '7'], ['entities', '3'], ['entities', '3']]):
            with self.assertRaises(ValueError):
                export.verify_counts(expected, rows)


if __name__ == '__main__':
    unittest.main()
