#!/usr/bin/env python3
"""Export sensor recorder history locally, then upload raw Parquet to a volume.

Dependencies: pymysql, pyarrow, websocket-client. Credentials are read from
1Password for each invocation; upstream errors/URLs never enter the log.
"""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

UTC = timezone.utc
VOLUME = '/Volumes/workspace/home_assistant/raw'
LABELS = ('friendly_name', 'unit_of_measurement', 'device_class', 'state_class')
DATASETS = ('states', 'statistics', 'entities')
TERMS = ('phone', 'smartphone', 'iphone', 'ipad', 'geocode', 'geocoded',
         'location', 'latitude', 'longitude', 'gps', 'ssid', 'bssid',
         'last seen', 'presence', 'occupancy', 'person', 'device tracker',
         'travel', 'distance', 'odometer', 'next departure', 'park event',
         'away end', 'motion', 'door', 'window')
PHYSICAL = ('temperature', 'humidity', 'power', 'energy', 'current', 'voltage',
            'memory', 'rssi', 'uptime')
FILE_PATTERN = re.compile(r'(states|statistics|entities)/date=(\d{4}-\d{2}-\d{2})/part\.parquet')


class ExportError(ValueError):
    """A deliberately safe message authored by this script."""


def exclusion(entity):
    eid = entity['entity_id']
    platform = entity.get('platform')
    if not eid.startswith('sensor.'):
        return 'outside sensor domain'
    if (platform in ('mobile_app', 'mbapi2020')
            or re.match(r'^sensor\.[a-z0-9_]*(?:iphone|ipad|phone)[0-9]*_', eid)
            or re.match(r'^sensor\.[a-z]{2}[0-9]{2}[a-z]{3}_', eid)):
        return 'phone or vehicle telemetry'
    if platform == 'ohme' or eid.startswith('sensor.ohme_'):
        return 'EV charging can reveal vehicle presence'
    if (platform in ('hass_claude_usage', 'codex_usage')
            or eid.startswith(('sensor.claude_usage_', 'sensor.codex_usage_'))):
        return 'doubtful keyboard activity telemetry'
    if eid == 'sensor.roomba_last_mission_start_time':
        return 'vacuum start can follow departure'
    if eid.startswith('sensor.lenovo_tb328fu_'):
        return 'doubtful kiosk activity telemetry'
    if platform == 'sun':
        return 'solar timing can disclose location'
    fields = [entity.get(k) for k in ('entity_id', 'friendly_name', 'platform', 'device_name')]
    fields += entity.get('attribute_keys', [])
    words = [' ' + ' '.join(re.findall('[a-z0-9]+', str(v or '').lower())) + ' '
             for v in fields]
    for term in TERMS:
        if any(' ' + term + ' ' in field for field in words):
            return 'explicit presence/location term: ' + term
    if not platform and not any(entity.get(k) for k in LABELS[:3]):
        if not eid.endswith(tuple('_' + word for word in PHYSICAL)):
            return 'insufficient metadata for ambiguous retired sensor'
    return None


def labels(attributes):
    return {key: attributes.get(key) if isinstance(attributes.get(key), str) else None
            for key in LABELS}


def utc_date(value):
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise ExportError('date must have YYYY-MM-DD format')
    try:
        return datetime.strptime(value, '%Y-%m-%d').replace(tzinfo=UTC).timestamp()
    except ValueError:
        raise ExportError('invalid calendar date') from None


def iso(ts):
    return datetime.fromtimestamp(ts, UTC).isoformat()


def day(ts):
    return datetime.fromtimestamp(ts, UTC).date().isoformat()


