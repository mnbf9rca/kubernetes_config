import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
import hal_daily_check as d
AT=dt.datetime(2026,10,18,8,tzinfo=dt.timezone.utc)
def state(eid,value,**attrs):
 return dict(entity_id=eid,state=value,attributes=attrs,last_reported=AT.isoformat())
def snapshot():
 return dict(states=[state('sensor.flower_battery','0',device_class='battery',friendly_name='Flower sensor'),state('update.core','on',title='Home Assistant Core',installed_version='2026.9.3',latest_version='2026.10.2',release_url='https://example.org/core'),state('sensor.offline_devices','0',devices=[],integrations={'zha':1})],entities=[dict(entity_id='sensor.flower_battery',device_id='plant'),dict(entity_id='update.core',platform='hassio',unique_id='core')],devices=[dict(id='plant',name='Flower sensor',config_entries=['z'])],entries=[dict(entry_id='z',domain='zha',state='loaded',title='Zigbee')],traces=[],logs=[],repairs=[],gaps=[])
class Findings(unittest.TestCase):
 def test_flat_battery_and_exact_update_not_hidden(self):
  r=getattr(d,'findings',lambda *a:{})(snapshot(),AT,AT-dt.timedelta(days=1))
  self.assertEqual(len(r.get('updates',[])),1);self.assertIn('replace',' '.join(r.get('items',[])).lower())
 def test_unknown_is_not_flat(self):
  s=snapshot();s['states'][0]['state']='unknown';self.assertEqual(d.findings(s,AT,AT-dt.timedelta(days=1))['items'],[])
 def test_aborted_condition_and_not_triggered_are_not_failures(self):
  self.assertFalse(d.failed_trace({'script_execution':'aborted'},{'trace':{'action/0':[{'result':{'result':False}}]}}))
  self.assertTrue(d.failed_trace({'script_execution':'aborted'},{'trace':{'action/0':[{'error':'failed'}]}}))
  self.assertFalse(d.failed_trace({'not_triggered':True,'error':'x'}))
 def test_retention_bucket_coverage(self):
  rows=[{'timestamp':{'start':(AT-dt.timedelta(minutes=i)).isoformat()}} for i in range(5)]
  self.assertFalse(d.trace_covered(rows,AT-dt.timedelta(days=1)));self.assertTrue(d.trace_covered(rows[:4],AT-dt.timedelta(days=1)))
 def test_ignored_repairs_are_not_findings(self):
  s=snapshot();s['repairs']=[dict(domain='spook',issue_id='x',ignored=True,severity='error')]
  self.assertEqual(d.findings(s,AT,AT-dt.timedelta(days=1))['items'],d.findings(snapshot(),AT,AT-dt.timedelta(days=1))['items'])
class Grouping(unittest.TestCase):
 def test_all_unhealthy_hub_members_group_without_invented_cause(self):
  devices=[dict(id='hub',name='Coordinator',config_entries=['z'])]+[dict(id=x,name=x,via_device_id='hub',config_entries=['z']) for x in ('a','b')]
  bad=[dict(device_id=x,name=x,condition='offline') for x in ('a','b')]
  groups=d.group_devices(bad,devices,{'a','b'},{'z':'Zigbee'},set())
  self.assertEqual(len(groups),1);self.assertEqual(len(groups[0]['members']),2);self.assertNotIn('taking',groups[0]['line'])
 def test_healthy_member_prevents_all_down_claim(self):
  devices=[dict(id=x,config_entries=['z']) for x in ('a','b')]
  groups=d.group_devices([dict(device_id='a',condition='offline',name='Lamp')],devices,{'a','b'},{'z':'Zigbee'},set())
  self.assertEqual(len(groups[0]['members']),1);self.assertNotIn('all',groups[0]['line'].lower())
