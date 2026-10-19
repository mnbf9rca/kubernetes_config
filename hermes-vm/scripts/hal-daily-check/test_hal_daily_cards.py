import copy
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import hal_daily_cards as c
import hal_daily_check as d
from test_hal_daily_check import AT,snapshot

NATIVE_FIELDS={'id','title','body','assignee','status','priority','tenant','workspace_kind','workspace_path','branch_name','project_id','created_by','created_at','started_at','completed_at','result','skills','max_runtime_seconds','max_retries','model_override','provider_override','session_id','workflow_template_id','current_step_key','completion_contract','last_failure_error'}

class Board:
 def __init__(self):self.tasks={};self.serial=0;self.calls=[]
 def all(self):return [{k:copy.deepcopy(v) for k,v in task.items() if k in NATIVE_FIELDS} for task in self.tasks.values()]
 def show(self,tid):return {k:copy.deepcopy(v) for k,v in self.tasks[tid].items() if k in NATIVE_FIELDS}
 def create(self,title,body,assignee,key,parents=()):
  old=next((x for x in self.tasks.values() if x['idempotency_key']==key and x['status']!='archived'),None)
  if old:return old['id']
  self.serial+=1;tid=f't_{self.serial:08x}';self.tasks[tid]=dict(id=tid,title=title,body=json.dumps(body),assignee=assignee,idempotency_key=key,status='todo' if parents else 'ready',parents=list(parents),created_at=int(AT.timestamp()),result=None);return tid
 def archive(self,tid):self.tasks[tid]['status']='archived'
 def unblock(self,tid):self.tasks[tid]['status']='ready'
 def schedule(self,tid):self.tasks[tid]['status']='scheduled';self.calls.append(('schedule',tid))

def batch(board):
 r=d.findings(snapshot(),AT,AT-dt.timedelta(days=1));r['inventory']=['zha','private-canary']
 tid=board.create('assess',dict(schema='hal-daily.batch.v1',updates=r['updates'],inventory=r['inventory']), 'hal','hal-daily:batch:2026-10-18')
 return tid,r['updates'][0]['key']

def choice(key):return {key:dict(releases=[dict(component='home-assistant/core',release_version='2026.10',url='https://example.org/core')])}

def envelope(version='2026.10'):
 url='https://example.org/core'
 return dict(status='OK',reason='',sources=[url],quotes=[dict(source=url,text='A breaking change')],answer=dict(component='home-assistant/core',release_version=version,retrieval_status='ok',source_urls=[url],version_match=True,summary='Summary',security_fixes=[],breaking_changes=['Change'],migrations=[],operator_actions=[],changed_components=[],relevant_bug_fixes=[],risk='routine',confidence='high',evidence=[dict(source_url=url,quote='A breaking change',supports='breaking_changes')]))

class Tickets(unittest.TestCase):
 def test_one_morning_batch_and_changed_day_does_not_duplicate_open_pair(self):
  b=Board();r=d.findings(snapshot(),AT,AT-dt.timedelta(days=1));adapter=c.Tickets(b)
  adapter.ensure(r,{}, {},'2026-10-18',AT);adapter.ensure(r,{}, {},'2026-10-18',AT)
  adapter.ensure(r,{}, {},'2026-10-19',AT+dt.timedelta(days=1))
  self.assertEqual(len(b.tasks),1)
 def test_clear_archives_episode_and_recurrence_has_new_root(self):
  b=Board();adapter=c.Tickets(b);r=dict(updates=[],inventory=[],gaps=[],situations=[dict(key='situation',members=[dict(device_id='a',condition='offline')],line='Lamp offline')],device_complete=True)
  adapter.ensure(r,{}, {},'2026-10-18',AT);first=list(b.tasks)[0];b.tasks[first]['status']='done'
  adapter.ensure(dict(r,situations=[]),{}, {},'2026-10-19',AT);adapter.ensure(r,{}, {},'2026-10-20',AT)
  self.assertEqual(b.tasks[first]['status'],'archived');self.assertEqual(len(b.tasks),2)