def chunks(start, end, seconds):
    if start >= end or seconds <= 0:
        raise ExportError('date range must be nonempty and increasing')
    while start < end:
        stop = min(end, (start // seconds + 1) * seconds)
        yield start, stop
        start = stop


def schema(dataset):
    import pyarrow as pa
    integer, number, text, boolean = pa.int64(), pa.float64(), pa.string(), pa.bool_()
    if dataset == 'states':
        fields = [('state_id', integer), ('metadata_id', integer), ('entity_id', text),
                  ('state', text), ('last_changed_ts', number),
                  ('last_updated_ts', number), ('last_reported_ts', number)]
    elif dataset == 'statistics':
        fields = [('id', integer), ('metadata_id', integer)]
        fields += [(k, number) for k in ('created_ts', 'start_ts', 'mean', 'min', 'max',
                                         'last_reset_ts', 'state', 'sum', 'mean_weight')]
        fields += [(k, text) for k in ('statistic_id', 'source', 'unit_of_measurement', 'name', 'unit_class')]
        fields += [('has_mean', boolean), ('has_sum', boolean), ('mean_type', integer)]
    else:
        fields = [('metadata_id', integer), ('entity_id', text)]
        fields += [(k, text) for k in (*LABELS, 'device_name', 'area_name', 'platform')]
        fields += [('snapshot_ts', number)]
    return pa.schema(fields)


def write_parquet(path, dataset, batches):
    import pyarrow as pa
    import pyarrow.parquet as pq
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.parquet.partial')
    count = 0
    with pq.ParquetWriter(temporary, schema(dataset), compression='zstd') as writer:
        for batch in batches:
            writer.write_table(pa.Table.from_pylist(batch, schema=schema(dataset)))
            count += len(batch)
    temporary.replace(path)
    return count


def op(*arguments):
    result = subprocess.run(['op', *arguments], capture_output=True, timeout=30)
    if result.returncode:
        raise ExportError('1Password credential read failed; upstream output withheld')
    return result.stdout.decode().strip()


def item_origin(item):
    value = json.loads(op('item', 'get', item, '--vault', 'Homelab', '--format', 'json'))
    url = urllib.parse.urlsplit(value['urls'][0]['href'])
    if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password:
        raise ExportError('invalid item URL; value withheld')
    if item == 'databricks' and url.scheme != 'https':
        raise ExportError('Databricks requires HTTPS')
    return urllib.parse.urlunsplit((url.scheme, url.netloc, '', '', ''))


def registries(origin):
    import websocket
    url = origin.replace('https://', 'wss://').replace('http://', 'ws://') + '/api/websocket'
    with closing(websocket.create_connection(url, timeout=60)) as ws:
        if json.loads(ws.recv()).get('type') != 'auth_required':
            raise ExportError('Home Assistant authentication protocol failed')
        ws.send(json.dumps({'type': 'auth', 'access_token': op('read', 'op://Homelab/homeassistant/access_token')}))
        if json.loads(ws.recv()).get('type') != 'auth_ok':
            raise ExportError('Home Assistant credential failed')
        result = {}
        for ident, name in enumerate(('entity', 'device', 'area'), 1):
            ws.send(json.dumps({'id': ident, 'type': 'config/' + name + '_registry/list'}))
            reply = json.loads(ws.recv())
            if reply.get('id') != ident or not reply.get('success'):
                raise ExportError('Home Assistant registry read failed; response withheld')
            result[name] = reply['result']
    return result


def inventory(connection, registry, snapshot):
    entities = {row['entity_id']: row for row in registry['entity']}
    devices = {row['id']: row for row in registry['device']}
    areas = {row['area_id']: row['name'] for row in registry['area']}
    with connection.cursor() as cursor:
        cursor.execute("SELECT metadata_id,entity_id FROM states_meta WHERE entity_id LIKE 'sensor.%'")
        source = cursor.fetchall()
        cursor.execute("SELECT id,statistic_id,source,unit_of_measurement,has_mean,has_sum,name,mean_type,unit_class FROM statistics_meta WHERE statistic_id LIKE 'sensor.%'")
        statistics = cursor.fetchall()
    admitted, excluded = [], []
    for row in source:
        with connection.cursor() as cursor:
            cursor.execute('SELECT a.shared_attrs FROM states s LEFT JOIN state_attributes a ON a.attributes_id=s.attributes_id WHERE s.metadata_id=%s ORDER BY s.last_updated_ts DESC LIMIT 1', (row['metadata_id'],))
            latest = cursor.fetchone()
        attributes = json.loads(latest['shared_attrs']) if latest and latest['shared_attrs'] else {}
        entry = entities.get(row['entity_id'], {})
        device = devices.get(entry.get('device_id'), {})
        metadata = {**row, **labels(attributes), 'platform': entry.get('platform'),
                    'device_name': device.get('name_by_user') or device.get('name'),
                    'area_name': areas.get(entry.get('area_id') or device.get('area_id')),
                    'snapshot_ts': snapshot}
        reason = exclusion({**metadata, 'attribute_keys': list(attributes)})
        if reason:
            excluded.append({'entity_id': row['entity_id'], 'platform': metadata['platform'], 'reason': reason})
        else:
            admitted.append(metadata)
    known = {row['entity_id'] for row in source}
    if any(row['statistic_id'] not in known for row in statistics):
        raise ExportError('unreviewed statistics-only entity; inspect metadata before export')
    return admitted, excluded, statistics


def save_manifest(staging, manifest):
    temporary = staging / 'manifest.json.partial'
    temporary.write_text(json.dumps(manifest, indent=2) + '\n')
    temporary.replace(staging / 'manifest.json')


def export_history(args):
    import pymysql
    snapshot = time.time()
    end = min(utc_date(args.end), snapshot) if args.end else snapshot
    start = utc_date(args.start) if args.start else None
    if start is not None and start >= end:
        raise ExportError('date range must be nonempty and increasing')
    staging = Path(args.staging).expanduser().resolve() if args.staging else Path(tempfile.mkdtemp(prefix='ha-databricks-export-', dir='/private/tmp'))
    checkout = Path(__file__).resolve().parents[1]
    worktrees = subprocess.run(['git', '-C', str(checkout), 'worktree', 'list', '--porcelain'],
                               capture_output=True, text=True, check=True).stdout
    for line in worktrees.splitlines():
        if line.startswith('worktree '):
            repository = Path(line.removeprefix('worktree ')).resolve()
            if staging == repository or repository in staging.parents:
                raise ExportError('staging must be outside every repository checkout')
    if staging.exists() and any(staging.iterdir()):
        raise ExportError('staging directory must be empty')
    staging.mkdir(parents=True, exist_ok=True, mode=0o700)
    origin = item_origin('homeassistant')
    connection = pymysql.connect(host=urllib.parse.urlsplit(origin).hostname,
                                 user=op('read', 'op://Homelab/homeassistant/mariadb_username'),
                                 password=op('read', 'op://Homelab/homeassistant/mariadb_password'),
                                 database='homeassistant', connect_timeout=15, read_timeout=60,
                                 autocommit=True, cursorclass=pymysql.cursors.DictCursor)
    manifest = {'version': 1, 'complete': False, 'requested_start': args.start,
                'requested_end': args.end, 'cutoff': iso(end), 'snapshot': iso(snapshot),
                'files': [], 'totals': dict.fromkeys(DATASETS, 0)}
    with connection:
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
            cursor.fetchone()
            cursor.execute('SET SESSION max_statement_time=30')
            cursor.execute('SET SESSION TRANSACTION READ ONLY')
        admitted, excluded, statistics = inventory(connection, registries(origin), snapshot)
        manifest.update(admitted=admitted, excluded=excluded)
        save_manifest(staging, manifest)
        ids = {row['metadata_id']: row['entity_id'] for row in admitted}
        safe = set(ids.values())
        statistics = {row['id']: row for row in statistics if row['statistic_id'] in safe}
        print(f'Inventory: admitted={len(admitted)} excluded={len(excluded)} statistics={len(statistics)}', flush=True)
        if not ids or not statistics:
            raise ExportError('no admitted entities or statistics; inspect inventory')
        queries = {
            'states': ('states', 'last_updated_ts', 'ix_states_last_updated_ts',
                       'state_id,metadata_id,state,last_changed_ts,last_updated_ts,last_reported_ts', ids, 3600),
            'statistics': ('statistics', 'start_ts', 'ix_statistics_start_ts',
                           'id,metadata_id,created_ts,start_ts,mean,min,max,last_reset_ts,state,sum,mean_weight', statistics, 86400),
        }
        for dataset, (table, column, index, columns, metadata, window) in queries.items():
            with connection.cursor() as cursor:
                cursor.execute(f'SELECT {column} AS ts FROM {table} WHERE {column} IS NOT NULL ORDER BY {column} LIMIT 1')
                earliest = cursor.fetchone()
            if not earliest:
                raise ExportError('source dataset is empty')
            lower = max(start if start is not None else utc_date(day(earliest['ts'])), utc_date(day(earliest['ts'])))
            if lower >= end:
                raise ExportError('requested interval precedes retained dataset')
            placeholders = ','.join(['%s'] * len(metadata))
            sql = f'SELECT {columns} FROM {table} FORCE INDEX ({index}) WHERE {column}>=%s AND {column}<%s AND metadata_id IN ({placeholders})'
            manifest.setdefault('intervals', {})[dataset] = [iso(lower), iso(end)]
            for first, stop in chunks(lower, end, 86400):
                started = time.monotonic()
                def batches():
                    for left, right in chunks(first, stop, window):
                        try:
                            with connection.cursor(pymysql.cursors.SSDictCursor) as cursor:
                                cursor.execute(sql, (left, right, *metadata))
                                while batch := cursor.fetchmany(5000):
                                    for row in batch:
                                        if dataset == 'states':
                                            row['entity_id'] = ids[row['metadata_id']]
                                        else:
                                            row.update({k: v for k, v in statistics[row['metadata_id']].items() if k != 'id'})
                                            for key in ('has_mean', 'has_sum'):
                                                row[key] = None if row[key] is None else bool(row[key])
                                    yield batch
                        except Exception:
                            print(f'Source failed: dataset={dataset} interval={iso(left)}..{iso(right)}; export stopped', flush=True)
                            raise
                        time.sleep(0.1)
                relative = f'{dataset}/date={day(first)}/part.parquet'
                path = staging / relative
                count = write_parquet(path, dataset, batches())
                manifest['files'].append({'path': relative, 'rows': count, 'bytes': path.stat().st_size})
                manifest['totals'][dataset] += count
                save_manifest(staging, manifest)
                print(f'{dataset} {day(first)} rows={count} bytes={path.stat().st_size} seconds={time.monotonic()-started:.1f}', flush=True)
        relative = f'entities/date={day(snapshot)}/part.parquet'
        count = write_parquet(staging / relative, 'entities', [admitted])
        manifest['files'].append({'path': relative, 'rows': count, 'bytes': (staging / relative).stat().st_size})
        manifest['totals']['entities'] = count
        manifest['complete'] = True
        save_manifest(staging, manifest)
    print('Export complete: ' + json.dumps(manifest['totals']) + ' staging=' + str(staging), flush=True)


class Databricks:
    def __init__(self):
        self.origin = item_origin('databricks')
        self.token = op('read', 'op://Homelab/databricks/access_token')

    def request(self, method, path, data=None, raw=False):
        headers = {'Authorization': 'Bearer ' + self.token}
        if data is not None:
            headers['Content-Type'] = 'application/octet-stream' if raw else 'application/json'
            if not raw:
                data = json.dumps(data).encode()
            else:
                headers['Content-Length'] = str(os.fstat(data.fileno()).st_size)
        request = urllib.request.Request(self.origin + path, data=data, headers=headers, method=method)
        with urllib.request.urlopen(request, timeout=60) as response:
            if method == 'HEAD':
                return dict(response.headers)
            body = response.read()
            return json.loads(body) if body else {}

    def file_size(self, remote):
        try:
            headers = self.request('HEAD', '/api/2.0/fs/files' + remote)
            return int(next(v for k, v in headers.items() if k.lower() == 'content-length'))
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            raise

    def put_file(self, remote, local):
        with local.open('rb') as source:
            try:
                self.request('PUT', '/api/2.0/fs/files' + remote + '?overwrite=false', source, raw=True)
            except urllib.error.HTTPError as error:
                if error.code != 404:
                    raise
                self.request('PUT', '/api/2.0/fs/directories' + remote.rsplit('/', 1)[0])
                source.seek(0)
                self.request('PUT', '/api/2.0/fs/files' + remote + '?overwrite=false', source, raw=True)

    def setup(self):
        for path, payload in (
            ('schemas', {'catalog_name': 'workspace', 'name': 'home_assistant'}),
            ('volumes', {'catalog_name': 'workspace', 'schema_name': 'home_assistant',
                         'name': 'raw', 'volume_type': 'MANAGED'}),
        ):
            full = 'workspace.home_assistant' + ('.raw' if path == 'volumes' else '')
            try:
                value = self.request('GET', '/api/2.1/unity-catalog/' + path + '/' + full)
            except urllib.error.HTTPError as error:
                if error.code != 404:
                    raise
                value = self.request('POST', '/api/2.1/unity-catalog/' + path, payload)
            if path == 'volumes' and (value.get('volume_type') != 'MANAGED'
                                      or value.get('full_name') != full):
                raise ExportError('existing volume is not the expected managed destination')

    def query(self, sql):
        warehouses = self.request('GET', '/api/2.0/sql/warehouses')['warehouses']
        if len(warehouses) != 1:
            raise ExportError('expected exactly one existing warehouse')
        result = self.request('POST', '/api/2.0/sql/statements',
                              {'warehouse_id': warehouses[0]['id'], 'statement': sql,
                               'wait_timeout': '0s', 'disposition': 'INLINE', 'format': 'JSON_ARRAY'})
        deadline = time.monotonic() + 900
        ident = result['statement_id']
        while result['status']['state'] in ('PENDING', 'RUNNING'):
            if time.monotonic() > deadline:
                self.request('POST', '/api/2.0/sql/statements/' + ident + '/cancel')
                raise TimeoutError('verification exceeded 15 minutes; statement cancelled')
            time.sleep(3)
            result = self.request('GET', '/api/2.0/sql/statements/' + ident)
        if result['status']['state'] != 'SUCCEEDED':
            code = result['status'].get('error', {}).get('error_code', 'UNKNOWN')
            code = re.sub('[^A-Z_]', '', code)
            raise ExportError('Databricks verification failed: ' + code + '; upstream message withheld')
        return result['result']['data_array']


def upload_file(api, local, remote):
    size = api.file_size(remote)
    if size is not None:
        if size != local.stat().st_size:
            raise ExportError('remote file collision; overwrite refused: ' + remote)
        return 'skipped'
    api.put_file(remote, local)
    return 'uploaded'


def verify_counts(expected, rows):
    actual = {name: int(count) for name, count in rows}
    if len(rows) != len(expected) or actual != expected:
        raise ExportError('verification count mismatch: expected=' + json.dumps(expected) + ' actual=' + json.dumps(actual))
    return actual


def upload_history(args):
    staging = Path(args.staging).expanduser().resolve()
    manifest = json.loads((staging / 'manifest.json').read_text())
    if manifest.get('version') != 1 or manifest.get('complete') is not True:
        raise ExportError('only complete version-1 exports can be uploaded')
    partitions = {dataset: [] for dataset in DATASETS}
    totals = dict.fromkeys(DATASETS, 0)
    seen = set()
    for entry in manifest['files']:
        match = FILE_PATTERN.fullmatch(entry['path'])
        if not match or entry['path'] in seen:
            raise ExportError('invalid or duplicate manifest file')
        dataset, date = match.groups()
        utc_date(date)
        local = (staging / entry['path']).resolve()
        if staging not in local.parents or not local.is_file() or local.stat().st_size != entry['bytes']:
            raise ExportError('local manifest file missing, changed or outside staging')
        partitions[dataset].append(date)
        totals[dataset] += entry['rows']
        seen.add(entry['path'])
    if totals != manifest['totals'] or any(not dates for dates in partitions.values()):
        raise ExportError('manifest counts or datasets are incomplete')
    api = Databricks()
    api.setup()
    for entry in manifest['files']:
        result = upload_file(api, staging / entry['path'], VOLUME + '/' + entry['path'])
        print(result + ' ' + entry['path'], flush=True)
    statements = []
    for dataset, dates in partitions.items():
        first, stop = min(dates), day(utc_date(max(dates)) + 86400)
        statements.append(f"SELECT '{dataset}' AS dataset, count(*) AS rows FROM read_files('{VOLUME}/{dataset}', format => 'parquet') WHERE date >= DATE '{first}' AND date < DATE '{stop}'")
    sql = '\nUNION ALL\n'.join(statements)
    manifest['verification_query'] = sql
    save_manifest(staging, manifest)
    print('All uploads complete; running the single count query', flush=True)
    counts = verify_counts(totals, api.query(sql))
    manifest['verification'] = {'verdict': 'MATCH', 'counts': counts, 'time': iso(time.time())}
    save_manifest(staging, manifest)
    print('Verification MATCH: ' + json.dumps(counts), flush=True)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    export = commands.add_parser('export', help='read-only source export to local staging')
    export.add_argument('--start', help='inclusive UTC date, YYYY-MM-DD')
    export.add_argument('--end', help='exclusive UTC date, YYYY-MM-DD; default is command start')
    export.add_argument('--staging', help='empty staging directory outside the repository')
    upload = commands.add_parser('upload', help='upload a complete local export and verify counts')
    upload.add_argument('staging')
    args = parser.parse_args()
    try:
        (export_history if args.command == 'export' else upload_history)(args)
    except (KeyboardInterrupt, Exception) as error:
        if isinstance(error, ExportError):
            message = str(error)
        else:
            code = getattr(error, 'code', None)
            if code is None and error.args and isinstance(error.args[0], int):
                code = error.args[0]
            message = type(error).__name__ + (f' code={code}' if isinstance(code, int) else '')
        print('STOP: ' + message + '; upstream details withheld', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
