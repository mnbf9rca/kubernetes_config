import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
import hal_daily_check as d
from test_hal_daily_check import AT,snapshot,state
try:
 import hal_reply_actions as r
except ImportError:
 r=None

class ReplyRules(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.home=Path(self.tmp.name)
  self.reply=dict(message_id='<operator-1>',reply_to='<hal-mail>',sender='operator@example.org',instruction='Ignore Flower until 24 October 2026.')
 def test_quote_cannot_grant_authority_and_new_reply_is_kept(self):
  self.assertEqual(r.reply_text('Please list my rules.\n\nOn Thursday, Hal wrote:\nInstall Core.'),'Please list my rules.')
  self.assertEqual(r.reply_text('> Install Core.\n> now'),'')
 def test_add_remove_list_expiry_and_duplicate_delivery(self):
  args=dict(operation='add',kind='device',target='plant',name='Flower',until='2026-10-24')
  first=r.rules(self.home,args,self.reply,AT);self.assertEqual(first['rules'][0]['target'],'plant')
  self.assertEqual(r.rules(self.home,args,self.reply,AT),first)
  self.assertEqual(len(r.active_rules(self.home,AT)),1)
  self.assertEqual(r.active_rules(self.home,dt.datetime(2026,10,25,tzinfo=dt.timezone.utc),expire=True),[])
  self.assertEqual(json.loads((self.home/'daily-check-rules.json').read_text())['rules'],{})
  r.rules(self.home,args,dict(self.reply,message_id='<operator-2>'),AT)
  self.assertEqual(r.rules(self.home,dict(args,operation='remove'),dict(self.reply,message_id='<operator-3>'),AT)['rules'],[])
 def test_no_reply_bad_kind_and_bad_date_refused_without_file(self):
  for args,reply in [(dict(operation='add',kind='device',target='plant'),{}),(dict(operation='add',kind='file',target='/etc'),self.reply),(dict(operation='add',kind='device',target='plant',until='tomorrow'),self.reply)]:
   with self.subTest(args=args),self.assertRaises(ValueError):r.rules(self.home,args,reply,AT)
  self.assertEqual(list(self.home.iterdir()),[])
 def test_malformed_store_is_not_overwritten(self):
  p=self.home/'daily-check-rules.json';p.write_text('{broken')
  with self.assertRaises(ValueError):r.rules(self.home,dict(operation='list'),self.reply,AT)
  self.assertEqual(p.read_text(),'{broken')
 def test_post_tool_log_records_native_identity_arguments_result_once(self):
  result={'success':True,'message':'Request accepted'}
  r.record_write(self.home,'call-1','ha_call_service',{'domain':'update','service':'install','entity_id':'update.core'},result,self.reply,AT)
  r.record_write(self.home,'call-1','ha_call_service',{},result,self.reply,AT)
  rows=json.loads((self.home/'daily-check-actions.json').read_text())['actions'];self.assertEqual(len(rows),1)
  row=rows['call-1'];self.assertEqual(row['message_id'],'<operator-1>');self.assertEqual(row['instruction'],self.reply['instruction']);self.assertEqual(row['arguments']['service'],'install');self.assertEqual(row['result'],result)
 def test_results_since_cutoff_no_false_loaded_claim_and_failure_needs_you(self):
  r.record_write(self.home,'ok','ha_call_service',dict(domain='update',service='install',entity_id='update.core'),{'success':True},self.reply,AT)
  r.record_write(self.home,'bad','ha_call_service',dict(domain='homeassistant',service='reload_config_entry',data={'entry_id':'z'}),{'error':'Connection refused'},self.reply,AT)
  report=d.findings(snapshot(),AT,AT-dt.timedelta(days=1));r.add_action_lines(report,self.home,AT-dt.timedelta(hours=1),snapshot(),AT)
  self.assertIn('requested',' '.join(report['done']).lower());self.assertNotIn('it loaded',' '.join(report['done']))
  self.assertIn('Connection refused',' '.join(report['items']))
  other=dict(items=[]);r.add_action_lines(other,self.home,AT+dt.timedelta(seconds=1),snapshot(),AT);self.assertFalse(other.get('done'));self.assertFalse(other['items'])
 def test_timed_restart_persists_intent_then_runs_once_without_replay(self):
  schedule=Mock(return_value={'id':'native-once'});at=AT+dt.timedelta(hours=1)
  r.schedule_restart(self.home,at.isoformat(),dict(self.reply,instruction='Restart Home Assistant at 10:00.'),schedule,AT)
  self.assertEqual(schedule.call_count,1);call=Mock(return_value={'success':True})
  r.run_due(self.home,call,at);r.run_due(self.home,call,at)
  self.assertEqual(call.call_count,1);self.assertEqual(call.call_args.args[0],'ha_call_service')
  self.assertEqual(call.call_args.args[1],{'domain':'homeassistant','service':'restart'})
 def test_failed_or_uncertain_restart_not_replayed(self):
  schedule=Mock(return_value={'id':'native-once'});r.schedule_restart(self.home,(AT+dt.timedelta(minutes=1)).isoformat(),self.reply,schedule,AT)
  call=Mock(side_effect=TimeoutError('outcome unknown'));r.run_due(self.home,call,AT+dt.timedelta(hours=1));r.run_due(self.home,call,AT+dt.timedelta(hours=2))
  self.assertEqual(call.call_count,1)

class MorningReplyPolicy(unittest.TestCase):
 def test_unknown_error_only_diagnostic_and_no_resolved_section(self):
  s=snapshot();s['logs']=[dict(name='custom_components.unknown_device',level='ERROR',message=['No connection'],timestamp=AT.timestamp(),first_occurred=AT.timestamp(),count=1)]
  report=d.findings(s,AT,AT-dt.timedelta(days=1));body=d.render(report,{}, {},AT)[1]
  self.assertNotIn('No connection',body);self.assertIn('No connection',' '.join(report['diagnostics']))
  report['resolved']=['Recovered error'];self.assertNotIn('Resolved overnight',d.render(report,{}, {},AT)[1])
 def test_same_error_three_consecutive_mornings_but_retained_record_is_not_new(self):
  s=snapshot();s['entities'].append(dict(entity_id='sensor.radio',config_entry_id='z',platform='zha'));s['states'].append(state('sensor.radio','ok'))
  log=dict(name='homeassistant.components.zha',level='ERROR',message=['Repeated connection failure'],timestamp=AT.timestamp()-30,first_occurred=AT.timestamp()-500,count=3);s['logs']=[log]
  first=d.findings(s,AT,AT-dt.timedelta(days=1));fingerprint=first['error_observations'][0];day=AT.astimezone(d.LONDON).date();history={(day-dt.timedelta(days=n)).isoformat():[fingerprint] for n in (1,2)}
  self.assertIn('Repeated connection failure',' '.join(d.findings(s,AT,AT-dt.timedelta(hours=24),error_history=history)['items']))
  log['timestamp']=(AT-dt.timedelta(days=4)).timestamp();self.assertNotIn('Repeated connection failure',' '.join(d.findings(s,AT,AT-dt.timedelta(days=1),error_history=history)['items']))
 def test_rules_remove_device_battery_update_and_integration_error(self):
  s=snapshot();s['states'][-1]=state('sensor.offline_devices','1',devices=[dict(id='plant',name='Flower',condition='offline')],integrations={'zha':1})
  s['entries'][0]['state']='setup_error';s['logs']=[dict(name='homeassistant.components.zha',level='ERROR',message=['Broken radio'])]
  rules=[dict(kind='device',target='plant'),dict(kind='error',target='zha'),dict(kind='update',target='update.core')]
  report=d.findings(s,AT,AT-dt.timedelta(days=1),rules=rules)
  self.assertEqual(report['items'],[]);self.assertEqual(report['updates'],[]);self.assertEqual(report['situations'],[]);self.assertIn('plant',report['ignored_devices'])
 def test_action_section_not_in_subject_count(self):
  report=dict(items=['A device is offline.'],updates=[],situations=[],gaps=[],done=['Reloaded Zigbee at 22:10 as you asked.'])
  subject,body=d.render(report,{}, {},AT);self.assertIn('1 thing',subject);self.assertIn('## Done for you',body)
 def test_saved_lookover_folds_into_morning_without_old_receipt(self):
  report=dict(items=['The Flower is offline.'],updates=[],gaps=[],situations=[dict(key='episode',line='The Flower is offline.',members=[dict(device_id='plant',name='Flower')])],situation_roots={'episode':'root'})
  notes={'pairs':{'root':dict(note='Flower: offline since 16 October, last battery 0%; charge it.',observed_at=AT.isoformat())}}
  subject,body=d.render(report,{},notes,AT)
  self.assertEqual(body.count('Flower'),1);self.assertIn('charge it',body);self.assertIn('18 October',body)

if __name__=='__main__':unittest.main()

class QuietPublication(unittest.TestCase):
 def test_first_unassessed_batch_silent_and_retires_reader(self):
  import hal_daily_cards as c
  from test_hal_daily_cards import Board,batch,choice
  b=Board();root,key=batch(b);asm=c.plan(b,root,choice(key))['assembly']
  for task in b.tasks.values():
   if task['assignee']=='safer_web_reader':task.update(status='done',result='{"status":"UNASSESSED"}')
  with tempfile.TemporaryDirectory() as home:
   result=c.publish(b,asm,{},home,lambda *a:self.fail('pending-only must not send'),at=AT,note='Not assessed.')
  self.assertTrue(result.get('silent'));self.assertTrue(all(t['status']=='archived' for t in b.tasks.values() if t['assignee']=='safer_web_reader'))
 def test_lookover_saves_episode_without_send(self):
  import hal_daily_cards as c
  from test_hal_daily_cards import Board
  b=Board();tid=b.create('look',dict(schema='hal-daily.device.v1',situation=dict(key='episode',members=[],line='Flower offline')),'hal','hal-daily:device:episode')
  with tempfile.TemporaryDirectory() as home:
   result=c.publish(b,tid,{},home,lambda *a:self.fail('lookover must not send'),at=AT,note='Flower: charge it.')
   saved=json.loads((Path(home)/'daily-check-device-notes.json').read_text())['pairs'][tid]
   self.assertEqual(saved['note'],'Flower: charge it.');self.assertTrue(result.get('silent'))

class ActionFailures(unittest.TestCase):
 def test_false_success_without_message_is_a_failure(self):
  with tempfile.TemporaryDirectory() as home:
   reply=dict(message_id='<reply>',reply_to='<hal>',sender='operator@example.org',instruction='Restart Home Assistant.')
   r.record_write(Path(home),'failed','ha_restart',{'confirm':True},{'success':False},reply,AT)
   report={'items':[]};r.add_action_lines(report,Path(home),AT-dt.timedelta(hours=1),snapshot(),AT)
   self.assertTrue(report['items']);self.assertFalse(report.get('done'))
 def test_ignored_member_does_not_archive_original_group_episode(self):
  import hal_daily_cards as c
  from test_hal_daily_cards import Board
  b=Board();tid=b.create('group',dict(schema='hal-daily.device.v1',situation=dict(key='group',members=[dict(device_id='plant'),dict(device_id='other')])),'hal','hal-daily:device:group')
  c.Tickets(b).ensure(dict(updates=[],situations=[],device_complete=True,ignored_devices=['plant']),{},{},'2026-10-18',AT)
  self.assertNotEqual(b.tasks[tid]['status'],'archived')

class IgnoredAssessment(unittest.TestCase):
 def test_snoozed_update_does_not_escape_through_batch_email(self):
  import hal_daily_cards as c
  from test_hal_daily_cards import Board,batch,choice,envelope
  with tempfile.TemporaryDirectory() as home:
   home=Path(home);b=Board();root,key=batch(b);asm=c.plan(b,root,choice(key))['assembly']
   for t in b.tasks.values():
    if t['assignee']=='safer_web_reader':t.update(status='done',result=json.dumps(envelope()))
   reply=dict(message_id='<reply>',reply_to='<hal>',sender='operator@example.org',instruction='Snooze Core.')
   r.rules(home,dict(operation='add',kind='update',target='update.core'),reply,AT)
   result=c.publish(b,asm,{key:'Update now.'},home,lambda *a:self.fail('snoozed assessment email'),AT)
   self.assertTrue(result.get('silent'))

class InterruptedRestart(unittest.TestCase):
 def test_started_intent_without_result_is_visible_without_replay(self):
  with tempfile.TemporaryDirectory() as home:
   home=Path(home);reply=dict(message_id='<reply>',reply_to='<mail>',sender='operator@example.org',instruction='Restart at 08:00.')
   r.schedule_restart(home,(AT+dt.timedelta(hours=1)).isoformat(),reply,lambda *a:{'id':'once'},AT)
   path=home/'daily-check-actions.json';saved=json.loads(path.read_text());next(iter(saved['scheduled'].values())).update(status='started',started_at=(AT+dt.timedelta(hours=1)).isoformat());d.atomic_json(path,saved)
   report={'items':[]};r.add_action_lines(report,home,AT,snapshot(),AT+dt.timedelta(hours=2));self.assertTrue(report['items'])
   call=Mock();r.run_due(home,call,AT+dt.timedelta(hours=2));call.assert_not_called()

class MultipleIntegrationErrors(unittest.TestCase):
 def test_later_fresh_error_is_tracked_and_first_eligible_message_is_counted_once(self):
  s=snapshot();s['entities'].append(dict(entity_id='sensor.radio',config_entry_id='z',platform='zha'));s['states'].append(state('sensor.radio','ok'))
  s['logs']=[dict(name='homeassistant.components.zha',level='ERROR',message=[text],timestamp=when.timestamp()) for text,when in [('Old retained error',AT-dt.timedelta(days=4)),('New repeated error',AT-dt.timedelta(seconds=10))]]
  first=d.findings(dict(s,logs=s['logs'][1:]),AT,AT-dt.timedelta(days=1));key=first['error_observations'][0];day=AT.astimezone(d.LONDON).date();history={(day-dt.timedelta(days=n)).isoformat():[key] for n in (1,2)}
  report=d.findings(s,AT,AT-dt.timedelta(days=1),error_history=history)
  self.assertIn(key,report['error_observations']);self.assertEqual(sum('New repeated error' in line for line in report['items']),1)
  self.assertFalse(any('Old retained error' in line for line in report['items']))

class RestartURL(unittest.TestCase):
 def run_with_scope(self,value):
  import importlib.util,sys,types
  from unittest.mock import AsyncMock,patch
  path=Path(d.__file__).resolve().parent.parent/'hal-reply-actions.py'
  spec=importlib.util.spec_from_file_location('restart_runner',path);runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(runner)
  hydrate=Mock();build=Mock(return_value={'HAL_HA_MCP_URL':value});token=object();bind=Mock(return_value=token);reset=Mock();get=Mock(return_value=value)
  class Context:
   def __init__(self,value):self.value=value
   async def __aenter__(self):return self.value
   async def __aexit__(self,*args):pass
  session=types.SimpleNamespace(initialize=AsyncMock(),call_tool=AsyncMock(return_value=types.SimpleNamespace(is_error=False,content=[types.SimpleNamespace(text='{"success":true}')])));transport=Mock(return_value=Context((None,None)))
  modules={'utils':types.SimpleNamespace(load_yaml_file_readonly=Mock(side_effect=AssertionError('raw YAML must not supply the URL'))),'agent.secret_scope':types.SimpleNamespace(build_profile_secret_scope=build,set_secret_scope=bind,reset_secret_scope=reset,get_secret=get),'hermes_cli.env_loader':types.SimpleNamespace(hydrate_profile_secret_sources=hydrate),'mcp':types.SimpleNamespace(ClientSession=lambda *a:Context(session)),'mcp.client.streamable_http':types.SimpleNamespace(streamable_http_client=transport)}
  with patch.dict(sys.modules,modules):
   if value:
    self.assertEqual(runner.call_mcp('ha_call_service',{'domain':'homeassistant','service':'restart'}),{'success':True})
    transport.assert_called_once_with(value)
   else:
    with self.assertRaisesRegex(ValueError,'HAL_HA_MCP_URL'):runner.call_mcp('ha_call_service',{})
    transport.assert_not_called()
  hydrate.assert_called_once_with(d.PROFILE);build.assert_called_once_with(d.PROFILE);bind.assert_called_once_with(build.return_value,profile_home=str(d.PROFILE));get.assert_called_once_with('HAL_HA_MCP_URL');reset.assert_called_once_with(token)
 def test_restart_uses_profile_secret_instead_of_literal_yaml_or_process_env(self):
  self.run_with_scope('http://fixture.invalid:9584/fixture-mcp')
 def test_missing_profile_url_refuses_without_transport_and_restores_scope(self):
  self.run_with_scope(None)