class Graph(unittest.TestCase):
 def test_plan_uses_native_create_public_only_and_dependencies(self):
  b=Board();root,key=batch(b)
  out=getattr(c,'plan',lambda *a,**k:{})(b,root,choice(key))
  self.assertIn('assembly',out)
  readers=[t for t in b.tasks.values() if t['assignee']=='safer_web_reader']
  self.assertEqual(len(readers),1);self.assertNotIn('private-canary',readers[0]['body'])
  self.assertEqual(b.tasks[out['assembly']]['parents'],[readers[0]['id']])
  c.plan(b,root,choice(key));self.assertEqual(len(b.tasks),3)

class ReaderRequest(unittest.TestCase):
 def test_emitted_planner_and_reader_ask_for_household_summary(self):
  b=Board();report=d.findings(snapshot(),AT,AT-dt.timedelta(days=1));report['inventory']=['private-canary']
  c.Tickets(b).ensure(report,{}, {},'2026-10-18',AT)
  root=next(t for t in b.tasks.values() if json.loads(t['body'])['schema']=='hal-daily.batch.v1')
  planner=json.loads(root['body'])['instruction'];key=report['updates'][0]['key']
  c.plan(b,root['id'],choice(key))
  reader=next(t for t in b.tasks.values() if t['assignee']=='safer_web_reader')
  instruction=json.loads(reader['body'])['instruction']
  for text in (planner,instruction):
   for requirement in ('household update question','breaking changes and migration steps in full','exact wording','before or after upgrading','summary'):
    self.assertIn(requirement,text)
  self.assertIn('changed_components includes only',instruction)
  self.assertIn('Do not enumerate every changed component',instruction)
  self.assertNotIn('private-canary',reader['body'])
  self.assertNotIn('return every named changed component',instruction)
 def test_emitted_reader_names_five_plain_string_lists(self):
  b=Board();root,key=batch(b);c.plan(b,root,choice(key))
  reader=next(t for t in b.tasks.values() if t['assignee']=='safer_web_reader')
  instruction=json.loads(reader['body'])['instruction']
  self.assertIn('security_fixes, breaking_changes, migrations, operator_actions and relevant_bug_fixes are lists of plain strings, one item per change',instruction)
  self.assertIn('component name first in the string if useful',instruction)
 def test_revised_request_gets_new_reader_without_resetting_old_result(self):
  b=Board();root,key=batch(b);c.plan(b,root,choice(key))
  old=next(t for t in b.tasks.values() if t['assignee']=='safer_web_reader');old['status']='done';old['result']=json.dumps(envelope())
  other=b.create('new batch',json.loads(b.tasks[root]['body']),'hal','hal-daily:batch:next')
  with patch.object(c,'READER_INSTRUCTION',c.READER_INSTRUCTION+' Revised summary request.'):
   c.plan(b,other,choice(key));c.plan(b,other,choice(key))
  readers=[t for t in b.tasks.values() if t['assignee']=='safer_web_reader']
  self.assertEqual(len(readers),2);self.assertEqual(old['status'],'done');self.assertEqual(old['result'],json.dumps(envelope()))
  assembly=next(t for t in b.tasks.values() if json.loads(t['body']).get('root_id')==other)
  self.assertNotEqual(json.loads(assembly['body'])['readers'][key],[old['id']])

class Envelopes(unittest.TestCase):
 def test_correct_then_malformed_and_unassessed(self):
  body=dict(component='home-assistant/core',release_version='2026.10',url='https://example.org/core')
  self.assertIsInstance(c.validate_reader(json.dumps(envelope()),body),dict)
  for mutation in ({'status':'UNASSESSED'},{'extra':1},{'answer':{}},{'sources':['https://bad.example/page']}):
   with self.subTest(mutation=mutation):
    bad=envelope();bad.update(mutation)
    with self.assertRaises(ValueError):c.validate_reader(json.dumps(bad),body)
 def test_wrong_version_and_duplicate_keys_refused(self):
  body=dict(component='home-assistant/core',release_version='2026.10',url='https://example.org/core')
  with self.assertRaises(ValueError):c.validate_reader(json.dumps(envelope('2025.9')),body)
  with self.assertRaises(ValueError):c.validate_reader('{"status":"OK","status":"OK"}',body)

