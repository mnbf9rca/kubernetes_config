import contextlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

# The production module imports pinned Hermes interfaces; tests stub only those boundaries.
kt=types.ModuleType('tools.kanban_tools')
kt._default_task_id=lambda _:os.environ.get('HERMES_KANBAN_TASK')
kt._worker_run_id=lambda _:int(os.environ['HERMES_KANBAN_RUN_ID']) if os.environ.get('HERMES_KANBAN_RUN_ID') else None
kt._reject_delegated_child_mutation=lambda _:None
kt._is_dispatcher_owned_worker=lambda:True
kt._board=lambda _:None
profiles=types.ModuleType('hermes_cli.profiles');profiles.current_profile_name=lambda _:'security_reviewer'
session=types.ModuleType('gateway.session_kanban');session._bound_claim_is_live=lambda _:True
registry=types.ModuleType('tools.registry');registry.tool_error=lambda msg:json.dumps({'error':msg})
for name,module in [('tools.kanban_tools',kt),('hermes_cli.profiles',profiles),('gateway.session_kanban',session),('tools.registry',registry)]:sys.modules[name]=module
spec=importlib.util.spec_from_file_location('request_plugin',Path(__file__).with_name('__init__.py'))
plugin=importlib.util.module_from_spec(spec);spec.loader.exec_module(plugin)

class RequestTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.root=Path(self.tmp.name).resolve();plugin.REPORT_ROOT=self.root/'reports';plugin.BOARD_DB=self.root/'kanban.db'
  self.report=plugin.REPORT_ROOT/'t_12345678'/'report.md';self.report.parent.mkdir(parents=True);self.report.write_text('Tooling gaps: scanner absent')
  self.env=patch.dict(os.environ,{'HERMES_KANBAN_TASK':'t_12345678','HERMES_KANBAN_RUN_ID':'7','HERMES_KANBAN_BOARD':'security_reviewer','HERMES_KANBAN_DB':str(plugin.BOARD_DB),'HERMES_KANBAN_CLAIM_LOCK':'claim'},clear=True);self.env.start();self.addCleanup(self.env.stop)
  self.calls=[];self.rows={}
  def create(conn,**kw):
   self.calls.append(kw);self.rows.setdefault(kw['idempotency_key'],types.SimpleNamespace(id='t_abcdef12',status='blocked'));return self.rows[kw['idempotency_key']].id
  self.kb=types.SimpleNamespace(create_task=create,get_task=lambda conn,tid:next(iter(self.rows.values())))
  @contextlib.contextmanager
  def board(name):self.assertEqual(name,'security_reviewer');yield self.kb,None
  self.p=patch.object(plugin,'_board',board);self.p.start();self.addCleanup(self.p.stop)
 def args(self,**kw):return {'tool':'CodeQL','reason':'Cannot assess the requested dataflow without this tool.','report_path':str(self.report),**kw}
 def test_native_unassigned_blocked_idempotent(self):
  a=json.loads(plugin.handle_request(self.args()));b=json.loads(plugin.handle_request(self.args(tool='codeql')))
  self.assertEqual(a['task_id'],b['task_id']);self.assertEqual(a['status'],'blocked')
  for kw in self.calls:
   self.assertIsNone(kw['assignee']);self.assertEqual(kw['initial_status'],'blocked');self.assertEqual(kw['parents'],());self.assertEqual(kw['idempotency_key'],'security-reviewer-tooling:codeql')
  next(iter(self.rows.values())).status='done'
  self.assertEqual(json.loads(plugin.handle_request(self.args()))['status'],'done')
 def test_invalid_context_or_payload_never_opens_board(self):
  for key in ('HERMES_KANBAN_TASK','HERMES_KANBAN_RUN_ID','HERMES_KANBAN_CLAIM_LOCK','HERMES_KANBAN_DB','HERMES_KANBAN_BOARD'):
   with self.subTest(key=key),patch.dict(os.environ,{key:'wrong'}),patch.object(plugin,'_board',side_effect=AssertionError('board opened')):
    self.assertIn('error',json.loads(plugin.handle_request(self.args())))
  for kw in ({'tool':'x\nassignee=hal'},{'tool':'x'*65},{'reason':''},{'reason':'x'*4001},{'report_path':str(self.root/'outside')},{'board':'other'}):
   with self.subTest(kw=list(kw)),patch.object(plugin,'_board',side_effect=AssertionError('board opened')):
    self.assertIn('error',json.loads(plugin.handle_request(self.args(**kw))))
  with patch.object(plugin,'current_profile_name',return_value='hal'),patch.object(plugin,'_board',side_effect=AssertionError('board opened')):
   self.assertIn('error',json.loads(plugin.handle_request(self.args())))
  with patch.object(plugin,'_is_dispatcher_owned_worker',return_value=False):self.assertIn('error',json.loads(plugin.handle_request(self.args())))
  self.report.unlink();self.report.symlink_to('/etc/passwd')
  self.assertIn('error',json.loads(plugin.handle_request(self.args())))
 def test_registration_adds_one_without_override(self):
  calls=[];plugin.register(types.SimpleNamespace(register_tool=lambda **kw:calls.append(kw) or object()))
  self.assertEqual(len(calls),1);self.assertEqual(calls[0]['toolset'],'kanban');self.assertNotIn('override',calls[0])
  with self.assertRaises(RuntimeError):plugin.register(types.SimpleNamespace(register_tool=lambda **kw:None))

if __name__=='__main__':unittest.main()