class Presentation(unittest.TestCase):
 def test_reminders_require_receipt_and_never_repeat_advice(self):
  r=d.findings(snapshot(),AT,AT-dt.timedelta(days=1));key=r['updates'][0]['key'];cache={'pairs':{key:dict(assessment='LONG PRIVATE ADVICE',assessed_at=AT.isoformat(),sent_receipt=None)}}
  body=d.render(r,cache,{},AT)[1];self.assertIn('assessment pending',body);self.assertNotIn('LONG PRIVATE',body)
  cache['pairs'][key]['sent_receipt']={'accepted_at':AT.isoformat()};self.assertIn('assessed on 18 October',d.render(r,cache,{},AT)[1])
 def test_no_notes_is_not_stale_pending(self):
  r=d.findings(snapshot(),AT,AT-dt.timedelta(days=1));key=r['updates'][0]['key']
  body=d.render(r,{'pairs':{key:{'outcome':'no_public_notes'}}},{},AT)[1]
  self.assertIn('no release notes to assess',body);self.assertNotIn('ask Hal',body)
 def test_quiet_verdict_only_and_gap_never_quiet(self):
  r=dict(items=[],updates=[],situations=[],gaps=[]);subject,body=d.render(r,{},{},AT)
  self.assertEqual(subject,'House: nothing needs you');self.assertEqual(body,'Nothing needs your attention today.')
  r['gaps']=['devices'];self.assertNotEqual(d.render(r,{},{},AT)[0],subject)
class Delivery(unittest.TestCase):
 def test_refusal_retry_is_identical_then_receipt_suppresses(self):
  with tempfile.TemporaryDirectory() as tmp:
   app=d.Daily(Path(tmp),snapshot,None,lambda *a:{'success':False},lambda *a:None,clock=lambda:AT)
   self.assertEqual(app._production(),1);first=json.loads((Path(tmp)/'daily-check-state.json').read_text())
   app.collect=lambda:self.fail('retry recollected');sent=[];app.send=lambda *a:(sent.append(a) or {'success':True})
   self.assertEqual(app._production(),0);self.assertEqual(sent[0],(first['subject'],first['body']))
   self.assertEqual(app._production(),0);self.assertEqual(len(sent),1)
 def test_preview_has_no_side_effect(self):
  with tempfile.TemporaryDirectory() as tmp:
   app=d.Daily(Path(tmp),snapshot,None,lambda *a:self.fail('mail'),lambda *a:self.fail('push'),clock=lambda:AT)
   app.run();self.assertEqual(list(Path(tmp).iterdir()),[])
class Confidentiality(unittest.TestCase):
 def test_exact_credential_canary_scrubbed(self):
  self.assertNotIn('PASSWORD-CANARY',d.clean('Flower PASSWORD-CANARY',('PASSWORD-CANARY',)))
class ScriptBoundary(unittest.TestCase):
 def test_client_rejects_mutation(self):
  with self.assertRaises(ValueError):d.HA('https://example.invalid','token').read('call_service',domain='update')
class HangRecovery(unittest.TestCase):
 def test_failed_area_keeps_other_findings(self):
  s=snapshot();s['gaps']=['updates: sixty-second read hang guard'];r=d.findings(s,AT,AT-dt.timedelta(days=1))
  self.assertTrue(r['items']);self.assertIn('updates',d.render(r,{},{},AT)[1])
class Heartbeat(unittest.TestCase):
 def test_accepted_incomplete_is_up(self):
  with tempfile.TemporaryDirectory() as tmp:
   pushes=[];s=snapshot();s['gaps']=['devices'];app=d.Daily(Path(tmp),lambda:s,None,lambda *a:{'success':True},lambda *a:pushes.append(a),clock=lambda:AT)
   self.assertEqual(app._production(),0);self.assertEqual(pushes[-1],('up','accepted'))

class InvalidState(unittest.TestCase):
 def test_malformed_saved_advice_does_not_abort_delivery(self):
  with tempfile.TemporaryDirectory() as tmp:
   (Path(tmp)/'daily-check-assessments.json').write_text('[]');sent=[]
   app=d.Daily(Path(tmp),snapshot,None,lambda *a:(sent.append(a) or {'success':True}),lambda *a:None,clock=lambda:AT)
   self.assertEqual(app._production(),0);self.assertEqual(len(sent),1)
 def test_missing_ids_each_get_log_only_diagnostics(self):
  s=snapshot();s['states'] += [state('automation.a','on',friendly_name='Blinds'),state('automation.b','off',friendly_name='Lights')]
  report=d.findings(s,AT,AT-dt.timedelta(days=1));text=' '.join(report['diagnostics'])
  self.assertFalse(any('Give ' in line for line in report['items']))
  self.assertIn('Blinds',text);self.assertIn('Lights',text)