class ReaderListTypes(unittest.TestCase):
 def test_exact_trial_three_breaking_object_shape_in_key_order(self):
  value=envelope();value['answer']['breaking_changes']=[
   {'component':'Authentication','change':'Legacy authentication-provider mode is removed. Usernames are always lowercase with leading/trailing spaces removed; resolve existing non-normalized-username repairs.'},
   {'change':'Allow a list of states with a duration','component':'State conditions'}]
  expected=dict(component='home-assistant/core',release_version='2026.10',url='https://example.org/core')
  answer=c.validate_reader(json.dumps(value),expected)
  self.assertEqual(answer['breaking_changes'],[
   'Authentication: Legacy authentication-provider mode is removed. Usernames are always lowercase with leading/trailing spaces removed; resolve existing non-normalized-username repairs.',
   'Allow a list of states with a duration: State conditions'])
 def test_five_lists_accept_only_strings_or_all_string_objects(self):
  expected=dict(component='home-assistant/core',release_version='2026.10',url='https://example.org/core')
  for field in ('security_fixes','breaking_changes','migrations','operator_actions','relevant_bug_fixes'):
   with self.subTest(field=field):
    value=envelope();value['answer'][field]=['Keep this text',{'component':'MQTT','change':'Check triggers','step':'Restart'},{}]
    self.assertEqual(c.validate_reader(json.dumps(value),expected)[field],['Keep this text','MQTT: Check triggers: Restart',''])
   for bad in (None,False,4,['nested'],{'change':None},{'change':False},{'change':4},{'change':['nested']},{'change':{'nested':'text'}}):
    with self.subTest(field=field,bad=bad):
     value=envelope();value['answer'][field]=[bad]
     with self.assertRaises(ValueError):c.validate_reader(json.dumps(value),expected)
   for bad in ('not a list',{'component':'MQTT','change':'Check triggers'},None):
    with self.subTest(field=field,whole_field=bad):
     value=envelope();value['answer'][field]=bad
     with self.assertRaises(ValueError):c.validate_reader(json.dumps(value),expected)

class ReaderIdentityShape(unittest.TestCase):
 def test_boolean_and_textual_true_spellings(self):
  expected=dict(component='home-assistant/core',release_version='2026.10.2',url='https://example.org/core')
  for flag in (True,'true',' TRUE '):
   with self.subTest(flag=flag):
    value=envelope('2026.10.2');value['answer']['version_match']=flag
    self.assertIs(c.validate_reader(json.dumps(value),expected)['version_match'],True)
 def test_negative_unknown_and_wrong_identity_still_refused(self):
  expected=dict(component='home-assistant/core',release_version='2026.10.2',url='https://example.org/core')
  for flag in (False,None,0,1,{},'false','unknown','Confirmed: 2026.10.20','Confirmed: not 2026.10.2','Confirmed: 2026.10.2 is a mismatch','2026.10.2',
   "Confirmed: the post doesn't include 2026.10.2",'Confirmed: 2026.10.2 is missing from the post',
   'Confirmed: cannot find 2026.10.2',
   'Confirmed: the monthly 2026.10 release post explicitly includes 2026.10.2 - October 16. This assessment includes monthly changes and patches through that version.'):
   with self.subTest(flag=flag):
    value=envelope('2026.10.2');value['answer']['version_match']=flag
    with self.assertRaises(ValueError):c.validate_reader(json.dumps(value),expected)
  for field,bad in (('component','wrong component'),('release_version','2026.10.3'),('retrieval_status','partial'),('risk','unassessed')):
   with self.subTest(field=field):
    value=envelope('2026.10.2');value['answer'].update(version_match='true',**{field:bad})
    with self.assertRaises(ValueError):c.validate_reader(json.dumps(value),expected)
 def test_emitted_instruction_explains_identity_and_boolean(self):
  b=Board();root,key=batch(b);c.plan(b,root,choice(key))
  reader=next(t for t in b.tasks.values() if t['assignee']=='safer_web_reader');instruction=json.loads(reader['body'])['instruction']
  self.assertIn('Copy component and release_version exactly from this task',instruction)
  self.assertIn('version_match is a JSON Boolean true or false',instruction)
  self.assertIn('Put explanations in summary',instruction)

