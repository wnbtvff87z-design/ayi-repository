import ast,asyncio,json,logging,unittest
from pathlib import Path
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape
SOURCE=Path(__file__).resolve().parents[1]/'relay'/'main.py'
tree=ast.parse(SOURCE.read_text());functions=[n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name in ('relay_ended','websocket')]
for n in functions:n.decorator_list=[]
class Response:
 def __init__(self,content,status_code=200,media_type=None):self.content=content;self.status_code=status_code
class Disconnected(Exception):pass
ns={'Request':object,'WebSocket':object,'Response':Response,'WebSocketDisconnect':Disconnected,'json':json,'re':__import__('re'),'escape':escape,'log':logging.getLogger('test'),'valid_http':lambda req,form:True,'valid_ws':lambda ws:True,'number':lambda v:v,'env':lambda key:'bN1bDXgDIGX5lw0rtY2B'}
exec(compile(ast.Module(body=functions,type_ignores=[]),str(SOURCE),'exec'),ns)
class Req:
 def __init__(self,payload):self.payload=payload
 async def form(self):return {'HandoffData':json.dumps(self.payload)}
class WS:
 def __init__(self):self.sent=[];self.events=[{'type':'setup','callSid':'CA1','from':'+341','to':'+342'},{'type':'prompt','voicePrompt':'chau','last':True}]
 async def accept(self):pass
 async def receive_text(self):
  if not self.events:raise Disconnected()
  return json.dumps(self.events.pop(0))
 async def send_text(self,text):self.sent.append(json.loads(text))
class Relay(unittest.TestCase):
 def test_end_reasons_hang_up_without_duplicate_tts(self):
  for reply,reason in [('¡Gracias a vos! Hasta luego.','goodbye'),('De acuerdo, no hice cambios. ¡Hasta luego!','cancelled'),('La operación sigue pendiente de verificación. No la repitas; consultá con recepción. Hasta luego.','verification')]:
   async def core(path,data):return {'business':{'business_id':'test','voice':'bN1bDXgDIGX5lw0rtY2B'}} if path=='/internal/business' else {'reply':reply}
   ns['core']=core;ns['event_external_id']=lambda event,sid,text,seq:'CA1:1'
   ws=WS();asyncio.run(ns['websocket'](ws));self.assertEqual(len(ws.sent),1);self.assertEqual(ws.sent[0]['type'],'end')
   payload=json.loads(ws.sent[0]['handoffData']);self.assertEqual(payload['reason'],reason)
   result=asyncio.run(ns['relay_ended'](Req(payload)));xml=ET.fromstring(result.content)
   self.assertIsNotNone(xml.find('Hangup'));self.assertIsNotNone(xml.find('Say'))
def test_structured_goodbye_ends_call_even_with_different_reply(self):
 async def core(path,data):
  if path=='/internal/business':return {'business':{'business_id':'test','voice':'bN1bDXgDIGX5lw0rtY2B'}}
  return {'reply':'¡Muchas gracias, que descanses!','end_call':True,'end_reason':'goodbye'}
 ns['core']=core;ns['event_external_id']=lambda event,sid,text,seq:'CA1:turn:1'
 ws=WS();asyncio.run(ns['websocket'](ws))
 self.assertEqual(len(ws.sent),1)
 self.assertEqual(ws.sent[0]['type'],'end')
 self.assertEqual(json.loads(ws.sent[0]['handoffData'])['reason'],'goodbye')
if __name__=='__main__':unittest.main()