class CLI(unittest.TestCase):
 def test_fixture_preview_is_default_and_never_writes(self):
  import subprocess,sys
  with tempfile.TemporaryDirectory() as tmp:
   fixture=Path(tmp)/'input.json';fixture.write_text(json.dumps(snapshot()))
   result=subprocess.run([sys.executable,str(Path(d.__file__)),'--fixture',str(fixture)],capture_output=True,text=True)
   self.assertEqual(result.returncode,0);self.assertIn('assessment pending',result.stdout);self.assertEqual(len(list(Path(tmp).iterdir())),1)
 def test_fixture_cannot_be_production(self):
  import subprocess,sys
  result=subprocess.run([sys.executable,str(Path(d.__file__)),'--production','--fixture','missing'],capture_output=True,text=True)
  self.assertNotEqual(result.returncode,0)

class OutputBoundaries(unittest.TestCase):
 def test_credential_in_label_never_reaches_mail_board_or_file(self):
  with tempfile.TemporaryDirectory() as tmp:
   s=snapshot();s['states'][0]['attributes']['friendly_name']='secret"CANARY';sent=[];created=[]
   class Board:
    def ensure(self,report,*args):created.append(json.dumps(report))
   app=d.Daily(Path(tmp),lambda:s,Board(),lambda *a:(sent.append(a) or {'success':True}),lambda *a:None,clock=lambda:AT,secrets=('secret"CANARY',))
   self.assertEqual(app._production(),0)
   self.assertNotIn('secret"CANARY',sent[0][1]);self.assertNotIn('secret\\"CANARY',''.join(created))
   self.assertNotIn('CANARY',(Path(tmp)/'daily-check-state.json').read_text())

class DeviceReminder(unittest.TestCase):
 def test_note_prose_not_repeated_but_receipted_date_is_shown(self):
  situation=dict(key='s',line='The lamp is offline; check its power.',members=[])
  report=dict(items=[situation['line']],updates=[],situations=[situation],situation_roots={'s':'t_1'},gaps=[])
  notes={'pairs':{'t_1':dict(note='PRIVATE DIAGNOSIS',observed_at=AT.isoformat(),sent_receipt={'accepted_at':AT.isoformat()})}}
  body=d.render(report,{},notes,AT)[1]
  self.assertIn('looked at this on 18 October',body);self.assertNotIn('PRIVATE DIAGNOSIS',body)
  notes['pairs']['t_1']['sent_receipt']=None;self.assertNotIn('looked at this',d.render(report,{},notes,AT)[1])

class APIBoundary(unittest.TestCase):
 def test_authentication_refusal_stops_subsequent_network_reads(self):
  calls=[];client=d.HA('https://example.invalid','canary')
  async def denied(*args):calls.append('network');raise PermissionError('refused')
  client._read=denied
  for command in ('get_states','trace/list'):
   with self.assertRaises(PermissionError):client.read(command)
  self.assertEqual(calls,['network'])
 def test_hung_websocket_is_actually_abandoned(self):
  import asyncio
  from unittest.mock import patch
  client=d.HA('https://example.invalid','canary')
  async def hung(*args):await asyncio.Event().wait()
  client._read=hung
  with patch.object(d,'HA_READ_SECONDS',0.01,create=True),self.assertRaises(TimeoutError):client.read('get_states')


class NativeMailGuard(unittest.TestCase):
 def test_hung_sender_child_is_killed_without_holding_the_daily_lock(self):
  import subprocess,sys
  from unittest.mock import patch
  real_run=subprocess.run
  def hung(argv,**kwargs):
   return real_run([sys.executable,'-c','import time; time.sleep(5)'],**kwargs)
  with patch.object(d,'native_recipient',return_value='fixture@example.org',create=True),patch.object(d,'MAIL_SECONDS',0.01,create=True),patch.object(d.subprocess,'run',side_effect=hung):
   sender=object.__new__(d.NativeMail);sender.home=Path('/does-not-exist');sender.recipient='fixture@example.org'
   with self.assertRaises(subprocess.TimeoutExpired):sender('House: test','body')

