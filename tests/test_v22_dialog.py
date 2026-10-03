import sys, types, unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'web'))
try: import psycopg
except ImportError:
    psycopg=types.ModuleType('psycopg');psycopg.rows=types.ModuleType('psycopg.rows');psycopg.rows.dict_row=object()
    sys.modules['psycopg']=psycopg;sys.modules['psycopg.rows']=psycopg.rows
try: import openai
except ImportError:
    openai=types.ModuleType('openai');openai.OpenAI=object;sys.modules['openai']=openai
try: import booking_safe
except ImportError:
    booking_safe=types.ModuleType('booking_safe')
    for name in ('cancel_for_caller','modify_for_caller','unique_reservation'):
        setattr(booking_safe,name,lambda *args,**kwargs: None)
    sys.modules['booking_safe']=booking_safe
import temporal, restaurant_dialog as d
B={'business_id':'REST-001','sector':'restaurante','allow_reservations':True,'timezone':'Europe/Madrid'}

DAY='2030-10-05'
class V22(unittest.TestCase):
    def test_quantity_never_time(self):
        for text in ('3 personas','somos tres personas','seria para 3 personas','el sabado 3','mesa para tres'):
            with self.subTest(text=text):
                self.assertIsNone(temporal.explicit_time(text))
        self.assertEqual(temporal.explicit_time('3 personas y a las 9 de la noche'),'21:00')
        self.assertEqual(temporal.explicit_time('seria para 3 personas a las 21 horas'),'21:00')
        self.assertEqual(temporal.explicit_time('a las nueve de la noche'),'21:00')
        self.assertEqual(temporal.explicit_time('20:30'),'20:30')
        self.assertIsNone(temporal.explicit_time('6 8 7 3 8 4 3 3'))
    def test_single_turn_party_and_time(self):
        state={'intent':'create','phase':'collecting','operation_id':'op','values':{'reservation_date':DAY},'offered':[{'date':DAY,'time':'19:00'},{'date':DAY,'time':'21:00'}],'last_requested_field':'party_size','time_band':'night'}
        ai={'intent':'create','updates':{'party_size':3,'reservation_time':'03:00'},'requested_times':['03:00'],'reply':''}
        with patch.object(d,'interpret',return_value=ai) as model,patch.object(d,'availability',return_value={'available':True}) as available,patch.object(d,'create') as create:
            reply,out=d.process(B,state,[],'Seria para 3 personas y a las 9 de la noche','Voice','c:1','+34000000000')
        self.assertEqual(out['values']['party_size'],3)
        self.assertEqual(out['values']['reservation_time'],'21:00')
        available.assert_called_with(B,DAY,'21:00',3)
        create.assert_not_called()
        self.assertNotIn('tres de la mañana',reply)
        model.assert_called_once()
    def test_group_only_does_not_pick_third_offered_slot(self):
        state={'intent':'create','phase':'collecting','operation_id':'op','values':{'reservation_date':DAY},'offered':[{'date':DAY,'time':t} for t in ('17:00','19:00','21:00')],'last_requested_field':'party_size','time_band':'night'}
        ai={'intent':'create','updates':{'party_size':3,'reservation_time':'03:00'},'requested_times':['03:00'],'reply':''}
        with patch.object(d,'interpret',return_value=ai),patch.object(d,'options',return_value=[{'date':DAY,'time':'19:00'},{'date':DAY,'time':'21:00'}]) as options,patch.object(d,'availability') as available:
            reply,out=d.process(B,state,[],'3 personas','Voice','c:2','+34000000000')
        self.assertEqual(out['values']['party_size'],3)
        self.assertNotIn('reservation_time',out['values'])
        self.assertNotIn('mañana',reply)
        available.assert_not_called()
        options.assert_called()
    def test_ambiguous_nine_matches_offered_night(self):
        offered=[{'date':DAY,'time':'19:00'},{'date':DAY,'time':'21:00'}]
        self.assertEqual(temporal.contextual_time('a las 9',offered=offered),'21:00')
        self.assertEqual(temporal.contextual_time('a las 9 de la mañana',offered=offered),'09:00')
        self.assertEqual(d._selection('a las 9',offered),offered[1])
    def test_explicit_period_overrides_chosen(self):
        self.assertEqual(temporal.contextual_time('a las 9 de la mañana',{'date':DAY,'time':'21:00'}),'09:00')
    def test_complete_booking_requires_final_yes(self):
        state={}
        turns=[
            ('Quisiera reservar este sábado a la noche',{'intent':'availability','updates':{'reservation_date':DAY},'requested_times':[],'reply':''}),
            ('Sería para 3 personas a las 9 de la noche',{'intent':'create','updates':{'party_size':3,'reservation_time':'03:00'},'requested_times':['03:00'],'reply':''}),
            ('Mariano Cortina',{'intent':'create','updates':{'customer_name':'Mariano Cortina'},'requested_times':[],'reply':''}),
            ('mariano arroba ejemplo punto com',{'intent':'create','updates':{'customer_email':'mariano@ejemplo.com'},'requested_times':[],'reply':''}),
            ('687 384 333',{'intent':'create','updates':{'customer_phone':'687384333'},'requested_times':[],'reply':''}),
        ]
        rows=[{'date':DAY,'time':t,'remaining':8,'capacity':8} for t in ('13:00','19:00','21:00')]
        with patch.object(temporal,'relative_day',return_value=DAY),patch.object(d,'relative_day',return_value=DAY),patch.object(d,'slots',return_value=rows),patch.object(d,'availability',return_value={'available':True}),patch.object(d,'create') as create:
            for i,(text,parsed) in enumerate(turns):
                with patch.object(d,'interpret',return_value=parsed) as model:
                    reply,state=d.process(B,state,[],text,'Voice',f'c:{i}', '+34000000000')
                    model.assert_called_once()
                self.assertNotIn('tres de la mañana',reply)
                create.assert_not_called()
            self.assertEqual(state['values']['reservation_time'],'21:00')
            self.assertEqual(state['values']['party_size'],3)
            self.assertEqual(state['phase'],'awaiting')
            self.assertIn('¿La registro?',reply)
            with patch.object(d,'interpret',return_value={'intent':'create','updates':{},'requested_times':[],'reply':''}):
                reply,state=d.process(B,state,[],'sí','Voice','c:5','+34000000000')
            create.assert_called_once()
            self.assertEqual(create.call_args.args[0]['reservation_time'],'21:00')
            self.assertTrue(create.call_args.args[0]['_confirmed'])
    def test_confirmation_requires_explicit_consent(self):
        self.assertFalse(d._confirmed('sí pero cambia la hora'))
        self.assertFalse(d._confirmed('3 personas'))
if __name__=='__main__':unittest.main()
