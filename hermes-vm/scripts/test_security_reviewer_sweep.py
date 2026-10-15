import hashlib
import importlib.util
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch
spec=importlib.util.spec_from_file_location('sweep',Path(__file__).with_name('security-reviewer-sweep.py'))
sweep=importlib.util.module_from_spec(spec);spec.loader.exec_module(sweep)

class SweepTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name).resolve();self.reports=self.root/'reports';self.reports.mkdir();self.db=self.root/'board.db'
  with sqlite3.connect(self.db) as c:c.execute('create table tasks (id text primary key,status text)')
  self.now=time.time()
 def add(self,n,status,old=True,final=True):
  task='t_'+f'{n:08x}';directory=self.reports/task;directory.mkdir();p=directory/('report.md' if final else 'report.tmp');p.write_text('fixture')
  if old:os.utime(p,(self.now-31*86400,)*2)
  if status:
   with sqlite3.connect(self.db) as c:c.execute('insert into tasks values (?,?)',(task,status))
  return directory
 def test_old_closed_only(self):
  deleted=[self.add(1,'done'),self.add(2,'archived')]
  kept=[self.add(i,s) for i,s in enumerate(['running','ready','blocked','todo','review','unknown',None],3)]
  kept += [self.add(10,'done',False),self.add(11,'done',final=False)]
  before=hashlib.sha256(self.db.read_bytes()).digest()
  result=sweep.sweep(self.reports,self.db,dry_run=False,now=self.now)
  self.assertEqual(result['deleted'],2)
  self.assertTrue(all(not p.exists() for p in deleted));self.assertTrue(all(p.exists() for p in kept));self.assertEqual(hashlib.sha256(self.db.read_bytes()).digest(),before)
 def test_scope_and_read_failure_keep(self):
  directory=self.add(1,'done');outside=self.root/'outside';outside.mkdir();(self.reports/'t_11111111').symlink_to(outside,target_is_directory=True)
  (directory/'link').symlink_to(outside,target_is_directory=True)
  self.assertEqual(sweep.sweep(self.reports,self.db,dry_run=False,now=self.now)['deleted'],0)
  self.assertTrue(directory.exists());self.assertTrue(outside.exists())
  missing=self.root/'missing.db'
  self.assertGreater(sweep.sweep(self.reports,missing,dry_run=False,now=self.now)['errors'],0);self.assertFalse(missing.exists())
  with sqlite3.connect(self.db) as c:c.execute('drop table tasks')
  self.assertGreater(sweep.sweep(self.reports,self.db,dry_run=False,now=self.now)['errors'],0);self.assertTrue(directory.exists())
 def test_dry_run_and_status_recheck(self):
  directory=self.add(1,'done');self.assertEqual(sweep.sweep(self.reports,self.db,dry_run=True,now=self.now)['deleted'],0);self.assertTrue(directory.exists())
  original=sweep.task_status;calls=[]
  def reopened(conn,task):
   calls.append(task);return original(conn,task) if len(calls)==1 else 'running'
  with patch.object(sweep,'task_status',reopened):self.assertEqual(sweep.sweep(self.reports,self.db,dry_run=False,now=self.now)['deleted'],0)
  self.assertEqual(len(calls),2);self.assertTrue(directory.exists())

if __name__=='__main__':unittest.main()