class DevelopmentNotices(unittest.TestCase):
 def test_unchanged_development_build_is_acknowledged_only_after_accepted_email(self):
  with tempfile.TemporaryDirectory() as tmp:
   s=snapshot();s['states'][1]['state']='off';s['states'][1]['attributes']['installed_version']='2026.11.0.dev20261018'
   clock=[AT];sent=[];app=d.Daily(Path(tmp),lambda:s,None,lambda *a:(sent.append(a) or {'success':True}),lambda *a:None,clock=lambda:clock[0])
   app._production();self.assertIn('development build',sent[-1][1])
   clock[0]+=dt.timedelta(days=1);app._production();self.assertNotIn('development build',sent[-1][1])
   s['states'][1]['attributes']['latest_version']='2026.11.0';clock[0]+=dt.timedelta(days=1);app._production();self.assertIn('development build',sent[-1][1])

class RunnerCredentials(unittest.TestCase):
 def test_runner_references_resolve_in_private_mapping_never_agent_environment(self):
  import os,sys,types
  from unittest.mock import patch
  seen=[]
  def apply(config,home,environ):
   seen.append(config)
   self.assertEqual(environ['HOME'],'/fixture-home')
   for name in config['onepassword']['env']:
    environ[name]={'HAL_DAILY_RUNNER_HASS_TOKEN':'fixture-token','HAL_DAILY_RUNNER_HASS_URL':'https://fixture.invalid','HAL_DAILY_CHECK_PUSH_TOKEN':'fixture-push'}[name]
  modules={'utils':types.SimpleNamespace(load_yaml_file_readonly=lambda p:json.loads(p.read_text())), 'agent.secret_scope':types.SimpleNamespace(load_env_file=lambda p:{'OP_SERVICE_ACCOUNT_TOKEN':'fixture-bootstrap'}), 'agent.secret_sources.registry':types.SimpleNamespace(apply_all=apply)}
  with tempfile.TemporaryDirectory() as tmp:
   home=Path(tmp);(home/'.op.env').write_text('fixture bootstrap')
   config={'secrets':{'onepassword':{'enabled':True,'env':{'HAL_DAILY_CHECK_PUSH_TOKEN':'op://fixture/push/token','OTHER':'op://fixture/other/token'}}},'daily_check':{'runner_secrets':{'onepassword':{'enabled':True,'env':{'HAL_DAILY_RUNNER_HASS_TOKEN':'op://fixture/ha/token','HAL_DAILY_RUNNER_HASS_URL':'op://fixture/ha/url'}}}}}
   (home/'config.yaml').write_text(json.dumps(config))
   with patch.dict(sys.modules,modules),patch.dict(os.environ,{'HOME':'/fixture-home'},clear=True):
    result=d.load_credentials(home)
    self.assertEqual(result['HAL_DAILY_RUNNER_HASS_TOKEN'],'fixture-token')
    self.assertNotIn('HASS_TOKEN',os.environ)
    self.assertNotIn('HAL_DAILY_RUNNER_HASS_TOKEN',os.environ)
    self.assertNotIn('fixture-token',os.environ.values())
    self.assertNotIn('OTHER',seen[0]['onepassword']['env'])
    self.assertIn('fixture-bootstrap',result.values())


class ScopeEdges(unittest.TestCase):
 def test_cycle_and_multiple_entries_never_form_an_all_down_group(self):
  bad=[dict(device_id=x,name=x,condition='offline') for x in ('a','b')]
  for devices in ([dict(id='a',via_device_id='b',config_entries=['z']),dict(id='b',via_device_id='a',config_entries=['z'])], [dict(id=x,config_entries=['z','other']) for x in ('a','b')]):
   groups=d.group_devices(bad,devices,{'a','b'},{'z':'Zigbee'},set())
   self.assertEqual(len(groups),2)
   self.assertTrue(all(g.get('grouping_unavailable') for g in groups))
 def test_missing_monitor_denominator_is_named_without_losing_findings(self):
  s=snapshot();s['states'][-1]['state']='1';s['states'][-1]['attributes'].update(devices=[dict(id='plant',name='Flower sensor')],integrations={'zha':99})
  report=d.findings(s,AT,AT-dt.timedelta(days=1))
  self.assertTrue(report['situations']);self.assertTrue(any('grouping' in g for g in report['diagnostics']))
 def test_prerelease_target_does_not_enter_normal_update_batch(self):
  for version in ('2026.11.0b1','2026.11.0rc2','v2.0.0-beta.1','2026.11.0.dev1'):
   s=snapshot();s['states'][1]['attributes']['latest_version']=version
   self.assertEqual(d.findings(s,AT,AT-dt.timedelta(days=1))['updates'],[])
 def test_switched_off_automation_retains_failed_run_and_coverage(self):
  s=snapshot();s['states'].append(state('automation.lamp','off',id='lamp',friendly_name='Lamp'))
  s['traces']=[dict(item_id='lamp',run_id=str(i),error='private-error',timestamp={'start':(AT-dt.timedelta(minutes=i+1)).isoformat()}) for i in range(5)]
  report=d.findings(s,AT,AT-dt.timedelta(days=1))
  self.assertTrue(any('Lamp automation failed' in line for line in report['items']))
  self.assertTrue(any('Lamp: retained run history' in line for line in report['diagnostics']))


