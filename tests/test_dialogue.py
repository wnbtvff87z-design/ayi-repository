import sys,types,unittest
from pathlib import Path
from unittest.mock import MagicMock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'web'))
booking=types.ModuleType('booking');booking.BookingError=type('BookingError',(Exception,),{});booking.availability=MagicMock(return_value={'available':True});booking.options=MagicMock(return_value=[{'date':'2030-10-05','time':'13:00'},{'date':'2030-10-05','time':'17:00'}]);booking.create=MagicMock();sys.modules['booking']=booking
safe=types.ModuleType('booking_safe');safe.reservations_for_caller=MagicMock(return_value=[]);safe.unique_reservation=MagicMock();safe.cancel_for_caller=MagicMock();safe.modify_for_caller=MagicMock();sys.modules['booking_safe']=safe
temporal=types.ModuleType('temporal');temporal.explicit_date=lambda t,z:'2030-10-05' if '2030-10-05' in t else None;temporal.relative_day=lambda t,z:'2030-10-05' if 'sabado' in t.lower() else None;temporal.explicit_time=lambda t:'13:00' if '13' in t else None;temporal.contextual_time=lambda t,*a,**kw:temporal.explicit_time(t);temporal.weekend_days=lambda z:('2030-10-05','2030-10-06');temporal.requested_band=lambda t:None;temporal.in_band=lambda t,b:True;sys.modules['temporal']=temporal
interp=types.ModuleType('interpret');interp.interpret=lambda b,s,h,t:{'intent':'create' if 'reserv' in t else 'question','updates':{'party_size':4} if '4' in t else {},'reply':'Sí, te escucho.'};sys.modules['interpret']=interp
import restaurant_dialog as d
B={'sector':'restaurante','allow_reservations':True,'timezone':'Europe/Madrid'}
def turn(s,t):return d.process(B,s,[],t,'Voice','id','+34600000000')
class Dialogue(unittest.TestCase):
 def test_no_repeat_and_choose_time(self):
  reply,s=turn({},'Quiero reservar el sabado');self.assertIn('personas',reply)
  reply,s=turn(s,'vamos a hacer 4');self.assertEqual(s['values']['party_size'],4);self.assertIn('13:00',reply)
  reply,s=turn(s,'¿me escuchás?');self.assertEqual(s['values']['party_size'],4);self.assertNotIn('13:00',reply)
  reply,s=turn(s,'el sábado a las 13');self.assertEqual(s['values']['reservation_time'],'13:00');self.assertNotIn('17:00',reply)
if __name__=='__main__':unittest.main()