class Publication(unittest.TestCase):
 def prepare(self,b):
  root,key=batch(b);out=c.plan(b,root,choice(key));reader=next(t for t in b.tasks.values() if t['assignee']=='safer_web_reader')
  reader['status']='done';reader['result']=json.dumps(envelope());return out['assembly'],key,reader
 def test_failed_reader_refuses_affected_pair_and_never_stores_its_prose(self):
  with tempfile.TemporaryDirectory() as tmp:
   b=Board();asm,key,reader=self.prepare(b);reader['result']=json.dumps(dict(status='UNASSESSED',answer={},sources=[],quotes=[],reason='failed'))
   sent=[];out=c.publish(b,asm,{key:'MUST NOT STORE'},Path(tmp),lambda *a:(sent.append(a) or {'success':True}),AT)
   self.assertIn(key,out['refused']);text=''.join(p.read_text() for p in Path(tmp).glob('*.json'));self.assertNotIn('MUST NOT STORE',text)
 def test_multi_sentence_long_assessment_one_mail_and_no_duplicate(self):
  with tempfile.TemporaryDirectory() as tmp:
   b=Board();asm,key,reader=self.prepare(b);sent=[];draft='Install after checking Zigbee. Watch its reconnect. '+('Useful detail. '*5000)
   send=lambda *a:(sent.append(a) or {'success':True})
   c.publish(b,asm,{key:draft},Path(tmp),send,AT);c.publish(b,asm,{key:draft},Path(tmp),send,AT)
   self.assertEqual(len(sent),1);self.assertIn(draft.rstrip(),sent[0][1]);self.assertTrue(json.loads((Path(tmp)/'daily-check-assessments.json').read_text())['pairs'][key]['sent_receipt'])
 def test_repeated_refusal_schedules_without_triage_and_retries_frozen(self):
  with tempfile.TemporaryDirectory() as tmp:
   b=Board();asm,key,_=self.prepare(b);attempt=[]
   send=lambda *a:(attempt.append(a) or {'success':False})
   for i in range(3):
    b.tasks[asm]['status']='running';c.publish(b,asm,{key:'Install after checking Zigbee.'},Path(tmp),send,AT)
    self.assertEqual(b.tasks[asm]['status'],'scheduled')
   self.assertEqual(attempt[0],attempt[2])
 def test_pending_only_not_emailed_daily(self):
  with tempfile.TemporaryDirectory() as tmp:
   b=Board();asm,key,reader=self.prepare(b);reader['status']='blocked';sent=[];send=lambda *a:(sent.append(a) or {'success':True})
   c.publish(b,asm,{},Path(tmp),send,AT)
   # A new morning batch with the same pending pair is a distinct assembly.
   b.tasks[asm]['idempotency_key']='old';other=b.create('assemble',json.loads(b.tasks[asm]['body']),'hal','hal-daily:assemble:other')
   c.publish(b,other,{},Path(tmp),send,AT+dt.timedelta(days=1));self.assertEqual(len(sent),1)

