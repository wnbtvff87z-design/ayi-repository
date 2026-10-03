"""Offline checks for the exact relay supplied by the user."""
import ast,asyncio,json,logging,re
from pathlib import Path
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape
SOURCE=Path(__file__).resolve().parents[1]/'relay'/'main.py'
tree=ast.parse(SOURCE.read_text(encoding='utf-8'))
functions=[node for node in tree.body if isinstance(node,ast.AsyncFunctionDef) and node.name in ('relay_ended','websocket')]
for node in functions:node.decorator_list=[]
class Response:
 def __init__(self,content,status_code=200,media_type=None):self.content=content;self.status_code=status_code
class Disconnected(Exception):pass
ns={'Request':object,'WebSocket':object,'Response':Response,'WebSocketDisconnect':Disconnected,'json':json,'re':re,
    'escape':escape,'log':logging.getLogger('test'),'valid_http':lambda req,form:True,
    'valid_ws':lambda ws:True,'number':lambda v:v,'env':lambda key:'bN1bDXgDIGX5lw0rtY2B'}
exec(compile(ast.Module(body=functions,type_ignores=[]),str(SOURCE),'exec'),ns)
class Request:
 def __init__(self,data):self.data=data
 async def form(self):return {'HandoffData':json.dumps(self.data)}
class WS:
 def __init__(self):
  self.sent=[];self.events=[{'type':'setup','callSid':'CA1','from':'+341','to':'+342'},
                            {'type':'prompt','voicePrompt':'chau','last':True}]
 async def accept(self):pass
 async def receive_text(self):
  if not self.events:raise Disconnected()
  return json.dumps(self.events.pop(0))
 async def send_text(self,text):self.sent.append(json.loads(text))

def test_end_carries_same_business_voice_and_callback_hangs_up():
 async def core(path,data):
  return {'business':{'business_id':'test','voice':'bN1bDXgDIGX5lw0rtY2B'}} if path=='/internal/business' else {'reply':'¡Gracias a vos! Hasta luego.','action':'end_call','reason':'goodbye'}
 ns['core']=core;ns['event_external_id']=lambda *a:'CA1:1'
 ws=WS();asyncio.run(ns['websocket'](ws))
 assert len(ws.sent)==1 and ws.sent[0]['type']=='end'
 data=json.loads(ws.sent[0]['handoffData'])
 assert data=={'reason':'goodbye','voice_id':'bN1bDXgDIGX5lw0rtY2B'}
 response=asyncio.run(ns['relay_ended'](Request(data)))
 xml=ET.fromstring(response.content)
 assert xml.find('Say').attrib['voice']=='ElevenLabs.bN1bDXgDIGX5lw0rtY2B'
 assert xml.find('Hangup') is not None
 assert 'Hasta luego' in xml.find('Say').text

def test_bad_voice_never_switches_to_default_tts():
 response=asyncio.run(ns['relay_ended'](Request({'reason':'goodbye','voice_id':'invalid!'})))
 xml=ET.fromstring(response.content)
 assert xml.find('Say') is None and xml.find('Hangup') is not None

def test_callback_signature_rejected():
 ns['valid_http']=lambda req,form:False
 response=asyncio.run(ns['relay_ended'](Request({'reason':'goodbye'})))
 assert response.status_code==403
 ns['valid_http']=lambda req,form:True
