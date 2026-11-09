import json
import tempfile
import unittest
from pathlib import Path

import httpx
from customer_refunds import RefundError
from refund_setup import gate_fingerprint, private_json, require_gate, save_yoo_connection

SHOP='1196399'
PAYMENT='00000000-0000-4000-8000-000000000001'

class YooConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def connect(self,base,*,wrong_shop=False,missing_receipt=False,amount='16195.68'):
        calls=[]
        def respond(request):
            calls.append(request)
            if request.url.path.endswith('/me'):
                data={'account_id':'other' if wrong_shop else SHOP,'test':False,'status':'enabled','fiscalization':{'enabled':True,'provider':'atol'}}
            elif '/payments/' in request.url.path:
                data={'id':PAYMENT,'status':'succeeded','paid':True,'test':False,'recipient':{'account_id':SHOP},'amount':{'value':amount,'currency':'RUB'}}
            else:data={'items':[] if missing_receipt else [{'type':'payment','status':'succeeded'}]}
            return httpx.Response(200,json=data)
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result=await save_yoo_connection(base,SHOP,'fake-live-key',PAYMENT,client,'16195.68')
        self.assertTrue(all(r.method=='GET' for r in calls))
        return result

    async def test_verified_yoo_connection_does_not_invalidate_existing_split_gate(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder);secret=base/'.secrets'
            for name in ('refunds_config.json','moysklad.json','yandex_pay.json'):private_json(secret/name,{'test':'unchanged'})
            private_json(secret/'yandex_pay_refunds_enabled',{'fingerprint':gate_fingerprint(base,'yandex_pay')})
            original=(secret/'refunds_config.json').read_bytes();marker=(secret/'yandex_pay_refunds_enabled').read_bytes()
            result=await self.connect(base)
            self.assertTrue(result['ready_for_activation']);require_gate(base,'yandex_pay')
            self.assertEqual((secret/'refunds_config.json').read_bytes(),original)
            self.assertEqual((secret/'yandex_pay_refunds_enabled').read_bytes(),marker)
            self.assertFalse((secret/'yookassa_refunds_enabled').exists())
            self.assertEqual((secret/'yookassa.json').stat().st_mode & 0o777,0o600)
            self.assertEqual(json.loads((secret/'yookassa.json').read_text())['shop_id'],SHOP)

    async def test_registered_receipt_or_amount_failure_preserves_verified_connection_only(self):
        for options in ({'missing_receipt':True},{'amount':'16000.00'}):
            with tempfile.TemporaryDirectory() as folder:
                base=Path(folder);result=await self.connect(base,**options)
                self.assertTrue(result['connection_saved']);self.assertFalse(result['ready_for_activation'])
                self.assertTrue((base/'.secrets/yookassa.json').exists())
                with self.assertRaises(RefundError):require_gate(base,'yookassa')
                self.assertNotIn('fake-live-key',json.dumps(result))

    async def test_wrong_shop_never_saves_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder)
            with self.assertRaises(ValueError):await self.connect(base,wrong_shop=True)
            self.assertFalse((base/'.secrets/yookassa.json').exists())
