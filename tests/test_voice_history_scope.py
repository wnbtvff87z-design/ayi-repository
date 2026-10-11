import ast
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
SOURCE=ROOT/'web'/'main.py'
tree=ast.parse(SOURCE.read_text())
helper=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='recent_history')
namespace={}
exec(compile(ast.Module(body=[helper],type_ignores=[]),str(SOURCE),'exec'),namespace)

class Cursor:
    def execute(self,query,params):
        self.query,self.params=query,params
        return self

    def fetchall(self):
        return []

def test_voice_history_is_scoped_to_call_sid_only():
    cursor=Cursor()
    namespace['recent_history'](cursor,'REST-001','Voice','+34600000000','CA123:turn:2')
    assert 'external_id LIKE %s' in cursor.query
    assert cursor.params==('REST-001','Voice','+34600000000','CA123:%')
    namespace['recent_history'](cursor,'REST-001','WhatsApp','+34600000000','SM123')
    assert 'external_id LIKE' not in cursor.query
    assert cursor.params==('REST-001','WhatsApp','+34600000000')
