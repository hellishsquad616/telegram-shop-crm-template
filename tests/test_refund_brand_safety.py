import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from customer_refunds import RefundError, RefundService, RefundStore, SETTINGS, make_receipt, names_match
from database import Database
from refund_setup import gate_fingerprint, private_json, require_gate, verify_yoo, verify_split

ORG = '00000000-0000-4000-8000-000000000001'
AGENT = '00000000-0000-4000-8000-000000000002'


class BrandSafetyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.service = RefundService(self.base/'refunds.sqlite3',self.base,'fake')
        self.settings = patch.dict(SETTINGS,{'ms_organization_id':ORG,'ms_organization_exclusive':True},clear=True)
        self.settings.start();self.addCleanup(self.settings.stop)

    def test_other_brand_and_unscoped_shared_account_are_blocked(self):
        self.service.check_brand({'organization':{'id':ORG}})
        with self.assertRaises(RefundError):self.service.check_brand({'organization':{'id':AGENT}})
        SETTINGS['ms_organization_exclusive']=False
        with self.assertRaises(RefundError):self.service.check_brand({'organization':{'id':ORG}})
        SETTINGS['ms_brand_scope']={'field':'project','id':AGENT}
        self.service.check_brand({'organization':{'id':ORG},'project':{'meta':{'href':'https://api.moysklad.ru/api/remap/1.2/entity/project/'+AGENT}}})
        with self.assertRaises(RefundError):self.service.check_brand({'organization':{'id':ORG},'project':{'id':ORG}})

    def test_brand_attribute_requires_one_exact_value(self):
        SETTINGS['ms_organization_exclusive']=False
        SETTINGS['ms_brand_scope']={'field':'attribute','id':AGENT,'value':'SHOPCRM'}
        doc={'organization':{'id':ORG},'attributes':[{'id':AGENT,'value':'SHOPCRM'}]}
        self.service.check_brand(doc)
        doc['attributes'][0]['value']='OTHER'
        with self.assertRaises(RefundError):self.service.check_brand(doc)

    async def test_customer_differences_are_warnings_but_order_number_stays_required(self):
        db=Database(self.base/'crm.sqlite3');db.initialize();self.service.crm_path=db.path
        with db.connect() as c:
            c.execute("INSERT INTO orders(order_number,customer_name,email,phone,created_at,updated_at) VALUES(?,?,?,?,?,?)",('123456789','Тестовый Покупатель','buyer@example.test','+79990000000',1,1))
        request={'order_number':'123456789','name':'Тестовый Покупатель','email':'buyer@example.test','phone':'89990000000'}
        record={'request':request}
        doc={'name':'order-123456789','agent':{'id':AGENT}}
        self.service.ms=AsyncMock(return_value={'name':'Тестовый Покупатель','email':'buyer@example.test','phone':'+79990000000'})
        expected=await self.service.customer(record,doc)
        self.assertTrue(expected)
        details=await self.service.customer_details(record,doc)
        self.assertEqual(details['warnings'],[])
        for field,value in (('name','Другой Покупатель'),('email','other@example.test'),('phone','+78880000000')):
            with self.subTest(field=field):
                bad=copy.deepcopy(record);bad['request'][field]=value
                self.assertTrue(await self.service.customer(bad,doc))
                details=await self.service.customer_details(bad,doc)
                self.assertEqual(len(details['warnings']),1)
                self.assertIn('не блокирует возврат',details['warnings'][0])
                self.assertEqual(details['customer']['name'],'Тестовый Покупатель')
        bad=copy.deepcopy(record);bad['request']['order_number']='999999999'
        with self.assertRaises(RefundError):await self.service.customer(bad,doc)
        self.service.ms.return_value={'name':'Покупатель Другого Бренда'}
        self.assertEqual(await self.service.customer(record,doc),expected)
        self.assertTrue((await self.service.customer_details(record,doc))['warnings'])

    async def test_missing_tilda_or_contact_fields_do_not_block_an_exact_document(self):
        record={'request':{'order_number':'123456789'}}
        doc={'name':'Возврат-123456789'}
        self.service.ms=AsyncMock(side_effect=AssertionError('No contact request is required'))
        self.assertTrue(await self.service.customer(record,doc))
        self.assertEqual((await self.service.customer_details(record,doc))['customer'],{'name':'','email':'','phone':''})
        self.service.ms.assert_not_awaited()

    def test_missing_size_and_partial_model_cannot_match(self):
        self.assertFalse(names_match('DEMO PUFFERS (M, black)','DEMO PUFFERS'))
        self.assertFalse(names_match('DEMO (M, black)','DEMO WINDBREAKER, Размер=M'))
        self.assertTrue(names_match('DEMO PUFFERS (M, black)','DEMO PUFFERS, Размер=M -20%'))

    async def test_payment_lookup_uses_tilda_date_when_ms_order_was_renamed(self):
        db=Database(self.base/'crm.sqlite3');db.initialize();self.service.crm_path=db.path
        with db.connect() as c:
            c.execute("INSERT INTO orders(order_number,created_at,updated_at) VALUES(?,?,?)",('123456789',1788276565,1788276565))
        self.service.ms=AsyncMock(side_effect=AssertionError('Renamed MS document is not the source date'))
        payment={'id':'matched','metadata':{'tilda_orderid':'123456789'}}
        self.service.yoo.pages=AsyncMock(return_value=[payment,{'metadata':{'tilda_orderid':'other'}}])
        self.assertEqual(await self.service.payment({'request':{'order_number':'123456789'}},{}),payment)
        params=self.service.yoo.pages.call_args.args[1]
        self.assertTrue(params['created_at.gte'].startswith('2026-08-30'))
        self.assertTrue(params['created_at.lte'].startswith('2026-09-04'))
        self.service.yoo.pages.return_value=[payment,payment]
        with self.assertRaises(RefundError):
            await self.service.payment({'request':{'order_number':'123456789'}},{})

    def test_gate_expires_when_shop_or_keys_or_brand_scope_change(self):
        folder=self.base/'.secrets'
        for name in ('refunds_config.json','moysklad.json','yookassa.json'):private_json(folder/name,{'test':'fake'})
        with self.assertRaises(RefundError):require_gate(self.base,'yookassa')
        private_json(folder/'yookassa_refunds_enabled',{'fingerprint':gate_fingerprint(self.base,'yookassa')})
        require_gate(self.base,'yookassa')
        private_json(folder/'moysklad.json',{'test':'changed'})
        with self.assertRaises(RefundError):require_gate(self.base,'yookassa')
        self.assertEqual((folder/'moysklad.json').stat().st_mode & 0o777,0o600)

    def test_unchecked_new_request_survives_restart(self):
        store=self.service.store
        record,_=store.ingest(-1001,5,'123456789\nЮKassa, за вещь',3)
        restarted=RefundStore(store.path)
        self.assertEqual([r['id'] for r in restarted.list(pending=True)],[record['id']])


class ReadOnlyConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_yookassa_verification_reads_shop_payment_registered_receipt_only(self):
        calls=[]
        def respond(request):
            calls.append(request)
            if request.url.path.endswith('/me'):data={'account_id':'500000','test':False,'status':'enabled','fiscalization':{'enabled':True,'provider':'atol'}}
            elif '/payments/' in request.url.path:data={'status':'succeeded','paid':True,'test':False,'recipient':{'account_id':'500000'},'amount':{'value':'100.00','currency':'RUB'}}
            else:data={'items':[{'type':'payment','status':'succeeded'}]}
            return httpx.Response(200,json=data)
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            data=await verify_yoo('500000','fake-secret','00000000-0000-4000-8000-000000000001',client)
        self.assertEqual(data['shop_id'],'500000');self.assertEqual(len(calls),3)
        self.assertTrue(all(r.method=='GET' for r in calls))

    async def test_split_verification_get_only_and_wrong_merchant_stops_setup(self):
        calls=[]
        def respond(request):
            calls.append(request)
            return httpx.Response(200,json={'code':200,'status':'success','data':{'order':{'merchantId':ORG,'orderId':'1234:123456789','currencyCode':'RUB','paymentStatus':'CAPTURED','paymentMethod':{'methodType':'SPLIT'},'orderAmount':'100.00','cart':{'items':[{'productId':'1'}]}}}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            data=await verify_split(ORG,'fake-merchant-secret','1234:123456789',client,'100.00')
            self.assertFalse(data['fiscalization_verified'])
            with self.assertRaises(ValueError):await verify_split(AGENT,'fake-merchant-secret','1234:123456789',client,'100.00')
        self.assertTrue(all(r.method=='GET' for r in calls))

class JournalConcurrencyTests(unittest.TestCase):
    def test_stale_card_for_second_document_is_blocked_after_first_refund_finishes(self):
        from customer_refunds import hashed
        with tempfile.TemporaryDirectory() as folder, patch('customer_refunds.OWNER',12345):
            store=RefundStore(Path(folder)/'journal.sqlite3')
            def prepare(message,doc,previous):
                r,_=store.ingest(-1001,message,'123456789\nЮKassa, за часть товаров',1)
                return store.prepare(r['id'],r['revision'],{'payment_id':'payment','order':{'id':doc},'amount_cents':10000,'previous_refunds':previous})
            first=prepare(1,'doc1',[])
            stale=prepare(2,'doc2',[])
            first=store.claim(first['id'],first['revision'],12345)
            store.update(first['id'],state='succeeded',refund_id='refund-1')
            restarted=RefundStore(store.path)
            with self.assertRaises(RefundError):restarted.claim(stale['id'],stale['revision'],12345)
            fresh=prepare(3,'doc2',['refund-1'])
            self.assertEqual(restarted.claim(fresh['id'],fresh['revision'],12345)['state'],'submitting')
