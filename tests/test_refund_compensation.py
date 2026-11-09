import asyncio
import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import tests.test_customer_refunds as yoo
import tests.test_yandex_refunds as ya
from customer_refunds import RefundService, RefundError, parse_request, selected_document_items, make_receipt, document_items
from customer_refunds_ui import RefundUI, card


def setUpModule():
    yoo.setUpModule()
    ya.setUpModule()


def tearDownModule():
    ya.tearDownModule()
    yoo.tearDownModule()


def comp_text(provider='Полная', name='DIAMOND ZIP HOODIE, Размер=M', amount='3 150'):
    return (yoo.NUMBER+'\nname: Тестовый Покупатель\nemail: buyer@example.test\nphone: +79990000000\n'
            +provider+', только за вещь\nТовар: '+name+'\nКомпенсация клиенту — '+amount+' ₽.\nВещь остаётся у клиента.')


def comp_document():
    doc=yoo.order();doc['sum']=315000
    positions=[{'id':'atelier','quantity':1,'price':315000,'assortment':{'name':'Ателье'}}]
    return doc,positions


class CompensationDomainTests(unittest.TestCase):
    def test_example_and_variants_parse_as_explicit_compensation(self):
        for reason in ['Компенсация клиенту — 3 150 ₽.', 'Ателье: 3\u202f150 ₽', 'Разница в цене — 3150,00 руб.']:
            text=comp_text().replace('Компенсация клиенту — 3 150 ₽.',reason)
            q=parse_request(text)
            self.assertEqual(q['scope'],'compensation')
            self.assertEqual(q['compensation_cents'],315000)
            self.assertEqual(q['item_selector'],'DIAMOND ZIP HOODIE, Размер=M')
            raw,selected=selected_document_items(*comp_document(),q)
            self.assertEqual(raw,selected)
        q=parse_request(comp_text().replace('только за вещь','компенсация'))
        self.assertEqual(q['scope'],'compensation')
        self.assertNotIn('item_selector_error',q)

    def test_missing_or_conflicting_amount_never_falls_back_to_full_goods(self):
        for text in [comp_text().replace('3 150 ₽','неизвестно'),
                     comp_text()+'\nРазница в цене — 3000 ₽',comp_text(amount='0,50'),comp_text(amount='3,150'),comp_text(amount='-3150')]:
            q=parse_request(text)
            self.assertEqual(q['scope'],'compensation')
            with self.assertRaises(RefundError):selected_document_items(*comp_document(),q)

    def test_document_must_equal_compensation_and_cannot_include_shipping_or_whole_goods_price(self):
        q=parse_request(comp_text())
        for doc,positions in [ (yoo.order(),yoo.positions()), comp_document() ]:
            if doc['sum']==315000:doc['sum']+=1
            with self.assertRaises(RefundError):selected_document_items(doc,positions,q)
        doc,positions=comp_document();positions[0]['assortment']['name']='Доставка CDEK'
        with self.assertRaises(RefundError):selected_document_items(doc,positions,q)

    def test_goods_and_delivery_select_correct_subset_from_mixed_ms_document(self):
        doc=yoo.order(True);positions=yoo.positions(True)
        for scope,chosen in [('goods','position-a'),('part','position-a'),('delivery','position-b')]:
            raw,items=selected_document_items(doc,positions,{'scope':scope})
            self.assertEqual(len(raw),2)
            self.assertEqual([i['id'] for i in items],[chosen])

    def test_previous_price_compensation_preserves_unit_and_only_remaining_price_is_returned(self):
        prior=copy.deepcopy(yoo.receipts()[0]);prior['type']='refund';prior['items']=prior['items'][:1]
        prior['items'][0]['amount']['value']='3150.00'
        receipt=make_receipt(yoo.payment(),yoo.receipts(),[prior],document_items(yoo.order(),yoo.positions(),'goods'),{},'goods')
        self.assertEqual(receipt['items'][0]['quantity'],'1')
        self.assertEqual(receipt['items'][0]['amount']['value'],'7962.00')
        wrong_currency=copy.deepcopy(prior);wrong_currency['items'][0]['amount']['currency']='USD'
        with self.assertRaises(RefundError):
            make_receipt(yoo.payment(),yoo.receipts(),[wrong_currency],document_items(yoo.order(),yoo.positions(),'goods'),{},'goods')
        # A later full return exhausts this unit: another copy cannot refund it.
        next_receipt=copy.deepcopy(prior);next_receipt['items']=receipt['items']
        with self.assertRaises(RefundError):
            make_receipt(yoo.payment(),yoo.receipts(),[prior,next_receipt],document_items(yoo.order(),yoo.positions(),'goods'),{},'goods')


class CompensationServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name)
        self.service=RefundService(self.base/'refunds.sqlite3',self.base,'fake')
        self.service.customer=AsyncMock(return_value='customer-fixture')
        self.doc,self.positions=comp_document()
        self.service.document=AsyncMock(side_effect=lambda *_:copy.deepcopy((self.doc,self.positions)))
        self.service.payment=AsyncMock(return_value=yoo.payment())
        self.service.yoo.pages=AsyncMock(side_effect=lambda path,params: [] if path=='/refunds' else yoo.receipts())
        self.service.yoo.call=AsyncMock(return_value=yoo.result(value='3150.00'))
        self.service.sync_ms=AsyncMock()
        self.store=self.service.store
        self.record,_=self.store.ingest(yoo.SUPPORT_CHAT,1,comp_text(),100)

    async def test_yoo_returns_exact_compensation_not_full_price_and_preserves_fiscal_fields(self):
        s=await self.service.preview(self.record,self.doc['id'])
        self.assertEqual(s['amount_cents'],315000)
        self.assertTrue(s['compensation']['item_stays_with_customer'])
        receipt=s['body']['receipt']
        self.assertEqual(receipt['items'][0]['amount']['value'],'3150.00')
        self.assertEqual(receipt['items'][0]['description'],yoo.receipts()[0]['items'][0]['description'])
        self.assertEqual(receipt['items'][0]['vat_code'],8)
        self.service.yoo.call.assert_not_awaited()
        ready=self.store.prepare(self.record['id'],self.record['revision'],s)
        self.assertIn('Вещь остаётся у клиента',card(ready))
        yoo.enable_gate(self.base,'yookassa')
        saved=await self.service.execute(ready['id'],ready['revision'],yoo.OWNER)
        self.assertEqual(saved['state'],'succeeded')
        self.assertEqual(self.service.yoo.call.call_count,1)
        with self.assertRaises(RefundError):await self.service.execute(ready['id'],ready['revision'],yoo.OWNER)
        self.assertEqual(self.service.yoo.call.call_count,1)

    async def test_wrong_product_size_or_excess_amount_blocks_before_money(self):
        for text in [comp_text(name='DIAMOND ZIP HOODIE, Размер=XS'),comp_text(amount='12 000')]:
            r,_=self.store.ingest(yoo.SUPPORT_CHAT,2,text,100)
            if r['request']['compensation_cents']==1200000:
                self.doc['sum']=1200000;self.positions[0]['price']=1200000
            with self.assertRaises(RefundError):await self.service.preview(r,self.doc['id'])
        self.service.yoo.call.assert_not_awaited()

    async def test_yandex_reduces_selected_price_keeps_quantity_and_other_cart_items(self):
        text=comp_text(provider='Сплит',name=ya.item()['title']).replace(yoo.NUMBER,ya.NUMBER)
        r,_=self.store.ingest(yoo.SUPPORT_CHAT,3,text,100)
        self.doc['name']='возврат-'+ya.NUMBER
        data=ya.provider_data()
        self.service.split.api.order=AsyncMock(return_value=data)
        self.service.split.api.order_id=Mock(return_value=ya.OID)
        s=await self.service.preview(r,self.doc['id'])
        self.assertEqual(s['amount_cents'],315000)
        self.assertEqual(s['body']['targetCart']['items'],[{'productId':'0','price':'7242.00'},{'productId':'1'}])
        self.assertNotIn('refundCart',s['body'])
        self.assertTrue(all('quantityCount' not in i for i in s['body']['targetCart']['items']))
        self.service.yoo.call.assert_not_awaited()

    async def test_manual_old_success_rechecks_exact_yoo_operation_only(self):
        r=self.store.update(self.record['id'],state='succeeded',refund_id='old-refund',payment_id=yoo.PID,
                            amount_cents=315000,ms_synced=1,receipt_json={'id':'receipt'})
        self.service.yoo.get=AsyncMock(return_value={**yoo.result(value='3150.00'),'id':'old-refund'})
        await self.service.check(r,force=True)
        self.service.yoo.get.assert_awaited_once_with('/refunds/old-refund')
        self.service.yoo.call.assert_not_awaited()

    async def test_yandex_mixed_document_goods_scope_excludes_delivery(self):
        text=ya.TEXT
        r,_=self.store.ingest(yoo.SUPPORT_CHAT,4,text,100)
        self.doc=ya.document();self.positions=[ya.position(),ya.position(True)]
        self.service.split.api.order=AsyncMock(return_value=ya.provider_data())
        self.service.split.api.order_id=Mock(return_value=ya.OID)
        s=await self.service.preview(r,self.doc['id'])
        self.assertEqual(s['amount_cents'],1039200)
        self.assertEqual(len(s['financial_items']),1)
        self.assertEqual(len(s['order']['items']),2)
        self.assertIn('selected_items',s)

    async def test_yandex_compensation_execute_requires_confirmation_and_tracks_same_operation(self):
        r,_=self.store.ingest(yoo.SUPPORT_CHAT,5,comp_text('Сплит',ya.item()['title']).replace(yoo.NUMBER,ya.NUMBER),100)
        self.doc['name']='возврат-'+ya.NUMBER
        self.service.split.api.order_id=Mock(return_value=ya.OID)
        data=ya.provider_data()
        self.service.split.api.order=AsyncMock(return_value=data)
        self.service.split.api.enabled=Mock()
        async def refund(oid,body,key):
            op=ya.operation(key,'SUCCESS',body['refundAmount'])
            data['operations'].append(op)
            return {'operation':op}
        self.service.split.api.refund=AsyncMock(side_effect=refund)
        s=await self.service.preview(r,self.doc['id'])
        with self.assertRaises(RefundError):await self.service.execute(r['id'],r['revision'],yoo.OWNER)
        self.service.split.api.refund.assert_not_awaited()
        ready=self.store.prepare(r['id'],r['revision'],s)
        saved=await self.service.execute(ready['id'],ready['revision'],yoo.OWNER)
        self.assertEqual(saved['state'],'succeeded')
        self.assertEqual(saved['amount_cents'],315000)
        self.service.split.api.refund.assert_awaited_once()


class RefundButtonRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        base=Path(self.temp.name)
        self.ui=RefundUI(base/'refunds.sqlite3',base,'fake',lambda _:False)
        self.record,_=self.ui.store.ingest(yoo.SUPPORT_CHAT,1,yoo.TEXT,100)
        self.context=SimpleNamespace(bot=SimpleNamespace(edit_message_text=AsyncMock()),user_data={})
        message=SimpleNamespace(message_id=777,reply_text=AsyncMock())
        self.update=SimpleNamespace(effective_message=message,
            callback_query=SimpleNamespace(data=f'cr:check:{self.record["id"]}:1',edit_message_text=AsyncMock()))

    async def test_refresh_binds_clicked_hidden_old_card_and_renders_check_timestamp(self):
        r=self.ui.store.update(self.record['id'],owner_message_id=0,state='succeeded',ms_synced=1,
                               receipt_json={'id':'saved'},refund_id='old-refund')
        self.ui.service.check=AsyncMock(return_value=r)
        self.ui.service.receipt=AsyncMock(return_value=r)
        await self.ui.dispatch_safe(self.update,self.context)
        self.assertEqual(self.ui.store.get(r['id'])['owner_message_id'],777)
        self.ui.service.check.assert_awaited_once()
        self.assertTrue(self.ui.service.check.await_args.kwargs['force'])
        self.context.bot.edit_message_text.assert_awaited_once()
        self.assertEqual(self.context.bot.edit_message_text.call_args.kwargs['message_id'],777)
        self.assertIn('Последняя проверка:',self.context.bot.edit_message_text.call_args.kwargs['text'])

    async def test_refresh_during_slow_worker_opens_card_without_waiting_or_double_get(self):
        lock=self.ui.locks.setdefault(self.record['id'],asyncio.Lock())
        await lock.acquire()
        self.ui.service.check=AsyncMock()
        try:
            await asyncio.wait_for(self.ui.dispatch_safe(self.update,self.context),timeout=0.5)
        finally:lock.release()
        self.update.callback_query.edit_message_text.assert_awaited_once()
        self.ui.service.check.assert_not_awaited()
        self.assertEqual(self.ui.store.get(self.record['id'])['owner_card_pending'],1)
        self.assertTrue(self.ui.wakeup.is_set())

    def test_missing_scope_standard_request_offers_owner_scope_buttons(self):
        r,_=self.ui.store.ingest(yoo.SUPPORT_CHAT,9,yoo.NUMBER+'\nПолная',100)
        data=[b.callback_data for row in self.ui.keyboard(r).inline_keyboard for b in row]
        self.assertIn(f"cr:scope:{r['id']}:{r['revision']}:delivery",data)
        self.assertIn(f"cr:scope:{r['id']}:{r['revision']}:goods",data)
