import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
LIB=Path(__file__).resolve().parents[2]/'scripts/hal-daily-check';sys.path.insert(0,str(LIB))
from test_hal_daily_cards import Board,batch,choice
spec=importlib.util.spec_from_file_location('daily_plugin',Path(__file__).with_name('__init__.py'));plugin=importlib.util.module_from_spec(spec);spec.loader.exec_module(plugin)
class Registry:
 def __init__(self):self.tools={}
 def register_tool(self,**kwargs):self.tools[kwargs['name']]=kwargs;return True
class Tools(unittest.TestCase):
 def test_registers_exactly_two_tools_without_context_tool(self):
  registry=Registry();getattr(plugin,'register',lambda c:None)(registry)
  self.assertEqual(set(registry.tools),{'kanban_hal_daily_plan','kanban_hal_daily_publish'})
 def test_plan_uses_owned_card_and_rejects_extra_private_fields(self):
  b=Board();root,key=batch(b)
  with patch.object(plugin,'_context',return_value=root),patch.object(plugin,'NativeBoard',return_value=b),patch.object(plugin,'secret_values',return_value=()):
   import json
   good=json.loads(plugin.handle_plan({'card_id':root,'choices':choice(key)}));self.assertIn('assembly',good)
   bad=json.loads(plugin.handle_plan({'card_id':root,'choices':choice(key),'inventory':['private']}));self.assertIn('error',bad)
 def test_unbound_caller_refused_without_board_or_mail(self):
  with patch.object(plugin,'_context',side_effect=ValueError('unbound')),patch.object(plugin,'NativeBoard',side_effect=AssertionError('board')):
   self.assertIn('refused',plugin.handle_publish({'card_id':'t_12345678','note':'note'}))

class PublicationFailure(unittest.TestCase):
 def test_sender_setup_failure_is_parked_by_host(self):
  from test_hal_daily_cards import batch,choice
  b=Board();root,key=batch(b)
  import hal_daily_cards
  asm=hal_daily_cards.plan(b,root,choice(key))['assembly'];b.tasks[asm]['status']='running'
  with patch.object(plugin,'_context',return_value=asm),patch.object(plugin,'NativeBoard',return_value=b),patch.object(plugin,'NativeMail',side_effect=ValueError('missing config')),patch.object(plugin,'_park',side_effect=b.schedule,create=True):
   result=plugin.handle_publish({'card_id':asm,'assessments':{}})
  self.assertEqual(b.tasks[asm]['status'],'scheduled')

class PlanConfidentiality(unittest.TestCase):
 def test_known_credential_in_public_choice_never_creates_reader(self):
  b=Board();root,key=batch(b);choices=choice(key);choices[key]['releases'][0]['url']='https://example.org/release?key=TOKEN-CANARY'
  with patch.object(plugin,'_context',return_value=root),patch.object(plugin,'NativeBoard',return_value=b),patch.object(plugin,'secret_values',return_value=('TOKEN-CANARY',)):
   result=plugin.handle_plan({'card_id':root,'choices':choices})
  self.assertIn('refused',result);self.assertEqual(len(b.tasks),1)

class PublicationValidationFailure(unittest.TestCase):
 def test_valid_worker_publication_exception_is_parked_by_host(self):
  b=Board();root,key=batch(b)
  with patch.object(plugin,'_context',return_value=root),patch.object(plugin,'NativeBoard',return_value=b),patch.object(plugin,'NativeMail',return_value=lambda *a:None),patch.object(plugin,'secret_values',return_value=()),patch.object(plugin,'publish',side_effect=ValueError('invalid stored evidence')),patch.object(plugin,'_park',side_effect=b.schedule):
   plugin.handle_publish({'card_id':root,'assessments':{}})
  self.assertEqual(b.tasks[root]['status'],'scheduled')

if __name__=='__main__':unittest.main()