class ProductionLock(unittest.TestCase):
 def test_busy_run_resolves_no_credentials_and_pushes_nothing(self):
  import os
  from unittest.mock import patch
  with patch.dict(os.environ,{'HERMES_HOME':str(d.PROFILE)}),patch.object(d,'lock',side_effect=BlockingIOError),patch.object(d,'load_credentials',side_effect=ValueError('must not resolve')) as credentials,patch.object(d,'kuma_push') as push:
   self.assertEqual(d.main(['--production']),0)
   credentials.assert_not_called();push.assert_not_called()


class ReceiptCrash(unittest.TestCase):
 def test_acceptance_before_receipt_failure_retries_identical_frozen_body(self):
  from unittest.mock import patch
  with tempfile.TemporaryDirectory() as tmp:
   calls=[];writes=[];real=d.atomic_json
   def write(path,value,secrets=()):
    writes.append(value)
    if value.get('receipt'):raise OSError('fixture disk failure')
    real(path,value,secrets)
   app=d.Daily(Path(tmp),snapshot,None,lambda *a:(calls.append(a) or {'success':True}),lambda *a:None,clock=lambda:AT)
   with patch.object(d,'atomic_json',side_effect=write):self.assertEqual(app._production(),1)
   self.assertEqual(app._production(),0);self.assertEqual(calls[0],calls[1])
   self.assertEqual(app._production(),0);self.assertEqual(len(calls),2)


