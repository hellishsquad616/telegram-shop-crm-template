import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock,patch

from customer_refunds import RefundService,RefundError
from customer_refunds_ui import RefundUI,card
from completed_refunds import approve_completed
from tests.test_yandex_refunds import OWNER,SUPPORT_CHAT,SUPPORT_TOPIC,MERCHANT,TEXT,OID,OP,document,position,provider_data,operation


class CompletedYandexTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for target,value in (("customer_refunds.OWNER",OWNER),("completed_refunds.OWNER",OWNER),
                ("customer_refunds_ui.OWNER",OWNER),("customer_refunds_ui.SUPPORT_CHAT",SUPPORT_CHAT),
                ("customer_refunds_ui.SUPPORT_TOPIC",SUPPORT_TOPIC),("yandex_refunds.OWNER",OWNER),("yandex_refunds.MERCHANT",MERCHANT)):
            p=patch(target,value);p.start();self.addCleanup(p.stop)
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name)
        self.service=RefundService(self.base/'refunds.sqlite3',self.base,'fake')
        self.store=self.service.store
        self.record,_=self.store.ingest(SUPPORT_CHAT,1,TEXT.replace('за вещь','за всё'),100)
        self.doc=document();self.positions=[position(),position(True)];self.data=provider_data()
        self.service.check_brand=Mock()
        self.service.document=AsyncMock(side_effect=lambda _: (copy.deepcopy(self.doc),copy.deepcopy(self.positions)))
        self.service.customer=AsyncMock(return_value='same-customer')
        self.service.customer_details=AsyncMock(return_value={'customer':{'name':'Test','email':'buyer@example.test'},'receipt_contact':{'email':'buyer@example.test'},'warnings':[]})
        self.api=self.service.split.api
        self.api.order_id=Mock(return_value=OID)
        self.api.order=AsyncMock(side_effect=lambda _:copy.deepcopy(self.data))
        self.api.refund=AsyncMock(side_effect=AssertionError('Never send money'))
        self.api.operation=AsyncMock(side_effect=AssertionError('Reconcile by original order'))
        self.api.enabled=Mock(side_effect=AssertionError('No money gate needed for reading'))
        self.service.sync_ms=AsyncMock(side_effect=AssertionError('Preserve MS'))
        self.service.book_pending_split=AsyncMock(side_effect=AssertionError('Already marked in MS'))
        self.service.ms=AsyncMock(side_effect=AssertionError('No MS writes'))

    async def cached_card_then_existing_operation(self,status='PENDING'):
        snapshot=await self.service.preview(self.record,self.doc['id'])
        self.record=self.store.prepare(self.record['id'],self.record['revision'],snapshot)
        self.doc['state']={'name':'Возврат-проведен'}
        self.doc['attributes']=[{'name':'Дата проведения возврата средств','value':'2026-10-22 11:36:00.000'}]
        self.op=operation('11111111-1111-4111-8111-000000000005',status,'11073.09')
        self.data['operations'].append(self.op)

    async def ready_existing(self):
        snapshot=await self.service.preview(self.store.get(self.record['id']),self.doc['id'])
        return self.store.prepare(self.record['id'],self.store.get(self.record['id'])['revision'],snapshot)

    def ui(self):
        ui=RefundUI(self.store.path,self.base,'fake',lambda _:False);ui.service=self.service
        context=SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=88)),
            edit_message_text=AsyncMock(),send_photo=AsyncMock(return_value=SimpleNamespace(message_id=89)),
            edit_message_media=AsyncMock(),edit_message_caption=AsyncMock()))
        update=SimpleNamespace(callback_query=SimpleNamespace(edit_message_text=AsyncMock()),
            effective_message=SimpleNamespace(message_id=99,reply_text=AsyncMock()))
        return ui,context,update

    async def test_existing_pending_operation_is_ready_for_document_approval_and_never_for_money(self):
        await self.cached_card_then_existing_operation()
        ready=await self.ready_existing()
        self.assertTrue(ready['snapshot']['existing_refund']);self.assertEqual(ready['state'],'ready')
        self.assertIsNone(ready['attempted_at']);self.assertIsNone(ready['receipt_json'])
        self.assertIn('Яндекс уже обрабатывает возврат',card(ready))
        self.assertNotIn('Уже возвращено:',card(ready))
        with self.assertRaises(RefundError):await self.service.execute(ready['id'],ready['revision'],OWNER)
        self.api.refund.assert_not_awaited();self.api.enabled.assert_not_called()

    async def test_existing_split_with_prepayment_submit_has_receipt_proof_and_original_paid_date(self):
        import uuid
        base=copy.deepcopy(self.data['operations'][0]);base['created']='2026-10-10T13:00:00+03:00'
        self.data['operations']=[{**base,'operationType':'PREPAYMENT'},
            {**base,'operationId':str(uuid.uuid4()),'operationType':'SUBMIT','created':'2026-10-10T13:01:00+03:00'}]
        await self.cached_card_then_existing_operation()
        ready=await self.ready_existing()
        proof=ready['snapshot']['existing_refund']['proof']
        self.assertEqual(proof['paid_at'],'2026-10-10T13:01:00+03:00')
        self.assertEqual(ready['snapshot']['payment_amount_cents'],1107309)
        self.api.refund.assert_not_awaited()
        self.assertIn('Яндекс уже обрабатывает возврат',card(ready))
        self.assertNotIn('Уже возвращено:',card(ready))
        with self.assertRaises(RefundError):await self.service.execute(ready['id'],ready['revision'],OWNER)
        self.api.refund.assert_not_awaited();self.api.enabled.assert_not_called()

    async def test_existing_card_refund_is_reconciled_without_creating_a_new_operation(self):
        self.data['order']['paymentMethod']={'methodType':'CARD'}
        await self.cached_card_then_existing_operation()
        ready=await self.ready_existing()
        self.assertEqual(ready['snapshot']['payment_method'],'CARD')
        self.assertIn('Оплата: Яндекс Пэй · Карта',card(ready))
        with self.assertRaises(RefundError):await self.service.execute(ready['id'],ready['revision'],OWNER)
        self.api.refund.assert_not_awaited();self.api.enabled.assert_not_called()

    async def test_old_cash_confirmation_becomes_document_confirmation_without_sending_money(self):
        await self.cached_card_then_existing_operation()
        with self.assertRaises(RefundError):await self.service.execute(self.record['id'],self.record['revision'],OWNER)
        current=self.store.get(self.record['id'])
        self.assertTrue(current['snapshot']['existing_refund']);self.assertEqual(current['state'],'ready')
        self.api.refund.assert_not_awaited()

    async def test_approval_publishes_pending_document_once_and_updates_it_to_success_after_restart(self):
        await self.cached_card_then_existing_operation();ready=await self.ready_existing()
        ui,context,update=self.ui()
        await ui.dispatch(update,context,['cr','confirm',str(ready['id']),str(ready['revision'])])
        bits=update.callback_query.edit_message_text.await_args.kwargs['reply_markup'].inline_keyboard[0][0].callback_data.split(':')
        self.assertEqual(bits[1],'publish')
        await ui.dispatch(update,context,bits)
        self.assertEqual(self.store.get(ready['id'])['state'],'pending')
        context.bot.send_photo.assert_not_awaited()
        await ui.reconcile_pending(context);await ui.reconcile_pending(context)
        context.bot.send_photo.assert_awaited_once()
        self.assertIn('в процессе',context.bot.send_photo.await_args.kwargs['caption'])
        self.assertIn('Не является фискальным чеком',context.bot.send_photo.await_args.kwargs['caption'])
        self.assertIn('22.10.2026 11:36',context.bot.send_photo.await_args.kwargs['caption'])
        self.op['status']='SUCCESS';self.data['order']['paymentStatus']='REFUNDED'
        self.data['order']['orderAmount']='0.00';self.data['order']['cart']={'items':[],'total':{'amount':'0.00'}}
        restarted,_,_=self.ui()
        await restarted.reconcile_pending(context);await restarted.reconcile_pending(context)
        saved=self.store.get(ready['id'])
        self.assertEqual(saved['state'],'succeeded');self.assertTrue(saved['completed_message_id'])
        context.bot.send_photo.assert_awaited_once();context.bot.edit_message_media.assert_awaited_once()
        self.api.refund.assert_not_awaited();self.api.operation.assert_not_awaited()
        self.service.sync_ms.assert_not_awaited();self.service.book_pending_split.assert_not_awaited()
        self.assertNotIn('Возврат в обработке:',card(saved))

    async def test_pending_external_id_can_be_absent_and_still_reconciles_by_order(self):
        await self.cached_card_then_existing_operation();self.op['externalOperationId']=None
        ready=await self.ready_existing();saved=await approve_completed(self.service,ready['id'],ready['revision'],OWNER)
        self.assertIsNone(saved['idempotence_key'])
        ui,context,_=self.ui();await ui.reconcile_pending(context)
        context.bot.send_photo.assert_awaited_once()
        self.api.refund.assert_not_awaited()

    async def test_existing_success_with_empty_cart_uses_the_verified_original_snapshot(self):
        await self.cached_card_then_existing_operation('SUCCESS')
        self.data['order']['paymentStatus']='REFUNDED';self.data['order']['orderAmount']='0.00'
        self.data['order']['cart']={'items':[],'total':{'amount':'0.00'}}
        ready=await self.ready_existing();saved=await approve_completed(self.service,ready['id'],ready['revision'],OWNER)
        self.assertEqual(saved['state'],'succeeded');self.assertEqual(saved['amount_cents'],1107309)
        self.assertEqual(len(saved['snapshot']['financial_items']),2)

    async def test_transient_recheck_failure_preserves_original_prices_for_later_empty_cart(self):
        await self.cached_card_then_existing_operation()
        self.store.preparation_error(self.record['id'],self.record['revision'],RefundError('Яндекс временно недоступен'))
        failed=self.store.get(self.record['id'])
        self.assertEqual(failed['state'],'needs_info');self.assertTrue(failed['snapshot'])
        self.op['status']='SUCCESS';self.data['order']['paymentStatus']='REFUNDED';self.data['order']['orderAmount']='0.00'
        self.data['order']['cart']={'items':[],'total':{'amount':'0.00'}}
        snapshot=await self.service.preview(failed,self.doc['id'])
        self.assertEqual(snapshot['existing_refund']['status'],'succeeded')
        self.assertEqual(snapshot['amount_cents'],1107309)

    async def test_untracked_full_success_with_original_cart_can_be_verified_without_old_card(self):
        await self.cached_card_then_existing_operation('SUCCESS')
        other,_=self.store.ingest(SUPPORT_CHAT,2,TEXT.replace('за вещь','за всё'),100)
        snapshot=await self.service.preview(other,self.doc['id'])
        self.assertEqual(snapshot['existing_refund']['status'],'succeeded')

    async def test_partial_untracked_operation_cannot_be_matched_just_by_equal_amount(self):
        await self.cached_card_then_existing_operation()
        self.op['amount']='10392.00'
        with self.assertRaises(RefundError):await self.ready_existing()
        self.api.refund.assert_not_awaited()

    async def test_no_provider_refund_or_wrong_merchant_never_creates_a_document_or_money(self):
        await self.cached_card_then_existing_operation();self.data['operations'].pop()
        with self.assertRaises(RefundError):await self.ready_existing()
        self.data['operations'].append(self.op);self.data['order']['merchantId']='another-shop'
        with self.assertRaises(RefundError):await self.ready_existing()
        self.api.refund.assert_not_awaited()

    async def test_pending_external_refund_failure_updates_existing_support_photo(self):
        await self.cached_card_then_existing_operation();ready=await self.ready_existing()
        await approve_completed(self.service,ready['id'],ready['revision'],OWNER)
        ui,context,_=self.ui();await ui.reconcile_pending(context)
        self.op['status']='FAIL';await ui.reconcile_pending(context)
        self.assertEqual(self.store.get(ready['id'])['state'],'failed')
        context.bot.send_photo.assert_awaited_once();context.bot.edit_message_media.assert_awaited_once()
        self.assertIn('не выполнил',context.bot.edit_message_media.await_args.kwargs['media'].caption)

    async def test_document_change_after_approval_prevents_publication(self):
        await self.cached_card_then_existing_operation();ready=await self.ready_existing()
        await approve_completed(self.service,ready['id'],ready['revision'],OWNER)
        self.positions[0]['quantity']=2
        ui,context,_=self.ui();await ui.reconcile_pending(context)
        context.bot.send_photo.assert_not_awaited()
        self.assertFalse(self.store.get(ready['id'])['ms_synced'])

    async def test_deleted_support_request_still_receives_existing_refund_document_in_same_topic(self):
        from telegram.error import BadRequest
        await self.cached_card_then_existing_operation();ready=await self.ready_existing()
        await approve_completed(self.service,ready['id'],ready['revision'],OWNER)
        ui,context,_=self.ui()
        context.bot.send_photo.side_effect=[BadRequest('Message to be replied not found'),SimpleNamespace(message_id=89)]
        await ui.reconcile_pending(context)
        args=context.bot.send_photo.await_args.kwargs
        self.assertEqual((args['chat_id'],args['message_thread_id']),(SUPPORT_CHAT,SUPPORT_TOPIC))
        self.assertNotIn('reply_to_message_id',args)
        self.assertEqual(self.store.get(ready['id'])['receipt_support_message_id'],89)
