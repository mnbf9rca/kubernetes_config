"""Offline tests against the installed native adapter source; no SDK dependencies or network.

Set HERMES_EMAIL_ADAPTER_SOURCE to plugins/platforms/email/adapter.py from Hermes.
Only the framework imports are stubbed; native MIME, SMTP and TLS methods run unchanged.
"""
import ast
import asyncio
import contextlib
import email
import importlib.util
import json
import logging
import os
from pathlib import Path
import ssl
import sys
import types
import unittest
from unittest.mock import patch
from dataclasses import dataclass
from enum import Enum

HERE=Path(__file__).resolve().parent
HAL=Path('/home/hermes/.hermes/profiles/hal')
RECIPIENT='fixture@example.invalid'

class Platform:
    EMAIL='email'
    def __new__(cls,name): return name

@dataclass
class Result:
    success: bool
    message_id: str|None=None
    error: str|None=None

class Base:
    def __init__(self,config,platform):
        self.config=config;self.platform=platform;self._running=False

class SMTP:
    def __init__(self,trace,*args,**kwargs):
        self.trace=trace;trace.append(('connect',args,kwargs));self.messages=[]
    def starttls(self,**kwargs):self.trace.append(('starttls',kwargs))
    def ehlo(self):self.trace.append(('ehlo',))
    def login(self,address,password):self.trace.append(('login',address,password))
    def send_message(self,message):
        self.messages.append(message);self.trace.append(('send',message));return {}
    def quit(self):self.trace.append(('quit',))
    def close(self):self.trace.append(('close',))


def native_module():
    source=os.environ.get('HERMES_EMAIL_ADAPTER_SOURCE')
    if not source: raise unittest.SkipTest('Set HERMES_EMAIL_ADAPTER_SOURCE to the native adapter source')
    tree=ast.parse(Path(source).read_text())
    tree.body=[node for node in tree.body if not (isinstance(node,ast.ImportFrom) and
        node.module.split('.')[0] in ('agent','gateway','utils'))]
    m=types.ModuleType('plugins.platforms.email.adapter')
    m.__file__=source
    m.__dict__.update(BasePlatformAdapter=Base,SendResult=Result,Platform=Platform,PlatformConfig=object,
        _get_secret=lambda name,default='':default,
        coerce_port=lambda value,default:int(value),
        is_truthy_value=lambda value,default=False:default if value in ('',None) else str(value).lower() in ('true','1','yes'),
        t=lambda value:value,cache_document_from_bytes=None,cache_image_from_bytes=None,
        MessageEvent=object,MessageType=object,cancel_task=None,safe_schedule_threadsafe=None,
        decode_json_list_literal=lambda value:[],send_error=lambda message:{'error':message})
    exec(compile(tree,source,'exec'),m.__dict__)
    return m


def native_functions(path,names,namespace):
    tree=ast.parse(Path(path).read_text())
    tree.body=[node for node in tree.body if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)) and node.name in names]
    exec(compile(tree,str(path),'exec'),namespace)
    return namespace


class VerdictTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.native=native_module()

    def setUp(self):
        self.trace=[];self.secret_reads=[]
        self.entries={'email':types.SimpleNamespace()}
        self.registry=types.SimpleNamespace(get=lambda name:self.entries.get(name),is_registered=lambda name:name in self.entries)
        self.email_config=types.SimpleNamespace(enabled=True,extra={
            'address':'hal@example.invalid','smtp_host':'smtp.example.invalid',
            'smtp_security':'starttls','smtp_tls_verify':True})
        self.alias_config=types.SimpleNamespace(enabled=True,extra={'address':'WRONG_ALIAS_CONFIG'})
        self.config=types.SimpleNamespace(platforms={})
        native=self.native
        def secret(name,default=''):
            self.secret_reads.append(name)
            return 'SYNTHETIC_PASSWORD' if name=='EMAIL_PASSWORD' else default
        self.enterContext(patch.object(native,'_get_secret',secret))
        self.enterContext(patch.object(native.smtplib,'SMTP',lambda *a,**kw:SMTP(self.trace,*a,**kw)))
        self.enterContext(patch.object(native.smtplib,'SMTP_SSL',lambda *a,**kw:SMTP(self.trace,*a,**kw)))
        self.enterContext(patch.object(native.socket,'create_connection',side_effect=AssertionError('network forbidden')))
        modules={}
        for name in ('plugins','plugins.platforms','plugins.platforms.email','gateway','hermes_constants'):
            modules[name]=types.ModuleType(name);modules[name].__path__=[]
        modules['plugins.platforms.email.adapter']=native
        modules['plugins.platforms.email'].adapter=native
        targets=types.ModuleType('tools.send_message_targets')
        source=Path(os.environ['HERMES_EMAIL_ADAPTER_SOURCE']).resolve().parents[3]
        exec(compile((source/'tools/send_message_targets.py').read_text(),str(source/'tools/send_message_targets.py'),'exec'),targets.__dict__)
        modules['tools.send_message_targets']=targets
        modules['gateway.channel_directory']=types.SimpleNamespace(resolve_channel_name=lambda *a:None)
        modules['gateway.config']=types.ModuleType('gateway.config')
        modules['gateway.platform_registry']=types.ModuleType('gateway.platform_registry')
        modules['gateway.platform_registry'].platform_registry=self.registry
        source=Path(os.environ['HERMES_EMAIL_ADAPTER_SOURCE']).resolve().parents[3]
        namespace=dict(Enum=Enum,contextlib=contextlib,Path=Path,__file__=str(source/'gateway/config.py'),
            _Platform__bundled_plugin_names={'email'},_Platform__bundled_plugin_aliases={})
        self.Platform=native_functions(source/'gateway/config.py',{'Platform'},namespace)['Platform']
        self.config.platforms[self.Platform.EMAIL]=self.email_config
        modules['gateway.config'].Platform=self.Platform
        modules['gateway.config'].load_gateway_config=lambda:self.config
        modules['hermes_constants'].get_hermes_home=lambda:HAL
        self.enterContext(patch.dict(sys.modules,modules))
        spec=importlib.util.spec_from_file_location('hal_email_verdict_tested',HERE/'__init__.py',submodule_search_locations=[str(HERE)])
        self.plugin=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.plugin)
        self.plugin.register(types.SimpleNamespace(register_platform=lambda **kw:self.entries.update({kw['name']:types.SimpleNamespace(**dict({'parse_target_ref_fn':None,'validate_target_ref_fn':None,'send_message_handler':None,'source':'plugin'},**kw))})))
        self.config.platforms[self.Platform(self.plugin.NAME)]=self.alias_config
        ns=dict(_live_adapter=lambda platform:(None,None),asyncio=asyncio,
            unknown_platform_error=lambda name:'Unknown platform: '+name,
            _not_configured_error=lambda *args:'Platform is not configured',
            _bounded_send_error=lambda error:str(error))
        self.dispatch=native_functions(source/'tools/send_message_tool.py',
            {'_platform_enum','_resolve_platform_config','_send_via_adapter'},ns)
        modules={name:types.ModuleType(name) for name in
            ('agent.message_sanitization','tools.interrupt','gateway.platforms.base','model_tools','hermes_cli.plugins')}
        modules['agent.message_sanitization']._sanitize_surrogates=lambda text:text
        modules['tools.interrupt'].is_interrupted=lambda:False
        modules['gateway.platforms.base'].BasePlatformAdapter=types.SimpleNamespace(
            extract_media=lambda text:([],text),filter_media_delivery_paths=lambda paths,**kw:paths,
            truncate_message=lambda text,limit:[text])
        modules['model_tools']._run_async=asyncio.run
        modules['hermes_cli.plugins'].discover_plugins=lambda:None
        self.enterContext(patch.dict(sys.modules,modules))
        # Stub framework authorization/mirroring only, never target/dispatch/receipt logic.
        ns.update(json=json,logger=logging.getLogger('fixture'),resolve_send_target=targets.resolve_send_target,
            tool_error=lambda error:json.dumps({'error':error}),_error=lambda error:{'error':error},
            _maybe_skip_cron_duplicate_send=lambda *a:None,_authorize_relay_target=lambda *a,**kw:None,
            _mirror_sent_message=lambda *a:False,_sanitize_error_text=str,
            _PLUGIN_STANDALONE_MEDIA={},_CHUNKED_ROUTES={},_TEXT_SENDERS={})
        self.tool=native_functions(source/'tools/send_message_tool.py',
            {'send_message_tool','prepare_send_message_platforms','_resolve_tool_target','_handle_send',
             '_send_to_platform','_send_chunks','_platform_max_length','_via_adapter_route'},ns)['send_message_tool']


    def envelope(self,subject='House: all clear',body='Nothing needs you today.\nChecked everything at 08:15.\n'):
        return json.dumps({'schema':'hal.email-verdict.v1','subject':subject,'body':body},ensure_ascii=False)

    def send(self,message=None,**kwargs):
        return asyncio.run(self.plugin.standalone_send(self.alias_config,RECIPIENT,
            message if message is not None else self.envelope(),**kwargs))

    def test_three_worked_subjects_and_native_mime_body(self):
        for subject,body in [('House: all clear','Nothing needs you today.\nChecked everything at 08:15.\n'),
            ('House: 1 device offline, 1 update worth doing','Two things need you.\n'),
            ("House: I couldn't check today","I couldn't reach Home Assistant this morning.\n")]:
            with self.subTest(subject=subject):
                result=self.send(self.envelope(subject,body));self.assertTrue(result['success'])
                message=[row[1] for row in self.trace if row[0]=='send'][-1]
                parsed=email.message_from_bytes(message.as_bytes())
                self.assertEqual(str(email.header.make_header(email.header.decode_header(parsed['Subject']))),subject)
                self.assertEqual(parsed.get_payload(0).get_payload(decode=True).decode(),body)
                self.assertEqual(parsed['To'],RECIPIENT)
                self.assertNotIn('Re:',str(parsed['Subject']))

    def test_native_configuration_credentials_tls_and_cleanup(self):
        self.send()
        adapter=self.plugin.VerdictAdapter(self.alias_config)
        self.assertIs(adapter.config,self.email_config)
        self.assertIn('EMAIL_PASSWORD',self.secret_reads)
        kinds=[r[0] for r in self.trace]
        self.assertEqual(kinds.count('send'),1);self.assertIn('starttls',kinds)
        self.assertEqual([r for r in self.trace if r[0]=='login'],[('login','hal@example.invalid','SYNTHETIC_PASSWORD')])
        self.assertEqual(kinds[-1],'quit')
        tls=next(r[1] for r in self.trace if r[0]=='starttls')['context']
        self.assertEqual(tls.verify_mode,ssl.CERT_REQUIRED);self.assertTrue(tls.check_hostname)

    def test_no_envelope_bad_headers_duplicates_or_extra_fields_never_submit(self):
        values=['ordinary body',self.envelope('House: fine\r\nBcc: attacker@example.invalid'),
            self.envelope('House: fine\nInjected'),self.envelope(''),self.envelope('Other subject'),
            self.envelope(body=''),self.envelope(body='MEDIA: file'),
            '{"schema":"hal.email-verdict.v1","subject":"House: a","subject":"House: b","body":"x"}',
            json.dumps({'schema':'hal.email-verdict.v1','subject':'House: all clear','body':'x','smtp_host':'attacker.invalid'})]
        for value in values:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):self.send(value)
        self.assertEqual(self.trace,[]);self.assertEqual(self.secret_reads,[])

    def test_gateway_alias_is_outbound_only_without_smtp_probe(self):
        adapter=self.plugin.VerdictAdapter(self.alias_config)
        with patch.object(adapter,'_probe_imap',side_effect=AssertionError('IMAP started')),\
             patch.object(adapter,'_probe_smtp',side_effect=AssertionError('SMTP probe')),\
             patch.object(asyncio,'create_task',side_effect=AssertionError('poller started')):
            self.assertTrue(asyncio.run(adapter.connect()))
            asyncio.run(adapter.disconnect())
        self.assertEqual(self.trace,[]);self.assertIsNone(adapter._poll_task)

    def test_subject_is_checked_immediately_before_native_smtp(self):
        adapter=self.plugin.VerdictAdapter(self.alias_config)
        msg,_,_=adapter._new_reply(RECIPIENT,self.envelope())
        msg.replace_header('Subject','Hermes Agent')
        with self.assertRaises(ValueError):adapter._smtp_send(msg)
        msg,_,_=self.native.EmailAdapter._new_reply(adapter,RECIPIENT,'body')
        with self.assertRaises(ValueError):adapter._smtp_send(msg)
        self.assertEqual(self.trace,[])

    def test_missing_native_method_fails_before_submission(self):
        for method in ('_new_reply','_smtp_send','_send_email','_connect_smtp','send'):
            with self.subTest(method=method),patch.object(self.native.EmailAdapter,method,None):
                with self.assertRaises(RuntimeError):self.send()
        with patch.dict(self.plugin._PLATFORM_PARSERS,{},clear=True):
            with self.assertRaisesRegex(RuntimeError,'native_target_parser_incompatible'):
                self.plugin.register(types.SimpleNamespace(register_platform=lambda **kw:None))
        self.assertEqual(self.trace,[])

    def test_other_profile_cannot_register_or_send(self):
        with patch.object(self.plugin,'get_hermes_home',return_value=Path('/home/hermes/.hermes')):
            with self.assertRaises(RuntimeError):self.send()
            with self.assertRaises(RuntimeError):self.plugin.register(types.SimpleNamespace(register_platform=lambda **kw:None))
        self.assertEqual(self.trace,[])

    def test_registers_distinct_profile_platform_and_leaves_email_entry_alone(self):
        entries={'email':object()};old=entries['email']
        self.plugin.register(types.SimpleNamespace(register_platform=lambda **kw:entries.setdefault(kw['name'],kw)))
        self.assertIs(entries['email'],old)
        entry=entries['hal_email_verdict'];self.assertFalse(entry.get('trusted_inbound',False))
        self.assertIs(entry['standalone_sender_fn'],self.plugin.standalone_send)
        self.assertFalse(entry['allow_update_command'])

    def test_native_cli_resolution_and_standalone_dispatch(self):
        platform,config,entry,error=self.dispatch['_resolve_platform_config'](self.plugin.NAME,self.config)
        self.assertIsNone(error);self.assertIs(config,self.alias_config)
        result=asyncio.run(self.dispatch['_send_via_adapter'](platform,config,RECIPIENT,self.envelope()))
        self.assertTrue(result['success']);self.assertEqual(sum(row[0]=='send' for row in self.trace),1)

    def test_real_send_message_tool_resolves_literal_target_and_returns_bound_receipt(self):
        result=json.loads(self.tool({'action':'send','target':self.plugin.NAME+':'+RECIPIENT,
                                     'message':self.envelope()}))
        self.assertTrue(result.get('success'),result)
        self.assertEqual(result['platform'],self.plugin.NAME)
        self.assertEqual(result['chat_id'],RECIPIENT)
        self.assertEqual(sum(row[0]=='send' for row in self.trace),1)

    def test_absent_plugin_is_unknown_even_with_alias_config_and_no_email_fallback(self):
        self.entries.pop(self.plugin.NAME)
        platform,config,entry,error=self.dispatch['_resolve_platform_config'](self.plugin.NAME,self.config)
        self.assertIsNone(platform);self.assertIsNone(config);self.assertIsNone(entry)
        self.assertIn('Unknown platform',error)
        self.assertEqual(self.trace,[])
        self.assertIsNone(self.dispatch['_resolve_platform_config']('email',self.config)[3])

    def test_native_failure_remains_failure_and_has_no_retry(self):
        with patch.object(self.native.EmailAdapter,'_connect_smtp',side_effect=OSError('synthetic refused')) as connect:
            result=self.send()
        self.assertIn('error',result);self.assertFalse(result.get('success'))
        self.assertEqual(connect.call_count,1)

    def test_media_and_threading_are_not_a_second_send_path(self):
        for kw in ({'thread_id':'x'},{'media_files':['file']},{'force_document':True}):
            with self.subTest(kw=kw),self.assertRaises(ValueError):self.send(**kw)
        self.assertEqual(self.trace,[])

if __name__=='__main__':unittest.main()
