#!/usr/bin/env bash
# The one failure mode the guidance hook has: the SDK moved and the patch no
# longer registers. Nothing in this repository can run a JavaScript test, so
# this build is the hook's only check. stdio, not --http, because that needs no
# session id and no SSE parsing. The InfluxDB environment is dummy: the
# assertion is the tool list, not a query.
#
# A real file, not an inline block in action.yml: actionlint lints workflows
# only, and does NOT reach a composite action's `run:` bodies (verified), so
# shell left inline here would be the one shell in the repository that nothing
# checks. `make check-workflows` and the workflow's `lint` job both shellcheck
# this directory.
set -euo pipefail

printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"ci","version":"0"}}}' \
  '{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}' \
  | timeout 60 docker run -i --rm \
      -e INFLUXDB_URL=http://127.0.0.1:8086 \
      -e INFLUXDB_TOKEN=not-a-real-token \
      -e INFLUXDB_ORG=ci \
      "$IMAGE" > tools.json || true

cat tools.json
grep -q 'how-to-use-health-data' tools.json
