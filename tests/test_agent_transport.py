"""Transport contract: web /internal/turn returns {success, reply, action}; relay hangs up on action, never on reply text."""
import ast, asyncio, json, logging, os, re, sys
from pathlib import Path
from unittest.mock import patch
from xml.sax.saxutils import escape
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'web'))
import main as core_app

# ---- relay (same offline harness as the other relay tests)
tree=ast.parse((ROOT/'relay'/'main.py').read_text(encoding='utf-8'))
functions=[n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='websocket']
for n in functions:n.decorator_list=[]
class Disconnected(Exception):pass
ns={'Request':object,'WebSocket':object,'json':json,'re':re,'escape':escape,'log':logging.getLogger('test'),'WebSocketDisconnect':Disconnected,
    'valid_ws':lambda ws:True,'number':lambda v:v,'env':lambda key:'bN1bDXgDIGX5lw0rtY2B','event_external_id':lambda *a:'CA1:1'}
exec(compile(ast.Module(body=functions,type_ignores=[]),'relay/main.py','exec'),ns)
class WS:
 def __init__(self):self.sent=[];self.events=[{'type':'setup','callSid':'CA1','from':'+341','to':'+342'},{'type':'prompt','voicePrompt':'chau','last':True}]
 async def accept(self):pass
 async def receive_text(self):
  if not self.events:raise Disconnected()
  return json.dumps(self.events.pop(0))
 async def send_text(self,text):self.sent.append(json.loads(text))
def run_relay(turn):
 async def core(path,data):return {'business':{'business_id':'t'}} if path=='/internal/business' else turn
 ns['core']=core;ws=WS();asyncio.run(ns['websocket'](ws));return ws.sent

def test_relay_ends_on_action_with_arbitrary_text():
 sent=run_relay({'success':True,'reply':'Fue un gusto, que andes bien','action':'end_call','reason':'goodbye'})
 assert [m['type'] for m in sent]==['end'] and json.loads(sent[0]['handoffData'])['reason']=='goodbye'

def test_relay_does_not_end_on_text_equality():
 for reply in ('¡Gracias a vos! Hasta luego.','De acuerdo, no hice cambios. ¡Hasta luego!'):
  for turn in ({'success':True,'reply':reply},{'success':True,'reply':reply,'action':'continue'}):
   sent=run_relay(turn)
   assert sent==[{'type':'text','token':reply,'last':True,'interruptible':True}]

def test_relay_unknown_reason_falls_back_to_goodbye():
 sent=run_relay({'reply':'chau','action':'end_call','reason':'<script>'})
 assert json.loads(sent[0]['handoffData'])['reason']=='goodbye'

def test_relay_ends_even_without_reply_text():
 assert run_relay({'reply':'','action':'end_call'})[0]['type']=='end'

# ---- web /internal/turn and converse_turn
class Cur:
 def __init__(self,row=None,rows=None):self.row=row;self.rows=rows or []
 def fetchone(self):return self.row
 def fetchall(self):return self.rows
class Conn:
 def __init__(self,state=None):self.state=state or {};self.updates=[];self.turns=[]
 def __enter__(self):return self
 def __exit__(self,*a):return False
 def execute(self,sql,args=()):
  if sql.startswith('SELECT state'):return Cur({'state':self.state,'updated_at':__import__('datetime').datetime.now(__import__('datetime').timezone.utc)})
  if sql.startswith('UPDATE customer_sessions'):self.updates.append(json.loads(args[0]))
  if sql.startswith('INSERT INTO conversation_turns'):self.turns.append(args)
  return Cur()
B={'business_id':'REST-001','sector':'restaurante'}

def test_converse_propagates_action_and_stores_it():
 conn=Conn()
 agent_out={'reply':'Chau!','action':'end_call','reason':'goodbye','state':{}}
 with patch.object(core_app,'db',lambda:conn),patch.object(core_app,'init_schema',lambda:None),patch.object(core_app,'process_turn',lambda *a:agent_out):
  out=core_app.converse_turn(B,'Voice','+34600',  'chau','CA1:event:1')
  assert out=={'reply':'Chau!','action':'end_call','reason':'goodbye'}
  assert core_app.converse(B,'WhatsApp','+34600','chau','m1')=='Chau!'
 assert conn.updates[0]['_last_turn']['action']=='end_call'

def test_duplicate_turn_keeps_action():
 conn=Conn({'_last_turn':{'external_id':'CA1:event:1','action':'end_call','reason':'goodbye'}})
 with patch.object(core_app,'db',lambda:conn),patch.object(core_app,'init_schema',lambda:None),patch.object(core_app,'duplicate_turn_reply',lambda *a:'Chau!'):
  out=core_app.converse_turn(B,'Voice','+34600','chau','CA1:event:1')
 assert out['action']=='end_call' and out['reply']=='Chau!'

def test_internal_turn_returns_action(monkeypatch):
 monkeypatch.setenv('INTERNAL_API_KEY','k')
 c=core_app.app.test_client()
 body={'business_id':'REST-001','business_phone':'+34000','customer_phone':'+34600','external_id':'e','text':'chau'}
 with patch.object(core_app,'lookup',lambda *a:B),patch.object(core_app,'converse_turn',lambda *a:{'reply':'Chau','action':'end_call','reason':'goodbye'}):
  r=c.post('/internal/turn',json=body,headers={'X-Internal-API-Key':'k'})
  assert r.status_code==200 and r.get_json()=={'success':True,'reply':'Chau','action':'end_call','reason':'goodbye'}
  assert c.post('/internal/turn',json=body).status_code==401