class Devices(unittest.TestCase):
 def test_episode_receipt_and_refused_send_park(self):
  with tempfile.TemporaryDirectory() as tmp:
   b=Board();tid=b.create('look',dict(schema='hal-daily.device.v1',situation=dict(key='s',members=[dict(device_id='x',condition='offline')],line='Lamp offline')),'hal','hal-daily:device:s')
   c.publish(b,tid,'The hub is down. Check its power.',Path(tmp),lambda *a:{'success':False},AT)
   self.assertEqual(b.tasks[tid]['status'],'scheduled')

class NativeCommand(unittest.TestCase):
 def test_create_body_stdin_idempotency_and_parent_argv(self):
  calls=[]
  def run(argv,**kwargs):
   calls.append((argv,kwargs));return type('Result',(),dict(returncode=0,stdout='{"id":"t_00000001"}',stderr=''))()
  b=c.NativeBoard(run=run,command=['hermes','-p','hal','kanban','--board','test'])
  self.assertEqual(b.create('title',{'private':'$(touch /bad)'},'hal','stable',['t_00000002']),'t_00000001')
  argv,kw=calls[0];self.assertIn('--idempotency-key',argv);self.assertIn('--parent',argv);self.assertNotIn('$(touch /bad)',' '.join(argv));self.assertIn('$(touch /bad)',kw['input'])

class Recovery(unittest.TestCase):
 def test_scheduled_and_blocked_hal_cards_wake(self):
  b=Board();root,key=batch(b);b.tasks[root]['status']='scheduled';r=d.findings(snapshot(),AT,AT-dt.timedelta(days=1))
  c.Tickets(b).ensure(r,{}, {},'2026-10-19',AT);self.assertEqual(b.tasks[root]['status'],'ready');self.assertEqual(len(b.tasks),1)

class ReleasePlan(unittest.TestCase):
 def test_missing_release_remains_pending_without_discovery(self):
  b=Board();root,key=batch(b);out=c.plan(b,root,{key:{'missing':['September release not located']}})
  self.assertIn(key,json.loads(b.tasks[out['assembly']]['body'])['missing']);self.assertEqual(len(b.tasks),2)

class Assessments(unittest.TestCase):
 def test_full_reader_envelope_is_used_beyond_native_context_prefix(self):
  body=dict(component='home-assistant/core',release_version='2026.10',url='https://example.org/core')
  value=envelope();value['answer']['summary']='x'*6000
  self.assertEqual(len(c.validate_reader(json.dumps(value),body)['summary']),6000)
 def test_delivered_pair_is_immutable_even_in_another_assembly(self):
  with tempfile.TemporaryDirectory() as tmp:
   b=Board();root,key=batch(b);asm=c.plan(b,root,choice(key))['assembly'];reader=next(x for x in b.tasks.values() if x['assignee']=='safer_web_reader')
   reader['status']='done';reader['result']=json.dumps(envelope());sent=[];send=lambda *a:(sent.append(a) or {'success':True})
   c.publish(b,asm,{key:'FIRST ADVICE'},Path(tmp),send,AT)
   newer=b.create('assemble',json.loads(b.tasks[asm]['body']),'hal','hal-daily:assemble:new')
   c.publish(b,newer,{key:'SECOND ADVICE'},Path(tmp),send,AT+dt.timedelta(days=1))
   self.assertEqual(len(sent),1);store=json.loads((Path(tmp)/'daily-check-assessments.json').read_text());self.assertEqual(store['pairs'][key]['assessment'],'FIRST ADVICE')


