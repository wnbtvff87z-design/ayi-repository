"""Offline protocol regression tests for the deployed relay source."""
import ast, asyncio, json, logging, sys
from pathlib import Path
from types import SimpleNamespace
from xml.sax.saxutils import escape
ROOT=Path(__file__).resolve().parents[1]
SOURCE=ROOT/'relay'/'main.py'
tree=ast.parse(SOURCE.read_text(encoding='utf-8'))
functions=[n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name in ('websocket','relay_ended')]
for node in functions:node.decorator_list=[]
class Response:
 def __init__(self,content,status_code=200,media_type=None):self.content=content;self.status_code=status_code
class Disconnect(Exception):pass
ns={'Request':object,'WebSocket':object,'json':json,'log':logging.getLogger('test'),'Response':Response,'WebSocketDisconnect':Disconnect,
    'valid_ws':lambda ws:True,'valid_http':lambda req,form:True,'number':lambda v:v,'escape':escape}
exec(compile(ast.Module(body=functions,type_ignores=[]),str(SOURCE),'exec'),ns)
class WS:
 def __init__(self,reply):
  self.reply=reply;self.sent=[];self.events=[{'type':'setup','callSid':'CA1','from':'+341','to':'+342'},
     {'type':'prompt','voicePrompt':'chau','last':True}]
 async def accept(self):pass
 async def receive_text(self):
  if not self.events:raise Disconnect()
  return json.dumps(self.events.pop(0))
 async def send_text(self,value):self.sent.append(json.loads(value))
class Request:
 async def form(self):return {'HandoffData':json.dumps({'reason':'goodbye'})}

def test_end_message_instead_of_speaking_twice():
 async def core(path,data):
  return {'business':{'business_id':'REST-001'}} if path=='/internal/business' else {'reply':'¡Gracias a vos! Hasta luego.'}
 ns['core']=core;ns['event_external_id']=lambda event,call,text:'CA1:1'
 ws=WS('¡Gracias a vos! Hasta luego.')
 asyncio.run(ns['websocket'](ws))
 assert ws.sent==[{'type':'end','handoffData':'{"reason": "goodbye"}'}]

def test_callback_says_goodbye_then_hangs_up():
 response=asyncio.run(ns['relay_ended'](Request()))
 assert response.status_code==200
 assert response.content.count('<Say')==1
 assert response.content.count('<Hangup/>')==1

def test_callback_requires_twilio_signature():
 ns['valid_http']=lambda req,form:False
 response=asyncio.run(ns['relay_ended'](Request()))
 assert response.status_code==403
 ns['valid_http']=lambda req,form:True

def test_connect_has_action_callback():
 text=SOURCE.read_text(encoding='utf-8')
 assert 'action="{action}" method="POST"' in text
 assert "'/relay-ended'" in text
