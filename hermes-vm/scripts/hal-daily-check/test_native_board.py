"""Installed-Hermes boundary tests in a native temporary root, never the real board."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import hal_daily_cards as c
from test_hal_daily_cards import batch,choice,envelope

@unittest.skipUnless(os.environ.get('HAL_DAILY_NATIVE_TESTS')=='1','installed native CLI only')
class NativeIntegration(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
  self.env=dict(os.environ,HERMES_KANBAN_HOME=self.temp.name)
  for key in list(self.env):
   if key.startswith('HERMES_KANBAN_') and key!='HERMES_KANBAN_HOME':self.env.pop(key)
  self.command=['/home/hermes/.local/bin/hermes','-p','hal','kanban']
  self.native(self.command+['boards','create','daily-fixture'])
  boards=json.loads(self.native(self.command+['boards','list','--json']).stdout)
  fixture=next(x for x in boards if x['slug']=='daily-fixture')
  self.assertTrue(Path(fixture['db_path']).is_relative_to(Path(self.temp.name)))
  self.board=c.NativeBoard(run=self.native,command=self.command+['--board','daily-fixture'])
 def native(self,command,**kwargs):
  kwargs.setdefault('capture_output',True);kwargs.setdefault('text',True);kwargs.setdefault('timeout',180)
  result=subprocess.run(command,env=self.env,**kwargs)
  if result.returncode:raise AssertionError('native command failed: '+str(result.returncode))
  return result
 def test_real_native_idempotency_dependencies_and_reuse(self):
  root,key=batch(self.board);result=c.plan(self.board,root,choice(key));again=c.plan(self.board,root,choice(key))
  self.assertEqual(result['assembly'],again['assembly'])
  self.assertEqual(len(self.board.all()),3)
  asm=self.board.show(result['assembly']);self.assertEqual(asm['status'],'todo')
  reader=next(x for x in self.board.all() if x['assignee']=='safer_web_reader')
  self.board._call(['complete',reader['id'],'--result',json.dumps(envelope())],json_output=False)
  self.assertEqual(next(x for x in self.board.all() if x['id']==asm['id'])['status'],'ready')
 def test_scheduled_publication_can_wake_repeatedly_without_triage(self):
  root,_=batch(self.board)
  for _ in range(3):
   self.board.schedule(root);self.assertEqual(self.board.show(root)['status'],'scheduled')
   self.board.unblock(root);self.assertEqual(self.board.show(root)['status'],'ready')