class MorningEmailFaults(unittest.TestCase):
 def test_update_entities_without_data_never_become_gaps_or_mail(self):
  data=snapshot()
  data['states'] += [state('update.window_'+value,value,friendly_name='Left Window Firmware',installed_version='16.0.1-dev') for value in ('unknown','unavailable')]
  report=d.findings(data,AT,AT-dt.timedelta(days=1));body=d.render(report,{},{},AT)[1]
  self.assertEqual(report['gaps'],[]);self.assertEqual(report['development'],[])
  self.assertNotIn('Left Window',body)
 def test_frequent_runs_have_log_only_coverage_and_only_errors_reach_mail(self):
  import contextlib,io
  data=snapshot();data['states'][0]['state']='100';data['states'][1]['state']='off'
  data['states'].append(state('automation.lights','off',id='lights',friendly_name='💡 Hall lights'))
  data['traces']=[dict(item_id='lights',timestamp={'start':(AT-dt.timedelta(minutes=i+1)).isoformat()},script_execution='finished') for i in range(5)]
  sent=[];log=io.StringIO()
  with tempfile.TemporaryDirectory() as home,contextlib.redirect_stdout(log):
   app=d.Daily(Path(home),lambda:data,None,lambda *a:(sent.append(a) or {'success':True}),lambda *a:None,clock=lambda:AT)
   self.assertEqual(app._production(),0)
  self.assertEqual(sent[0][0],'House: nothing needs you');self.assertNotIn('retained',sent[0][1]);self.assertIn('retained',log.getvalue())
  data['traces'][0]['error']='Connection failed'
  report=d.findings(data,AT,AT-dt.timedelta(days=1))
  self.assertEqual(report['items'],['The 💡 Hall lights automation failed; check its trace in Home Assistant.']);self.assertEqual(report['gaps'],[])
  for identity in (None,'removed'):
   data['traces']=[dict(item_id=identity,error='Connection failed',timestamp={'start':(AT-dt.timedelta(minutes=1)).isoformat()})]
   self.assertEqual(d.findings(data,AT,AT-dt.timedelta(days=1))['items'],['The unnamed automation failed; check its trace in Home Assistant.'])
 def test_could_not_finish_lists_only_whole_check_blockers_on_separate_lines(self):
  data=snapshot();data['gaps']=['Recent errors could not be checked.']
  data['states'][-1]['last_reported']=(AT-dt.timedelta(minutes=10)).isoformat()
  data['states'].append(state('automation.lights','on',id='lights',friendly_name='Lights'))
  data['traces']=[dict(item_id='lights',timestamp={})]
  report=d.findings(data,AT,AT-dt.timedelta(days=1));body=d.render(report,{},{},AT)[1]
  section=body.split('## Could not finish\n',1)[-1].splitlines()
  self.assertEqual(section,['- **Recent errors** could not be checked.','- **Offline-devices monitor** is stale or unreadable, so I could not check for offline devices.'])
  self.assertNotIn('run timing',body)
 def test_subject_counts_actions_but_not_pending_development_or_nonaction_blockers(self):
  data=snapshot();data['states'][-1]['last_reported']=(AT-dt.timedelta(minutes=10)).isoformat()
  data['states'].append(state('update.wled','off',title='WLED',installed_version='16.0.1-dev',latest_version='16.0.1-dev'))
  sent=[]
  with tempfile.TemporaryDirectory() as home:
   app=d.Daily(Path(home),lambda:data,None,lambda *a:(sent.append(a) or {'success':True}),lambda *a:None,clock=lambda:AT)
   self.assertEqual(app._production(),0)
  self.assertEqual(sent[0][0],'House: I could not finish the check; 1 thing needs you')
  self.assertIn('replace or recharge',sent[0][1]);self.assertIn('assessment pending',sent[0][1]);self.assertIn('development build',sent[0][1])
  report=d.findings(snapshot(),AT,AT-dt.timedelta(days=1));key=report['updates'][0]['key']
  self.assertEqual(d.render(report,{'pairs':{key:dict(assessment='Advice',assessed_at=AT.isoformat(),sent_receipt={'accepted_at':AT.isoformat()})}},{},AT)[0],'House: 2 things need you')
 def test_entity_type_suffixes_are_removed_and_emoji_stay(self):
  data=snapshot();data['states'][0]['attributes']['friendly_name']='🌿 monstera Battery'
  data['states'][1]['attributes']['title']='Spook 👻 Your homie Update'
  data['states'].append(state('update.window','on',title='Bathroom Window Firmware Update',installed_version='1',latest_version='2'))
  body=d.render(d.findings(data,AT,AT-dt.timedelta(days=1)),{},{},AT)[1]
  self.assertIn('**🌿 Monstera battery** is flat',body);self.assertIn('Spook 👻 Your homie 2026.9.3',body);self.assertIn('Bathroom Window 1 → 2',body)
  self.assertNotIn('Battery battery',body);self.assertNotIn('homie Update',body);self.assertNotIn('Firmware',body)
 def test_log_warning_names_the_actual_first_error_in_one_line(self):
  data=snapshot();data['logs']=[dict(level='ERROR',name='homeassistant.components.ohme',message=['Could not connect to the charger.\nTraceback: implementation detail','Another error'])]
  report=d.findings(data,AT,AT-dt.timedelta(days=1));body=d.render(report,{},{},AT)[1]
  self.assertIn('- **ohme**: Could not connect to the charger.',body.splitlines())
  self.assertNotIn('ask Hal',body);self.assertNotIn('Traceback',body);self.assertNotIn('Another error',body)

 def test_multiple_error_records_are_one_action_per_integration(self):
  data=snapshot();data['logs']=[dict(level='ERROR',name=name,message=[message]) for name,message in [('homeassistant.components.ohme.coordinator','First charger error.'),('homeassistant.components.ohme.client','Second charger error.'),('custom_components.ohme.client','Third charger error.')]]
  report=d.findings(data,AT,AT-dt.timedelta(days=1));subject,body=d.render(report,{},{},AT)
  self.assertEqual(subject,'House: 2 things need you')
  self.assertEqual([line for line in body.splitlines() if line.startswith('- **ohme**:')],['- **ohme**: First charger error.'])
  self.assertNotIn('Second charger error',body);self.assertNotIn('Third charger error',body)
 def test_blockers_name_timeouts_only_when_a_timeout_occurs(self):
  from unittest.mock import patch
  class Client:
   def read(self,command,**args):
    if command=='system_log/list':raise failure
    return []
  for failure in (TimeoutError('fixture timeout'),ValueError('fixture unavailable')):
   with self.subTest(failure=type(failure).__name__):
    result=d.collect_ha(Client());line=next(x for x in result['gaps'] if 'recent errors' in x)
    self.assertIn('sixty-second wait' if isinstance(failure,TimeoutError) else 'Home Assistant did not provide the data',line)
    self.assertNotIn('guard',line)
  sent=[]
  with tempfile.TemporaryDirectory() as home,patch.object(d,'findings',side_effect=ValueError('fixture malformed input')):
   app=d.Daily(Path(home),snapshot,None,lambda *a:(sent.append(a) or {'success':True}),lambda *a:None,clock=lambda:AT)
   self.assertEqual(app._production(),0)
  self.assertIn('- **Batteries**: The Home Assistant data could not be read.',sent[0][1])
  import subprocess
  class Board:
   def ensure(self,*args):raise failure
  for failure in (subprocess.TimeoutExpired(['fixture'],180),ValueError('fixture unavailable')):
   sent=[]
   with tempfile.TemporaryDirectory() as home:
    app=d.Daily(Path(home),snapshot,Board(),lambda *a:(sent.append(a) or {'success':True}),lambda *a:None,clock=lambda:AT)
    self.assertEqual(app._production(),0)
   self.assertIn("I could not read the task board",sent[0][1])
   self.assertEqual('three-minute wait' in sent[0][1],isinstance(failure,subprocess.TimeoutExpired))



