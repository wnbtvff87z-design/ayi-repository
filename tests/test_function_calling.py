import importlib.util,json,os,sys,types,unittest
from pathlib import Path
from unittest.mock import patch
class FunctionCalling(unittest.TestCase):
 def test_opt_in_is_interpretation_only(self):
  captured=[]
  data={'intent':'availability','updates':{k:None for k in ('customer_name','reservation_date','reservation_time','party_size','customer_phone','customer_email')},'requested_times':[],'meal':None,'time_expression':None,'reply':'','needs_clarification':False,'selection':None}
  class Completions:
   def create(self,**kw):
    captured.append(kw)
    call=types.SimpleNamespace(function=types.SimpleNamespace(name='interpret_turn',arguments=json.dumps(data)))
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(tool_calls=[call]))])
  class Client:
   def __init__(self,**kw):self.chat=types.SimpleNamespace(completions=Completions())
  fake=types.ModuleType('openai');fake.OpenAI=Client
  with patch.dict(sys.modules,{'openai':fake}),patch.dict(os.environ,{'OPENAI_API_KEY':'offline-placeholder','OPENAI_FUNCTION_CALLING':'true'}):
   spec=importlib.util.spec_from_file_location('isolated_interpret',Path(__file__).resolve().parents[1]/'web'/'interpret.py')
   mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
   result=mod.interpret({'timezone':'Europe/Madrid'}, {}, [], '¿Hay para el domingo?')
  self.assertEqual(result['intent'],'availability')
  self.assertEqual(result['updates'],{})
  self.assertEqual(captured[0]['tools'][0]['function']['name'],'interpret_turn')
  self.assertEqual(len(captured[0]['tools']),1)
  self.assertFalse(captured[0]['parallel_tool_calls'])
if __name__=='__main__':unittest.main()
