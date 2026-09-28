import ast
import pathlib
import unittest

ROOT=pathlib.Path(__file__).resolve().parents[1]

def routes(path):
    tree=ast.parse(path.read_text(encoding='utf-8'))
    result=set()
    for node in tree.body:
        if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                if isinstance(dec,ast.Call) and isinstance(dec.func,ast.Attribute) and dec.args and isinstance(dec.args[0],ast.Constant):
                    result.add(dec.args[0].value)
    return result

class TestIntegrationContract(unittest.TestCase):
    def test_web_old_routes_preserved(self):
        self.assertTrue({'/','/health','/booking-health','/test-airtable','/webhook-voice','/webhook-whatsapp','/voice-dial-result','/internal/restaurant','/internal/conversations','/internal/availability','/internal/booking','/internal/book-test'}.issubset(routes(ROOT/'web/main.py')))
    def test_relay_routes(self):
        self.assertTrue({'/health','/voice','/relay-ended','/ws'}.issubset(routes(ROOT/'relay/main.py')))
    def test_new_conversation_contract(self):
        self.assertTrue({'/internal/business','/internal/turn','/internal/reconcile-pending'}.issubset(routes(ROOT/'web/main.py')))
    def test_safe_defaults_and_legacy_mode(self):
        web=(ROOT/'web/main.py').read_text()
        booking=(ROOT/'web/booking.py').read_text()
        self.assertIn("TENANT_LOOKUP_MODE','legacy'",web)
        self.assertIn("BOOKING_TEST_MODE','false'",booking)
        self.assertIn('airtable_pending=true',booking)
    def test_voice_style_present(self):
        self.assertTrue((ROOT/'relay/voice_style.txt').exists())
        self.assertTrue((ROOT/'web/voice_style.txt').exists())

if __name__=='__main__':unittest.main()
