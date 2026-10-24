import ast
import json
from pathlib import Path
import unittest

# Compile the real pure parser without importing a live gateway or SMTP adapter.
module=ast.parse(Path(__file__).with_name('__init__.py').read_text())
node=next(n for n in module.body if isinstance(n,ast.FunctionDef) and n.name=='parse_envelope')
namespace={'json':json,'SCHEMA':'hal.email-verdict.v1'};exec(compile(ast.Module(body=[node],type_ignores=[]),'<verdict parser>','exec'),namespace)
parse=namespace['parse_envelope']
class Verdict(unittest.TestCase):
 def test_full_long_body_not_truncated_or_split(self):
  text='Useful assessment. '*10000
  self.assertEqual(parse(json.dumps(dict(schema='hal.email-verdict.v1',subject='House: updates assessed',body=text))),('House: updates assessed',text))
 def test_subject_injection_and_control_or_attachment_markers_refused(self):
  for subject,body in [('House: fine\nBcc: bad','text'),('wrong','text'),('House: report','bad\0text'),('House: report','MEDIA: /tmp/file')]:
   with self.subTest(subject=subject,body=body),self.assertRaises(ValueError):parse(json.dumps(dict(schema='hal.email-verdict.v1',subject=subject,body=body)))
 def test_duplicate_envelope_keys_refused(self):
  with self.assertRaises(ValueError):parse('{"schema":"a","schema":"b"}')


class MimeComposition(unittest.TestCase):
 def adapter(self,native_class=None):
  from email.mime.multipart import MIMEMultipart
  from email.mime.text import MIMEText
  import html,re,types
  class Native:
   def _new_reply(self,to_addr,body,*args,**kwargs):
    msg=MIMEMultipart();msg['Subject']='Hermes Agent';msg['Message-ID']='<fixture@example.org>';msg['To']=to_addr
    msg.attach(MIMEText(body,'plain','utf-8'));return msg,'<fixture@example.org>','Hermes Agent'
  nodes=[n for n in module.body if isinstance(n,ast.FunctionDef) and n.name=='render_html' or isinstance(n,ast.ClassDef) and n.name=='VerdictAdapter']
  ns=dict(namespace,native=types.SimpleNamespace(EmailAdapter=native_class or Native),html=html,re=re,MIMEText=MIMEText)
  exec(compile(ast.Module(body=nodes,type_ignores=[]),'<verdict MIME>','exec'),ns)
  adapter=object.__new__(ns['VerdictAdapter']);adapter._thread_context={};adapter._address='fixture@example.org'
  return adapter
 def test_native_message_carries_plain_and_html_from_same_body(self):
  from email import policy
  from email.parser import BytesParser
  body='## Needs you\n- **Spook 👻: update now.**\n  Check Repairs.\n  https://example.org/release\n\n## Could not finish\n- **Core**: not assessed.'
  msg,mid,subject=self.adapter()._new_reply('fixture@example.org',json.dumps(dict(schema='hal.email-verdict.v1',subject='House: 1 thing needs you',body=body)))
  msg=BytesParser(policy=policy.default).parsebytes(msg.as_bytes())
  self.assertEqual(msg.get_content_type(),'multipart/alternative');self.assertEqual(subject,msg['Subject']);self.assertEqual(mid,msg['Message-ID'])
  self.assertEqual([p.get_content_type() for p in msg.iter_parts()],['text/plain','text/html'])
  plain,rich=list(msg.iter_parts());self.assertEqual(plain.get_content(),body)
  text=rich.get_content();self.assertIn('<h2>Needs you</h2>',text);self.assertIn('<strong>Spook 👻: update now.</strong>',text);self.assertIn('<li>',text);self.assertIn('Check Repairs.',text)
 def test_html_escapes_untrusted_prose_and_leaves_unsafe_links_as_text(self):
  body='## Updates\n- **<script>alert(1)</script>** <img src=x>\n  javascript:alert(2)\n  https://example.org/?a=1&b=2'
  msg,_,_=self.adapter()._new_reply('fixture@example.org',json.dumps(dict(schema='hal.email-verdict.v1',subject='House: updates pending',body=body)))
  rich=msg.get_payload()[1].get_payload(decode=True).decode('utf-8')
  self.assertNotIn('<script>',rich);self.assertNotIn('<img',rich);self.assertIn('&lt;script&gt;',rich);self.assertNotIn('href="javascript:',rich);self.assertIn('a=1&amp;b=2',rich)

 def test_multiple_items_use_a_single_list_and_quiet_body_is_one_paragraph(self):
  for body,expected in [('## Needs you\n- **A**: act.\n- **B**: act.','<ul><li><strong>A</strong>: act.</li><li><strong>B</strong>: act.</li></ul>'),('Nothing needs your attention today.','<p>Nothing needs your attention today.</p>')]:
   msg,_,_=self.adapter()._new_reply('fixture@example.org',json.dumps(dict(schema='hal.email-verdict.v1',subject='House: fixture',body=body)))
   self.assertIn(expected,msg.get_payload()[1].get_payload(decode=True).decode('utf-8'))
 @unittest.skipUnless(Path('/home/hermes/.hermes/hermes-agent').is_dir(),'installed native adapter only')
 def test_real_native_adapter_builds_alternative_without_smtp(self):
  import sys
  sys.path.insert(0,'/home/hermes/.hermes/hermes-agent')
  from plugins.platforms.email.adapter import EmailAdapter
  body='## Needs you\n- **Spook 👻: update now.**\n  Check Repairs.'
  msg,mid,subject=self.adapter(EmailAdapter)._new_reply('fixture@example.org',json.dumps(dict(schema='hal.email-verdict.v1',subject='House: 1 thing needs you',body=body)))
  self.assertEqual(msg.get_content_type(),'multipart/alternative');self.assertEqual(msg['Subject'],subject)
  self.assertEqual(msg['Message-ID'],mid);self.assertEqual(msg.get_payload()[0].get_payload(decode=True).decode('utf-8'),body)
  self.assertIn('<strong>Spook 👻: update now.</strong>',msg.get_payload()[1].get_payload(decode=True).decode('utf-8'))

class TrialSubject(unittest.TestCase):
 def test_exact_trial_prefix_is_allowed_and_other_subject_guards_stay_closed(self):
  subject='[trial] House: 1 thing needs you'
  self.assertEqual(parse(json.dumps(dict(schema='hal.email-verdict.v1',subject=subject,body='## Needs you\n- **Lamp**: check power.')))[0],subject)
  for subject in ('[trial] House: ','[trial] anything','[trial] [trial] House: test','[trial] House: test\nBcc: bad'):
   with self.subTest(subject=subject),self.assertRaises(ValueError):parse(json.dumps(dict(schema='hal.email-verdict.v1',subject=subject,body='text')))

if __name__=='__main__':unittest.main()
