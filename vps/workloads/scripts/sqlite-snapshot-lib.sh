#!/bin/sh
# Shared by every sqlite quiesce sidecar in the vps namespace. Sourced, never
# executed: `. /scripts/sqlite-snapshot-lib.sh`.
#
# Publish a snapshot only if it actually contains a schema. A 0-byte or
# truncated source makes `.backup` succeed and emit a structurally valid but
# EMPTY database with a current mtime — which sails straight through the restic
# staleness gate, because mtime is the only property that gate checks.
# `count(*) from sqlite_master` is a schema-only read, cheap enough to run
# every cycle against a live DB.
#
# Publication is atomic: write `<src>.restic.tmp`, then `mv` it into place, so
# a failed run leaves the previous snapshot intact rather than truncating it.
#
# Returns 0 on a published snapshot, 1 on any failure. It never exits — see the
# `set -e` note in the callers.
snapshot() {
  src=$1
  tmp="$src.restic.tmp"
  if ! sqlite3 -cmd '.timeout 30000' "$src" ".backup $tmp"; then
    rm -f "$tmp"
    return 1
  fi
  objs=$(sqlite3 "$tmp" 'select count(*) from sqlite_master' 2>/dev/null)
  case "$objs" in
    ''|*[!0-9]*) objs=0 ;;
  esac
  if [ "$objs" -lt 1 ]; then
    echo "ERROR: snapshot of $src has no schema objects - refusing to publish" >&2
    rm -f "$tmp"
    return 1
  fi
  mv "$tmp" "$src.restic"
}

# Install sqlite3 if the image does not have it yet. Returns 1 on failure so
# the caller can back off instead of exiting; a failed `apk add` used to be
# repaired by a liveness probe, and the retry here is what replaced it.
ensure_sqlite3() {
  command -v sqlite3 >/dev/null 2>&1 && return 0
  if ! apk add --no-cache sqlite; then
    echo "ERROR: apk add sqlite failed; retrying in 5m" >&2
    return 1
  fi
}
