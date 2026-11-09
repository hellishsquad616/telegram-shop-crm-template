import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from customer_refunds import RefundService, PaymentLookupError, RefundError
from tests.test_customer_refunds import TEXT, NUMBER, PID, payment, order


class YooPaymentDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name)
        self.service=RefundService(self.base/'refunds.sqlite3',self.base,'fake')
        self.record,_=self.service.store.ingest(-1009999999999,1,TEXT,100)
        self.service.ms=AsyncMock(return_value={'rows':[order()]})
        self.service.yoo.pages=AsyncMock(return_value=[payment()])
        self.service.yoo.get=AsyncMock(return_value=payment())
        self.service.yoo.call=AsyncMock(side_effect=AssertionError('No financial writes'))
        p=patch('customer_refunds.SHOP_ID','382526');p.start();self.addCleanup(p.stop)

    async def test_missing_crm_and_only_renamed_ms_return_searches_payment_history_by_order(self):
        found=await self.service.payment(self.record,order())
        self.assertEqual(found['id'],PID)
        self.service.yoo.pages.assert_awaited_once_with('/payments',{'status':'succeeded'},max_pages=160)
        lookup=self.service.store.get(self.record['id'])['payment_lookup']
        self.assertEqual(lookup['order_number'],NUMBER)
        self.assertEqual(lookup['source_hash'],self.record['source_hash'])
        self.assertEqual(lookup['payment_id'],PID)
        self.assertIsNone(self.service.store.get(self.record['id'])['attempted_at'])
        self.service.yoo.call.assert_not_awaited()
        # Reopening or confirming does an exact GET rather than another scan.
        self.service.yoo.pages.reset_mock()
        await self.service.payment(self.service.store.get(self.record['id']),order())
        self.service.yoo.pages.assert_not_awaited()
        self.service.yoo.get.assert_awaited_once_with('/payments/'+PID)

    async def test_ambiguous_payments_or_truncated_history_never_choose_first(self):
        other=copy.deepcopy(payment());other['id']='11111111-1111-4111-8111-000000000002'
        self.service.yoo.pages.return_value=[payment(),other]
        with self.assertRaises(PaymentLookupError) as error:await self.service.payment(self.record,order())
        self.assertEqual(error.exception.status,'ambiguous')
        self.assertIsNone(self.service.store.get(self.record['id'])['payment_lookup'])
        self.service.yoo.pages.side_effect=RefundError('Pagination limit exceeded')
        with self.assertRaises(RefundError):await self.service.payment(self.record,order())

    async def test_wrong_ms_code_reports_actual_mismatch_without_binding_another_order(self):
        other=copy.deepcopy(payment());other['metadata']['tilda_orderid']='1010101009'
        self.service.yoo.pages.return_value=[other]
        doc={**order(),'code':'1010101009'}
        with self.assertRaises(PaymentLookupError) as error:await self.service.payment(self.record,doc)
        self.assertEqual(error.exception.status,'order_mismatch')
        self.assertIn('1010101009',str(error.exception))
        self.assertIn(NUMBER,str(error.exception))
        saved=self.service.store.get(self.record['id'])
        self.assertIsNone(saved['payment_lookup']);self.assertIsNone(saved['attempted_at'])

    async def test_wrong_imported_crm_date_uses_fallback_and_does_not_change_financial_source(self):
        self.service.crm_path=str(self.base/'crm.sqlite3')
        self.service.yoo.pages.side_effect=[[],[payment()]]
        with patch('database.Database') as db:
            db.return_value.get_order_by_number.return_value={'created_at':1700000000}
            found=await self.service.payment(self.record,order())
        self.assertEqual(found['id'],PID)
        self.assertIn('created_at.gte',self.service.yoo.pages.await_args_list[0].args[1])
        self.assertEqual(self.service.yoo.pages.await_args_list[1].args[1],{'status':'succeeded'})

    async def test_bound_snapshot_is_only_search_hint_and_payment_order_is_rechecked(self):
        r={**self.record,'ms_order_id':'return-copy','snapshot':{'source_hash':self.record['source_hash'],
             'order':{'id':'return-copy'},'payment_id':PID}}
        found=await self.service.payment(r,order())
        self.assertEqual(found['id'],PID)
        self.service.yoo.pages.assert_not_awaited()
        wrong=copy.deepcopy(payment());wrong['metadata']['tilda_orderid']='9999999999'
        self.service.yoo.get.return_value=wrong
        with self.assertRaises(RefundError):await self.service.payment(r,order())

    async def test_synthetic_receipt_lookup_can_search_without_mutating_a_real_request(self):
        r={'request':self.record['request'],'snapshot':{},'state':'succeeded'}
        found=await self.service.payment(r,{})
        self.assertEqual(found['id'],PID)
        self.assertIsNone(self.service.store.get(self.record['id'])['payment_lookup'])
