import unittest
import httpx
from refund_setup import verify_split

MERCHANT='00000000-0000-4000-8000-000000000001'
OID='10000000:1010101006'

class SplitConnectionDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def verify(self,method,require_split=True,merchant=MERCHANT,amount='7990.00'):
        calls=[]
        def respond(request):
            calls.append(request)
            return httpx.Response(200,json={'code':200,'status':'success','data':{'order':{'merchantId':merchant,'orderId':OID,'currencyCode':'RUB','paymentStatus':'CAPTURED','paymentMethod':method,'orderAmount':amount,'cart':{'items':[{'productId':'1'}]}}}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result=await verify_split(MERCHANT,'fake-diagnostic-key',OID,client,'7990.00',require_split=require_split)
        self.assertEqual(len(calls),1);self.assertEqual(calls[0].method,'GET')
        return result

    async def test_card_connection_can_be_inspected_but_never_confirms_split(self):
        data=await self.verify({'methodType':'CARD'},False)
        self.assertEqual(data['sample_payment_method'],'CARD');self.assertFalse(data['split_verified'])
        self.assertFalse(data['fiscalization_verified'])
        with self.assertRaisesRegex(ValueError,'CARD'):await self.verify({'methodType':'CARD'})

    async def test_unknown_shape_requires_diagnostics_instead_of_claiming_another_method(self):
        for raw in (None,{},'SPLIT',{'type':'SPLIT'},{'methodType':[]}):
            data=await self.verify(raw,False)
            self.assertFalse(data['split_verified']);self.assertIsNone(data['sample_payment_method'])
            with self.assertRaisesRegex(ValueError,'ожидаемом формате'):await self.verify(raw)

    async def test_diagnostic_mode_still_rejects_wrong_shop_and_amount(self):
        with self.assertRaisesRegex(ValueError,'другому магазину'):await self.verify({'methodType':'SPLIT'},False,merchant='other')
        with self.assertRaisesRegex(ValueError,'Сумма'):await self.verify({'methodType':'SPLIT'},False,amount='8000.00')

    async def test_both_documented_split_types_pass_strict_checks(self):
        for method in ('SPLIT','SPLIT_SBP'):
            data=await self.verify({'methodType':method})
            self.assertTrue(data['split_verified'])
            self.assertFalse(data['fiscalization_verified'])