class FailedReaderRetry(unittest.TestCase):
 def test_completed_unassessed_reader_is_retired_for_next_attempt(self):
  with tempfile.TemporaryDirectory() as tmp:
   b=Board();root,key=batch(b);asm=c.plan(b,root,choice(key))['assembly'];reader=next(x for x in b.tasks.values() if x['assignee']=='safer_web_reader')
   reader['status']='done';reader['result']=json.dumps(dict(status='UNASSESSED',answer={},sources=[],quotes=[],reason='not fetched'))
   c.publish(b,asm,{},Path(tmp),lambda *a:{'success':True},AT)
   self.assertEqual(reader['status'],'archived')
 def test_valid_sibling_survives_failed_other_pair(self):
  with tempfile.TemporaryDirectory() as tmp:
   b=Board();root,key=batch(b);original=json.loads(b.tasks[root]['body']);second=dict(original['updates'][0],key='second',package='other',name='Other update');original['updates'].append(second);b.tasks[root]['body']=json.dumps(original)
   choices=choice(key);choices['second']={'missing':['release not located']};asm=c.plan(b,root,choices)['assembly']
   reader=next(x for x in b.tasks.values() if x['assignee']=='safer_web_reader');reader['status']='done';reader['result']=json.dumps(envelope())
   sent=[];out=c.publish(b,asm,{key:'Useful advice.','second':'INVALID'},Path(tmp),lambda *a:(sent.append(a) or {'success':True}),AT)
   self.assertEqual(out['refused'],['second']);self.assertIn('Useful advice.',sent[0][1]);self.assertNotIn('INVALID',sent[0][1])

class StalePending(unittest.TestCase):
 def test_saved_but_unsent_assessment_is_named_after_three_days(self):
  b=Board();root,key=batch(b);b.tasks[root]['created_at']=int((AT-dt.timedelta(days=4)).timestamp());b.tasks[root]['status']='done'
  report=d.findings(snapshot(),AT,AT-dt.timedelta(days=1));c.Tickets(b).ensure(report,{'pairs':{key:{'assessment':'advice','sent_receipt':None}}},{},'2026-10-18',AT)
  self.assertTrue(any('ask Hal' in x for x in report['items']))

class CachedEvidenceBoundary(unittest.TestCase):
 def test_failed_current_reader_cannot_publish_an_unsent_cached_assessment(self):
  with tempfile.TemporaryDirectory() as tmp:
   b=Board();root,key=batch(b);asm=c.plan(b,root,choice(key))['assembly'];reader=next(x for x in b.tasks.values() if x['assignee']=='safer_web_reader')
   reader['status']='done';reader['result']=json.dumps(dict(status='UNASSESSED',answer={},sources=[],quotes=[],reason='not fetched'))
   d.atomic_json(Path(tmp)/'daily-check-assessments.json',{'pairs':{key:dict(assessment='CACHED UNSENT ADVICE',assessed_at=AT.isoformat(),sent_receipt=None)}})
   sent=[];out=c.publish(b,asm,{},Path(tmp),lambda *a:(sent.append(a) or {'success':True}),AT)
   self.assertIn(key,out['refused']);self.assertNotIn('CACHED UNSENT ADVICE',sent[0][1])


class DeviceNag(unittest.TestCase):
 def test_saved_unsent_note_still_gets_three_day_reminder(self):
  b=Board();adapter=c.Tickets(b)
  def report():return dict(updates=[],inventory=[],gaps=[],situations=[dict(key='s',members=[],line='Lamp offline')],device_complete=True)
  first=report();adapter.ensure(first,{}, {},'2026-10-18',AT);root=first['situation_roots']['s']
  notes={'pairs':{root:dict(note='Saved advice',observed_at=AT.isoformat())}}
  later=report();adapter.ensure(later,{},notes,'2026-10-21',AT+dt.timedelta(days=3))
  self.assertTrue(any('look at the board' in line for line in later['items']))
  notes['pairs'][root]['sent_receipt']={'accepted_at':AT.isoformat()}
  sent=report();adapter.ensure(sent,{},notes,'2026-10-22',AT+dt.timedelta(days=4))
  self.assertEqual(sent['gaps'],[])


