import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from customer_refunds import RefundService, RefundStore, RefundError, CompletedMismatch, completed_chargeback_available, parse_request
from customer_refunds_ui import RefundUI, card
from completed_refunds import approve_completed, mark_completed_chargeback
from tests.test_customer_refunds import OWNER, SUPPORT_CHAT, SUPPORT_TOPIC, TEXT, NUMBER, PID, order, positions, payment, receipts, result, refund_receipt


class CompletedRefundTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for target,value in (("customer_refunds.OWNER",OWNER),("customer_refunds.SHOP_ID","382526"),
                ("completed_refunds.OWNER",OWNER),("completed_refunds.SHOP_ID","382526"),
                ("customer_refunds_ui.OWNER",OWNER),("customer_refunds_ui.SUPPORT_CHAT",SUPPORT_CHAT),("customer_refunds_ui.SUPPORT_TOPIC",SUPPORT_TOPIC)):
            p=patch(target,value);p.start();self.addCleanup(p.stop)
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name)
        self.service=RefundService(self.base/'refunds.sqlite3',self.base,'fake')
        self.store=self.service.store
        self.record,_=self.store.ingest(SUPPORT_CHAT,1,TEXT.replace('за вещь','за всё'),100)
        self.doc=order(True,state='Возврат-проведен');self.rows=positions(True)
        self.service.document=AsyncMock(side_effect=lambda _: (copy.deepcopy(self.doc),copy.deepcopy(self.rows)))
        self.service.check_brand=Mock()
        self.service.customer=AsyncMock(return_value='same-customer')
        self.service.customer_details=AsyncMock(return_value={'customer':{'name':'Test','email':'buyer@example.test'},'receipt_contact':{'email':'buyer@example.test'},'warnings':[]})
        self.paid=payment();self.paid['refunded_amount']['value']='11798.76'
        self.service.payment=AsyncMock(side_effect=lambda *_:copy.deepcopy(self.paid))
        self.refund=result(value='11798.76');self.refund['created_at']='2026-10-20T11:00:00Z'
        self.history=[self.refund]
        self.registered=refund_receipt();self.registered['items']=receipts()[0]['items']
        self.provider_receipts=[self.registered]
        async def pages(path,params):
            if path=='/refunds':return copy.deepcopy(self.history)
            return copy.deepcopy(self.provider_receipts if 'refund_id' in params else receipts())
        self.service.yoo.pages=AsyncMock(side_effect=pages)
        self.service.yoo.get=AsyncMock(side_effect=lambda _:copy.deepcopy(self.refund))
        self.service.yoo.call=AsyncMock(side_effect=AssertionError('No money writes'))
        self.service.sync_ms=AsyncMock(side_effect=AssertionError('Preserve completed MS document'))
        self.service.ms=AsyncMock(side_effect=AssertionError('No MS writes'))

    async def ready(self):
        snapshot=await self.service.preview(self.record,self.doc['id'])
        return self.store.prepare(self.record['id'],self.record['revision'],snapshot)

    async def mismatch(self):
        self.history=[];self.paid['refunded_amount']['value']='0.00'
        with self.assertRaises(CompletedMismatch) as failure:
            await self.service.preview(self.record,self.doc['id'])
        self.store.preparation_error(self.record['id'],self.record['revision'],failure.exception)
        return self.store.get(self.record['id'])

    def ui(self):
        ui=RefundUI(self.store.path,self.base,'fake',lambda t:False)
        ui.service=self.service
        context=SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=88)),
            edit_message_text=AsyncMock(),send_photo=AsyncMock(return_value=SimpleNamespace(message_id=89))))
        update=SimpleNamespace(callback_query=SimpleNamespace(edit_message_text=AsyncMock()),
            effective_message=SimpleNamespace(message_id=99,reply_text=AsyncMock()))
        return ui,context,update

    async def test_candidates_include_completed_documents_but_not_paid_or_foreign_documents(self):
        foreign=order(state='Возврат-проведен');foreign['id']='foreign'
        self.service.check_brand.side_effect=lambda d: (_ for _ in ()).throw(RefundError('foreign')) if d['id']=='foreign' else None
        self.service.ms=AsyncMock(return_value={'rows':[self.doc,order(state='Оплачен'),foreign]})
        docs=await self.service.candidates(self.record)
        self.assertEqual([d['id'] for d in docs],[self.doc['id']])

    async def test_goods_request_can_publish_actual_single_full_receipt_with_delivery_without_new_money(self):
        self.record,_=self.store.ingest(SUPPORT_CHAT,7,TEXT,100)
        snapshot=await self.service.preview(self.record,self.doc['id'])
        self.assertEqual(snapshot['amount_cents'],1179876)
        self.assertEqual(snapshot['existing_refund']['status'],'succeeded')
        self.assertEqual(len(snapshot['financial_items']),2)
        self.assertTrue(any('включая доставку' in warning for warning in snapshot['customer_warnings']))
        self.assertIn('selected_items',snapshot)
        self.service.yoo.call.assert_not_awaited()
        self.service.ms.assert_not_awaited()

    async def test_completed_refund_requires_owner_publication_approval_and_cannot_execute_money(self):
        ready=await self.ready()
        self.assertEqual(ready['state'],'ready');self.assertIsNone(ready['attempted_at'])
        self.assertEqual(ready['snapshot']['existing_refund']['id'],self.refund['id'])
        self.assertIn('Возврат уже проведён',card(ready))
        with self.assertRaises(RefundError):await self.service.execute(ready['id'],ready['revision'],OWNER)
        with self.assertRaises(RefundError):self.store.claim(ready['id'],ready['revision'],OWNER)
        with self.assertRaises(RefundError):await approve_completed(self.service,ready['id'],ready['revision'],999)
        self.service.yoo.call.assert_not_awaited();self.service.sync_ms.assert_not_awaited()

    async def test_approval_sends_actual_receipt_once_without_rewriting_ms_date(self):
        self.doc['attributes']=[{'name':'Дата проведения возврата средств','value':'2026-10-20 14:01:00'}]
        ready=await self.ready();ui,context,update=self.ui()
        await ui.dispatch(update,context,['cr','confirm',str(ready['id']),str(ready['revision'])])
        yes=update.callback_query.edit_message_text.await_args.kwargs['reply_markup'].inline_keyboard[0][0]
        self.assertEqual(yes.text,'Да, отправить чек сотрудникам');self.assertIn(':publish:',yes.callback_data)
        context.bot.send_photo.assert_not_awaited()
        await ui.dispatch(update,context,yes.callback_data.split(':'))
        saved=self.store.get(ready['id'])
        self.assertEqual(saved['state'],'succeeded');self.assertEqual(saved['approved_by'],OWNER)
        self.assertIsNone(saved['idempotence_key']);self.assertTrue(saved['receipt_json'])
        context.bot.send_photo.assert_not_awaited() # Worker publishes, not the private button.
        await ui.reconcile_pending(context);await ui.reconcile_pending(context)
        context.bot.send_photo.assert_awaited_once()
        args=context.bot.send_photo.await_args.kwargs
        self.assertEqual(args['chat_id'],SUPPORT_CHAT);self.assertIn('20.10.2026 14:01',args['caption'])
        self.service.yoo.call.assert_not_awaited();self.service.sync_ms.assert_not_awaited();self.service.ms.assert_not_awaited()
        await ui.dispatch(update,context,yes.callback_data.split(':'))
        context.bot.send_photo.assert_awaited_once()

    async def test_full_refund_waits_for_real_receipt_and_recovers_after_restart(self):
        self.provider_receipts=[]
        ready=await self.ready();saved=await approve_completed(self.service,ready['id'],ready['revision'],OWNER)
        ui,context,_=self.ui()
        await ui.reconcile_pending(context)
        context.bot.send_photo.assert_not_awaited()
        self.assertIsNone(self.store.get(saved['id'])['receipt_json'])
        self.provider_receipts=[self.registered]
        restarted,_,_=self.ui()
        await restarted.reconcile_pending(context)
        self.assertTrue(self.store.get(saved['id'])['completed_message_id'])
        self.assertNotIn('Дата:',context.bot.send_photo.await_args.kwargs['caption'])
        self.service.yoo.call.assert_not_awaited();self.service.ms.assert_not_awaited()

    async def test_wrong_items_or_payment_are_never_published_and_no_refund_is_created(self):
        self.registered['items'][0]['description']='DIFFERENT HOODIE Размер=XS'
        with self.assertRaises(RefundError):await self.ready()
        self.registered['items']=receipts()[0]['items']
        self.paid['recipient']['account_id']='other-shop'
        with self.assertRaises(RefundError):await self.ready()
        self.service.yoo.call.assert_not_awaited()

    async def test_completed_document_change_after_approval_prevents_publication(self):
        ready=await self.ready();await approve_completed(self.service,ready['id'],ready['revision'],OWNER)
        self.rows[0]['quantity']=2
        ui,context,_=self.ui();await ui.reconcile_pending(context)
        context.bot.send_photo.assert_not_awaited()
        self.assertFalse(self.store.get(ready['id'])['ms_synced'])
        self.service.ms.assert_not_awaited();self.service.yoo.call.assert_not_awaited()

    async def test_mismatch_is_durable_visible_to_owner_and_not_automatically_a_chargeback(self):
        record=await self.mismatch()
        restarted=RefundStore(self.store.path).get(record['id'])
        self.assertTrue(completed_chargeback_available(restarted))
        self.assertIn('в ЮKassa возврата нет',card(restarted))
        self.assertIsNone(restarted['chargeback_at']);self.assertIsNone(restarted['attempted_at'])
        self.assertEqual(self.store.pending_decision_notices(),[])
        ui,_,_=self.ui()
        self.assertIn('Чарджбек',[b.text for row in ui.keyboard(restarted).inline_keyboard for b in row])
        self.service.yoo.call.assert_not_awaited()

    async def test_mismatch_is_detected_even_when_ms_has_more_items_than_requested_scope(self):
        self.record,_=self.store.ingest(SUPPORT_CHAT,1,TEXT,100) # Goods requested; MS also has delivery.
        record=await self.mismatch()
        self.assertTrue(completed_chargeback_available(record))
        self.assertEqual(len(record['completed_review']['items']),2)

    async def test_chargeback_checks_again_then_notifies_staff_once_and_blocks_future_money(self):
        record=await self.mismatch();ui,context,update=self.ui()
        self.store.show_owner_card(record['id'],99)
        with self.assertRaises(RefundError):await mark_completed_chargeback(self.service,record,999)
        bits=['cr','chargeback',str(record['id']),str(record['revision'])]
        await ui.dispatch(update,context,bits);await ui.dispatch(update,context,bits)
        context.bot.send_message.assert_awaited_once()
        text=context.bot.send_message.await_args.kwargs['text']
        self.assertIn('в ЮKassa возврата нет',text);self.assertIn('Клиент обратился в банк',text)
        saved=self.store.get(record['id'])
        self.assertTrue(saved['chargeback_at']);self.assertIsNone(saved['attempted_at']);self.assertIsNone(saved['succeeded_at'])
        self.assertFalse(completed_chargeback_available(saved))
        self.assertNotIn('Провести возврат',[b.text for row in ui.keyboard(saved).inline_keyboard for b in row])
        _,changed=self.store.ingest(SUPPORT_CHAT,1,TEXT+'\nновые сведения',100)
        self.assertFalse(changed)
        with self.assertRaises(RefundError):await self.service.preview(saved,self.doc['id'])
        self.service.yoo.call.assert_not_awaited();self.service.ms.assert_not_awaited()

    async def test_chargeback_prevents_another_request_from_refunding_same_payment(self):
        record=await self.mismatch()
        await mark_completed_chargeback(self.service,record,OWNER)
        other,_=self.store.ingest(SUPPORT_CHAT,2,TEXT,100)
        snapshot={'payment_id':PID,'order':{'id':'another-copy'},'amount_cents':10000,'previous_refunds':[]}
        other=self.store.prepare(other['id'],other['revision'],snapshot)
        with self.assertRaisesRegex(RefundError,'чарджбек'):self.store.claim(other['id'],other['revision'],OWNER)
        self.assertIsNone(self.store.get(other['id'])['attempted_at'])

    async def test_pending_provider_refund_never_offers_no_refund_chargeback(self):
        self.history[0]['status']='pending';self.paid['refunded_amount']['value']='0.00'
        with self.assertRaises(RefundError) as failure:await self.ready()
        self.assertNotIsInstance(failure.exception,CompletedMismatch)
        self.assertIn('обрабатывает',str(failure.exception))

    async def test_pending_operation_on_recheck_removes_the_old_chargeback_option(self):
        record=await self.mismatch()
        self.history=[{**self.refund,'status':'pending'}]
        with self.assertRaises(RefundError):await mark_completed_chargeback(self.service,record,OWNER)
        saved=self.store.get(record['id'])
        self.assertIsNone(saved['completed_review']);self.assertIsNone(saved['chargeback_at'])
        self.assertFalse(completed_chargeback_available(saved))
        self.assertEqual(self.store.pending_decision_notices(),[])

    async def test_chargeback_notice_survives_a_telegram_failure_and_restart(self):
        from telegram.error import TelegramError
        record=await self.mismatch()
        await mark_completed_chargeback(self.service,record,OWNER)
        ui,context,_=self.ui()
        context.bot.send_message.side_effect=TelegramError('offline')
        await ui.send_decision_notices(context)
        self.assertEqual(len(self.store.pending_decision_notices()),1)
        context.bot.send_message.side_effect=None
        restarted,_,_=self.ui();await restarted.send_decision_notices(context)
        self.assertEqual(self.store.pending_decision_notices(),[])

    async def test_changed_completed_document_requires_fresh_publication_approval(self):
        ready=await self.ready();self.doc['updated']='2026-10-21 14:00:00.000'
        with self.assertRaises(RefundError):await approve_completed(self.service,ready['id'],ready['revision'],OWNER)
        saved=self.store.get(ready['id'])
        self.assertEqual(saved['state'],'ready');self.assertIsNone(saved['approved_by'])
        self.assertGreater(saved['revision'],ready['revision'])
        self.service.yoo.call.assert_not_awaited()

    async def test_one_existing_refund_cannot_be_imported_into_two_requests(self):
        ready=await self.ready();await approve_completed(self.service,ready['id'],ready['revision'],OWNER)
        other,_=self.store.ingest(SUPPORT_CHAT,2,TEXT.replace('за вещь','за всё'),100)
        snapshot=await self.service.preview(other,self.doc['id'])
        other=self.store.prepare(other['id'],other['revision'],snapshot)
        with self.assertRaisesRegex(RefundError,'уже учтён'):await approve_completed(self.service,other['id'],other['revision'],OWNER)
        self.assertIsNone(self.store.get(other['id'])['attempted_at'])

    async def test_receipt_with_wrong_items_arriving_later_is_not_sent(self):
        self.provider_receipts=[]
        ready=await self.ready();await approve_completed(self.service,ready['id'],ready['revision'],OWNER)
        self.registered['items'][0]['description']='OTHER ITEM Размер=XS'
        self.provider_receipts=[self.registered]
        ui,context,_=self.ui();await ui.reconcile_pending(context)
        context.bot.send_photo.assert_not_awaited()
        self.assertIsNone(self.store.get(ready['id'])['receipt_json'])

    async def test_refund_arriving_before_chargeback_click_blocks_false_notification(self):
        record=await self.mismatch()
        self.history=[self.refund];self.paid['refunded_amount']['value']='11798.76'
        with self.assertRaises(RefundError):await mark_completed_chargeback(self.service,record,OWNER)
        self.assertIsNone(self.store.get(record['id'])['chargeback_at'])
        self.assertEqual(self.store.pending_decision_notices(),[])
        self.assertTrue(self.store.get(record['id'])['snapshot']['existing_refund'])

    async def test_changed_source_clears_old_mismatch_and_disallows_old_chargeback(self):
        record=await self.mismatch()
        edited,_=self.store.ingest(SUPPORT_CHAT,1,TEXT+'\nуточнение',100)
        self.assertIsNone(edited['completed_review'])
        with self.assertRaises(RefundError):self.store.chargeback(record['id'],record['revision'],OWNER)

    async def test_completed_state_does_not_become_an_ordinary_refund_document(self):
        from customer_refunds import document_items
        with self.assertRaises(RefundError):document_items(self.doc,self.rows,'all')
        self.assertEqual(parse_request(TEXT+'\nrefund_id: refund-result')['refund_id'],'refund-result')