class EmailComposition(unittest.TestCase):
 def test_sections_bold_leads_and_subject_count_match_action_bullets(self):
  report=d.findings(snapshot(),AT,AT-dt.timedelta(days=1));report['gaps']=['Recent errors could not be checked.']
  subject,body=d.render(report,{},{},AT)
  self.assertEqual(subject,'House: I could not finish the check; 1 thing needs you')
  self.assertIn('- **Flower sensor battery** is flat; replace or recharge it.',body)
  self.assertIn('- **Home Assistant Core 2026.9.3 → 2026.10.2**',body)
  self.assertIn('- **Recent errors** could not be checked.',body)
  self.assertLess(body.index('## Needs you'),body.index('## Updates'));self.assertLess(body.index('## Updates'),body.index('## Could not finish'))
  needs=body.split('## Needs you\n',1)[1].split('\n\n',1)[0]
  self.assertEqual(sum(line.startswith('- ') for line in needs.splitlines()),1)
 def test_delivered_update_action_moves_to_needs_you_and_prose_stays_private(self):
  report=d.findings(snapshot(),AT,AT-dt.timedelta(days=1));key=report['updates'][0]['key']
  subject,body=d.render(report,{'pairs':{key:dict(assessment='PRIVATE ADVICE',assessed_at=AT.isoformat(),sent_receipt={'accepted_at':AT.isoformat()})}},{},AT)
  self.assertEqual(subject,'House: 2 things need you');self.assertNotIn('## Updates',body);self.assertNotIn('PRIVATE ADVICE',body)
  self.assertEqual(sum(x.startswith('- ') for x in body.splitlines()),2)
 def test_real_error_is_one_sentence_and_keeps_environment_terms(self):
  data=snapshot();data['logs']=[dict(level='ERROR',name='custom_components.ohme.client',message=['Cannot connect to api.ohme.io:443. Retry later. Extra diagnostic.'])]
  body=d.render(d.findings(data,AT,AT-dt.timedelta(days=1)),{},{},AT)[1]
  self.assertIn('- **ohme**: Cannot connect to api.ohme.io:443.',body)
  self.assertNotIn('Retry later',body);self.assertNotIn('Extra diagnostic',body);self.assertNotIn('Check its log',body)

class ReviewFormatting(unittest.TestCase):
 def test_named_bold_leads_are_capitalised_and_blocker_is_not_all_bold(self):
  self.assertEqual(d.item_bullet('The monstera battery is flat; replace it.'),'- **Monstera battery** is flat; replace it.')
  self.assertEqual(d.item_bullet('The 🌿 monstera battery is flat; replace it.'),'- **🌿 Monstera battery** is flat; replace it.')
  self.assertEqual(d.item_bullet('I could not check batteries because the Home Assistant data could not be read.'),'- **Batteries**: The Home Assistant data could not be read.')

if __name__=='__main__':unittest.main()