class FullBoardBoundary(unittest.TestCase):
 def test_large_private_root_and_reader_are_read_from_board_and_shared(self):
  with tempfile.TemporaryDirectory() as tmp:
   b=Board();root,key=batch(b);data=json.loads(b.tasks[root]['body']);data['inventory']=['PRIVATE-HOUSE-CANARY-'+str(i) for i in range(1000)];b.tasks[root]['body']=json.dumps(data)
   first=c.plan(b,root,choice(key));reader=next(t for t in b.tasks.values() if t['assignee']=='safer_web_reader')
   self.assertGreater(len(b.tasks[root]['body']),8192);self.assertNotIn('PRIVATE-HOUSE',reader['body'])
   value=envelope();value['answer']['summary']='SAFE PUBLIC FACT. '*400;reader.update(status='done',result=json.dumps(value))
   self.assertGreater(len(reader['result']),4096)
   sent=[];c.publish(b,first['assembly'],{key:'Useful complete advice.'},Path(tmp),lambda *a:(sent.append(a) or {'success':True}),AT)
   other=b.create('new batch',data,'hal','hal-daily:batch:other');second=c.plan(b,other,choice(key))
   self.assertEqual(json.loads(b.tasks[second['assembly']]['body'])['readers'][key],[reader['id']])
   self.assertEqual(len([t for t in b.tasks.values() if t['assignee']=='safer_web_reader']),1)
   self.assertIn('Useful complete advice.',sent[0][1])
 def test_incomplete_device_check_never_clears_episode_and_recurrence_emails(self):
  with tempfile.TemporaryDirectory() as tmp:
   b=Board();tickets=c.Tickets(b);sent=[];send=lambda *a:(sent.append(a) or {'success':True})
   def report(rows,complete=True):return dict(updates=[],inventory=[],gaps=[],situations=rows,device_complete=complete)
   situation=dict(key='s',members=[dict(device_id='a',condition='offline')],line='Lamp offline')
   first=report([situation]);tickets.ensure(first,{}, {},'2026-10-18',AT);root=first['situation_roots']['s']
   c.publish(b,root,'Check its power.',Path(tmp),send,AT);c.publish(b,root,'No second email.',Path(tmp),send,AT)
   tickets.ensure(report([],False),{}, {},'2026-10-19',AT);self.assertNotEqual(b.tasks[root]['status'],'archived')
   tickets.ensure(report([]),{}, {},'2026-10-20',AT);self.assertEqual(b.tasks[root]['status'],'archived')
   again=report([situation]);tickets.ensure(again,{}, {},'2026-10-21',AT);new=again['situation_roots']['s'];self.assertNotEqual(root,new)
   c.publish(b,new,'A new episode.',Path(tmp),send,AT);self.assertEqual(len(sent),2)


class RunnerBoardPin(unittest.TestCase):
 def test_explicit_hal_pin_overrides_transient_dispatcher_board(self):
  import os
  from unittest.mock import patch
  with patch.dict(os.environ,{'HERMES_KANBAN_BOARD':'unrelated'}):
   self.assertEqual(c.NativeBoard(board='hal').command[-1],'hal')
   self.assertNotIn('--board',c.NativeBoard().command)

if __name__=='__main__':unittest.main()


