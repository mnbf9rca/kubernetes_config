#!/bin/sh
# The compressed SQL is the recovery artifact; restic copies the dump PVC at 03:00Z.
set -eu
umask 077
DUMP_DIR=${DUMP_DIR:-/dumps}
MIN_BYTES=360000 # one tenth of the 3,611,341 B / 40-table seed dump, 2026-10-09
KEEP=7
stamp=$(date -u +%Y%m%d%H%M%S)
raw="$DUMP_DIR/.netdisco-$stamp.$$.raw"
tmp="$DUMP_DIR/.netdisco-$stamp.$$.gz.tmp"
out="$DUMP_DIR/netdisco-$stamp-$$.sql.gz"

finish() {
  rc=$?
  trap - EXIT
  rm -f "$raw" "$tmp" 2>/dev/null || true
  if [ "$rc" -eq 0 ]; then status=up; verdict=ok; else status=down; verdict=failed; fi
  # wget diagnostics can quote the token-bearing URL; never print them.
  wget -q -T 15 -O /dev/null "$PUSH_URL?status=$status&msg=verdict%3D$verdict" >/dev/null 2>&1 || echo 'kuma push failed' >&2
  exit "$rc"
}
trap finish EXIT

# --file keeps pg_dump's exit status separate from gzip's.
pg_dump -h netdisco-postgres -U netdisco -d netdisco --clean --if-exists --file="$raw" >/dev/null 2>&1
if ! grep -q '^CREATE TABLE ' "$raw"; then
  echo 'netdisco dump has no tables' >&2
  exit 1
fi
gzip -c "$raw" > "$tmp"
bytes=$(wc -c < "$tmp")
if [ "$bytes" -lt "$MIN_BYTES" ]; then
  echo 'netdisco dump below measured floor' >&2
  exit 1
fi
mv "$tmp" "$out"
rm -f "$raw"

set -- "$DUMP_DIR"/netdisco-*.sql.gz
count=0
for file do [ -f "$file" ] && count=$((count + 1)); done
for file do
  [ "$count" -le "$KEEP" ] && break
  rm -f "$file"
  count=$((count - 1))
done
echo "netdisco dump: tables present, bytes=$bytes, kept=$count"
