#!/usr/bin/env python3
"""Remove only expired finalized reports whose own native board task is closed."""
import argparse
from pathlib import Path
import re
import shutil
import sqlite3
import time

REPORT_ROOT=Path('/home/hermes/.hermes/profiles/security_reviewer/workspace/reports')
BOARD_DB=Path('/home/hermes/.hermes/kanban/boards/security_reviewer/kanban.db')

def task_status(conn,task):
    row=conn.execute('SELECT status FROM tasks WHERE id=?',(task,)).fetchone()
    return row[0] if row else None

def sweep(report_root,board_db,*,dry_run,now=None):
    now=time.time() if now is None else now
    counts=dict(examined=0,deleted=0,skipped=0,errors=0)
    root=Path(report_root);db=Path(board_db)
    conn=None
    try:
        if root.is_symlink() or root.resolve()!=root.absolute() or not root.is_dir():
            raise ValueError('invalid report root')
        conn=sqlite3.connect(db.resolve().as_uri()+'?mode=ro',uri=True)
        conn.execute('PRAGMA query_only=ON')
        conn.execute('SELECT id,status FROM tasks LIMIT 0')
        for directory in root.iterdir():
            counts['examined']+=1
            report=directory/'report.md'
            if (not re.fullmatch(r't_[a-f0-9]{8,}',directory.name) or directory.is_symlink()
                or not directory.is_dir() or report.is_symlink() or not report.is_file()
                or report.stat().st_mtime>=now-30*86400
                or any(p.is_symlink() for p in directory.rglob('*'))):
                counts['skipped']+=1
                continue
            if task_status(conn,directory.name) not in ('done','archived') or dry_run:
                counts['skipped']+=1
                continue
            # Do not cache the earlier closed state across filesystem inspection.
            if task_status(conn,directory.name) not in ('done','archived'):
                counts['skipped']+=1
                continue
            shutil.rmtree(directory)
            counts['deleted']+=1
    except (OSError,sqlite3.Error,ValueError):
        counts['errors']+=1
    finally:
        if conn is not None:conn.close()
    return counts

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run',action='store_true')
    args=parser.parse_args();counts=sweep(REPORT_ROOT,BOARD_DB,dry_run=args.dry_run)
    verdict='error' if counts['errors'] else 'dry-run' if args.dry_run else 'ok'
    print('verdict='+verdict+' '+' '.join(f'{k}={v}' for k,v in counts.items()))
    return int(bool(counts['errors']))

if __name__=='__main__':raise SystemExit(main())
