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
if __name__=='__main__':unittest.main()