class AssessmentComposition(unittest.TestCase):
 def test_assessment_verdict_first_details_below_and_link_last(self):
  title='Spook v5.11.0 → v5.11.1'
  text=title+': update at the next convenient restart.\nCheck the Repairs list.\nThis release fixes a crash.\nhttps://example.org/release'
  rendered=c.assessment_bullet(title,text,'https://example.org/release')
  self.assertEqual(rendered,'- **'+title+': update at the next convenient restart.**\n  Check the Repairs list.\n  This release fixes a crash.\n  https://example.org/release')
  self.assertEqual(rendered.count(title),1);self.assertEqual(rendered.count('https://example.org/release'),1)
 def test_partial_batch_pending_package_once_with_reason_and_no_duplicate_section(self):
  with tempfile.TemporaryDirectory() as tmp:
   b=Board();root,key=batch(b);data=json.loads(b.tasks[root]['body'])
   data['updates'].append(dict(data['updates'][0],key='second',package='second',name='Spook'))
   b.tasks[root]['body']=json.dumps(data)
   asm=c.plan(b,root,dict(choice(key),second={'missing':['release not located']}))['assembly']
   reader=next(t for t in b.tasks.values() if t['assignee']=='safer_web_reader');reader.update(status='done',result=json.dumps(envelope()))
   sent=[];note='Spook 2026.9.3 → 2026.10.2: not assessed. The release post could not be read.'
   c.publish(b,asm,{key:'Update at the next convenient restart.\nCheck the Repairs list.'},Path(tmp),lambda *a:(sent.append(a) or {'success':True}),AT,note=note)
   subject,body=sent[0];self.assertIn('1',subject);self.assertIn('## Needs you',body);self.assertIn('## Updates',body)
   self.assertEqual(body.count('Spook'),1);self.assertIn('The release post could not be read.',body);self.assertNotIn('Could not assess',body)
   self.assertNotIn('## Could not finish',body);self.assertLess(body.index('Check the Repairs'),body.index('Spook'))
 def test_device_note_has_one_bold_bullet_per_item_and_a_counted_subject(self):
  with tempfile.TemporaryDirectory() as tmp:
   b=Board();tid=b.create('look',dict(schema='hal-daily.device.v1',situation=dict(key='s',members=[],line='Lamp offline')),'hal','hal-daily:device:s')
   sent=[];c.publish(b,tid,{},Path(tmp),lambda *a:(sent.append(a) or {'success':True}),AT,note='Zigbee: Check the hub power.\nLamp: Check its battery.')
   self.assertEqual(sent[0],('House: 2 things need you','## Needs you\n- **Zigbee**: Check the hub power.\n- **Lamp**: Check its battery.'))


class ReviewB1(unittest.TestCase):
 def test_host_title_differs_from_hal_and_two_pending_notes_match_versions(self):
  host='Spook 👻 Your homie v5.11.0 → v5.11.1'
  result=c.assessment_bullet(host,'Spook v5.11.0 → v5.11.1: update at the next convenient restart.\nCheck Repairs.','https://example.org/spook')
  self.assertEqual(result.splitlines()[0],'- **'+host+': update at the next convenient restart.**')
  self.assertEqual(result.count('v5.11.0'),1)
  with tempfile.TemporaryDirectory() as tmp:
   b=Board();root,key=batch(b);data=json.loads(b.tasks[root]['body'])
   data['updates']=[dict(data['updates'][0],key='spook',name='Spook 👻 Your homie',installed='v5.11.0',available='v5.11.1'),dict(data['updates'][0],key='core',name='Home Assistant Core',installed='2026.10.1',available='2026.10.2')]
   b.tasks[root]['body']=json.dumps(data)
   asm=c.plan(b,root,{'spook':{'missing':['release unavailable']},'core':{'missing':['release unavailable']}})['assembly']
   sent=[];note='Spook v5.11.0 → v5.11.1: not assessed. The Spook post was unavailable.\n2026.10.2: not assessed. The Core post was unavailable.'
   c.publish(b,asm,{},Path(tmp),lambda *a:(sent.append(a) or {'success':True}),AT,note=note)
   body=sent[0][1]
   self.assertEqual(body.count('v5.11.0'),1);self.assertEqual(body.count('2026.10.1'),1)
   self.assertEqual(body.count('v5.11.1'),1);self.assertEqual(body.count('2026.10.2'),1)
   self.assertIn('The Spook post was unavailable.',body);self.assertIn('The Core post was unavailable.',body)
   self.assertNotIn('## Could not finish',body)
 def test_inline_bold_is_preserved_and_only_whole_line_wrapper_is_removed(self):
  text='**Spook v5.11.0 → v5.11.1: update now.**\n**Before** you restart, note the **Repairs** list.'
  result=c.assessment_bullet('Spook 👻 Your homie v5.11.0 → v5.11.1',text,'https://example.org/spook')
  self.assertIn('\n  **Before** you restart, note the **Repairs** list.',result)
  self.assertNotIn('v5.11.1: Spook',result)
