import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock,patch

from telegram.error import TelegramError,BadRequest
from telegram.ext import ApplicationHandlerStop

from customer_refunds import RefundError,PaymentLookupError,parse_request,registered_yoo_receipt
from customer_refunds_ui import RefundUI
from refund_receipt_requests import ReceiptRequests,ReceiptRequestStore,parse_receipt_request
from tests.test_customer_refunds import OWNER,SUPPORT_CHAT,SUPPORT_TOPIC,NUMBER,TEXT,PID,payment,result,receipts,refund_receipt,order,positions


class ReceiptRequestTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for name,value in (("customer_refunds.OWNER",OWNER),("customer_refunds.SHOP_ID","382526"),
                ("customer_refunds.SUPPORT_CHAT",SUPPORT_CHAT),("customer_refunds.SUPPORT_TOPIC",SUPPORT_TOPIC),
                ("customer_refunds_ui.OWNER",OWNER),("customer_refunds_ui.SUPPORT_CHAT",SUPPORT_CHAT),("customer_refunds_ui.SUPPORT_TOPIC",SUPPORT_TOPIC)):
            item=patch(name,value);item.start();self.addCleanup(item.stop)
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name)
        self.ui=RefundUI(self.base/'refunds.sqlite3',self.base,'fake',lambda _:False)
        self.handler=self.ui.receipt_requests;self.store=self.ui.store
        self.context=SimpleNamespace(bot=SimpleNamespace(send_photo=AsyncMock(return_value=SimpleNamespace(message_id=801)),
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=802)),edit_message_media=AsyncMock()),
            application=SimpleNamespace(create_task=Mock()),user_data={})
        self.service=self.ui.service
        self.service.payment=AsyncMock(return_value=payment())
        self.service.yoo.pages=AsyncMock(return_value=[])
        self.service.yoo.call=AsyncMock(side_effect=AssertionError('No financial POST'))
        self.service.split.api.refund=AsyncMock(side_effect=AssertionError('No financial POST'))
        self.text='Нужен чек возврата — '+NUMBER

    def job(self,message=50):
        return self.handler.store.add(SUPPORT_CHAT,message,self.text,100)[0]

    def successful(self,proof=True):
        record,_=self.store.ingest(SUPPORT_CHAT,1,TEXT,100)
        snapshot={'payment_id':PID,'amount_cents':1111200,'payment_amount_cents':1179876,'available_cents':1179876,
            'previous_refunds':[],'order':{'id':'copy','name':'Возврат-'+NUMBER,'state':'Возврат','items':[]},
            'financial_items':[],'receipt_contact':{'email':'buyer@example.test'}}
        record=self.store.prepare(record['id'],record['revision'],snapshot)
        record=self.store.claim(record['id'],record['revision'],OWNER)
        fields={'state':'succeeded','refund_id':'refund-result','refund_status':'succeeded','receipt_status':'pending','ms_synced':1}
        if proof:fields.update(receipt_json=registered_yoo_receipt(refund_receipt(),record),receipt_status='succeeded')
        return self.store.update(record['id'],**fields)

    def test_parser_and_journal_preserve_request_without_inventing_a_refund_scope(self):
        for text in (self.text,'нужен чек возврата: #'+NUMBER,'Нужен чек возврата\nзаказ № '+NUMBER):
            self.assertEqual(parse_receipt_request(text),NUMBER)
            request=parse_request(text)
            self.assertEqual(request['kind'],'receipt_request');self.assertEqual(request['scope'],'')
        for text in ('Прошу вернуть деньги '+NUMBER,'Нужен чек оплаты '+NUMBER,self.text+' и 123456789'):
            self.assertIsNone(parse_receipt_request(text))
        job,created=self.handler.store.add(SUPPORT_CHAT,50,self.text,100)
        self.assertTrue(created)
        duplicate,created=self.handler.store.add(SUPPORT_CHAT,50,self.text,100)
        self.assertFalse(created);self.assertEqual(duplicate['id'],job['id'])
        with self.assertRaises(RefundError):self.handler.store.add(SUPPORT_CHAT,50,self.text.replace(NUMBER,'123456789'),100)
        self.assertEqual(ReceiptRequestStore(self.store.path).get(job['id'])['order_number'],NUMBER)

    async def test_known_success_replies_with_photo_once_and_never_requests_owner_or_money(self):
        self.successful();job=self.job()
        await self.handler.process(job['id'],self.context)
        await ReceiptRequests(self.ui).process(job['id'],self.context)
        self.assertEqual(self.handler.store.get(job['id'])['state'],'sent')
        self.context.bot.send_photo.assert_awaited_once()
        args=self.context.bot.send_photo.await_args.kwargs
        self.assertEqual(args['reply_to_message_id'],50);self.assertEqual(args['message_thread_id'],SUPPORT_TOPIC)
        self.context.bot.send_message.assert_not_awaited()
        self.service.yoo.call.assert_not_awaited();self.service.split.api.refund.assert_not_awaited()

    async def test_receipt_request_and_regular_completion_share_one_reply_for_same_source(self):
        record=self.successful();job=self.job(message=1)
        await self.handler.process(job['id'],self.context)
        current=self.store.get(record['id'])
        self.assertTrue(current['completed_message_id'])
        await self.ui.topic_result(current,self.context)
        self.context.bot.send_photo.assert_awaited_once()
        self.assertEqual(self.handler.store.get(job['id'])['state'],'sent')

    async def test_unperformed_refund_routes_existing_request_to_owner_without_duplicate_cash_journal(self):
        record,_=self.store.ingest(SUPPORT_CHAT,1,TEXT,100)
        job=self.job()
        for _ in range(2):await ReceiptRequests(self.ui).process(job['id'],self.context)
        self.assertEqual(len(self.store.for_order(NUMBER)),1)
        saved=self.handler.store.get(job['id'])
        self.assertEqual(saved['refund_request_id'],record['id'])
        self.assertEqual(saved['state'],'waiting')
        self.assertEqual(self.context.bot.send_message.await_count,2)
        owner=[c for c in self.context.bot.send_message.await_args_list if c.kwargs['chat_id']==OWNER]
        self.assertEqual(len(owner),1)
        self.assertIn('подтверждения суммы',owner[0].kwargs['text'])
        self.context.bot.send_photo.assert_not_awaited()
        self.service.yoo.call.assert_not_awaited()

    async def test_new_receipt_request_requires_explicit_scope_and_new_owner_confirmation(self):
        job=self.job();await self.handler.process(job['id'],self.context)
        record=self.store.for_order(NUMBER)[0]
        self.assertEqual(record['request']['scope'],'');self.assertIsNone(record['attempted_at'])
        self.assertEqual(record['source_text'],self.text)
        actions=[b.callback_data for row in self.ui.keyboard(record).inline_keyboard for b in row]
        self.assertTrue(any(v.endswith(':yookassa') for v in actions))
        record=self.store.choose_method(record,'yookassa',OWNER)
        actions=[b.callback_data for row in self.ui.keyboard(record).inline_keyboard for b in row]
        self.assertTrue(any(v.endswith(':delivery') for v in actions))
        self.service.candidates=AsyncMock(return_value=[order()])
        self.service.preview=AsyncMock(return_value={'payment_id':PID,'order':{'id':'return-copy'},'amount_cents':68676})
        update=SimpleNamespace(effective_message=SimpleNamespace(reply_text=AsyncMock()))
        self.ui.send_card=AsyncMock();self.ui.lookup_result=AsyncMock()
        await self.ui.dispatch(update,self.context,['cr','scope',str(record['id']),str(record['revision']),'delivery'])
        current=self.store.get(record['id'])
        self.assertEqual(current['request']['scope'],'delivery');self.assertEqual(current['state'],'ready')
        self.assertIsNone(current['attempted_at']);self.service.yoo.call.assert_not_awaited()

    async def test_manual_provider_button_checks_receipt_without_a_cash_operation(self):
        job=self.job();await self.handler.process(job['id'],self.context)
        record=self.store.for_order(NUMBER)[0]
        self.ui.send_card=AsyncMock();self.ui.open_card=AsyncMock()
        update=SimpleNamespace(callback_query=SimpleNamespace(data=f"cr:method:{record['id']}:{record['revision']}:yookassa"),effective_message=SimpleNamespace(reply_text=AsyncMock()))
        await self.ui.dispatch_safe(update,self.context)
        saved=self.store.get(record['id'])
        self.assertEqual(saved['request']['method'],'yookassa')
        self.assertEqual(saved['request']['scope'],'')
        self.assertIsNone(saved['attempted_at'])
        self.ui.open_card.assert_awaited_once();self.service.yoo.call.assert_not_awaited()

    async def test_typed_method_is_bound_to_open_card_and_prepares_only_after_explicit_scope(self):
        record,_=self.store.ingest(SUPPORT_CHAT,1,NUMBER+'\nза всё',100)
        update=SimpleNamespace(effective_user=SimpleNamespace(id=OWNER),effective_chat=SimpleNamespace(type='private'),
            effective_message=SimpleNamespace(message_id=123,text='ЮKassa',reply_text=AsyncMock()),
            callback_query=SimpleNamespace(edit_message_text=AsyncMock()))
        await self.ui.open_card(record,update)
        self.assertEqual(self.ui.note_requests[OWNER]['action'],'method')
        self.ui.prepare_incoming=AsyncMock()
        update.callback_query=None
        with self.assertRaises(ApplicationHandlerStop):await self.ui.input(update,self.context)
        self.assertEqual(self.store.get(record['id'])['request']['method'],'yookassa')
        self.ui.prepare_incoming.assert_awaited_once()
        self.assertNotIn(OWNER,self.ui.pending);self.assertIsNone(self.store.get(record['id'])['attempted_at'])

    async def test_automatic_method_resolution_clears_pending_text_prompt_instead_of_looping(self):
        record,_=self.store.ingest(SUPPORT_CHAT,1,NUMBER+'\nза всё',100)
        update=SimpleNamespace(effective_user=SimpleNamespace(id=OWNER),effective_chat=SimpleNamespace(type='private'),
            effective_message=SimpleNamespace(message_id=123,text='ЮKassa',reply_text=AsyncMock()),
            callback_query=SimpleNamespace(edit_message_text=AsyncMock()))
        await self.ui.open_card(record,update)
        self.store.resolve_method(record,'yookassa','Полная оплата')
        self.ui.send_card=AsyncMock();update.callback_query=None
        with self.assertRaises(ApplicationHandlerStop):await self.ui.input(update,self.context)
        self.assertNotIn(OWNER,self.ui.pending);self.assertNotIn(OWNER,self.ui.note_requests)
        self.ui.send_card.assert_awaited_once();self.assertIsNone(self.store.get(record['id'])['attempted_at'])

    async def test_unperformed_request_offers_refund_but_registered_money_waits_for_receipt(self):
        job=self.job();await self.handler.process(job['id'],self.context)
        self.assertEqual(self.handler.store.get(job['id'])['outcome'],'not_refunded')
        record=self.store.for_order(NUMBER)[0];record=self.store.choose_method(record,'yookassa',OWNER)
        labels=[b.text for row in self.ui.keyboard(record).inline_keyboard for b in row]
        self.assertIn('Оформить возврат',labels)
        self.assertIn('возврат ещё не проведён',__import__('customer_refunds_ui').card(self.handler.annotate(record)))
        done=self.successful(proof=False)
        self.service.receipt=AsyncMock(return_value=done)
        second=self.job(message=51);await self.handler.process(second['id'],self.context)
        self.assertEqual(self.handler.store.get(second['id'])['outcome'],'receipt_pending')
        self.assertIn('Деньги возвращены',__import__('customer_refunds_ui').card(self.handler.annotate(record)))
        labels=[b.text for row in self.ui.keyboard(record).inline_keyboard for b in row]
        self.assertNotIn('Оформить возврат',labels)
        self.service.yoo.call.assert_not_awaited()

    async def test_old_receipt_card_links_existing_success_instead_of_asking_method_or_restarting_money(self):
        done=self.successful()
        old,_=self.store.ingest(SUPPORT_CHAT,50,self.text,100)
        self.store.decision(old['id'],old['revision'],OWNER,'rejected','Повторная заявка')
        job=self.job();await self.handler.process(job['id'],self.context)
        old=self.store.get(old['id']);shown=self.handler.annotate(old)
        self.assertEqual(shown['related_refund']['id'],done['id'])
        self.assertIn('уже проведён',__import__('customer_refunds_ui').card(shown))
        actions=[b.callback_data for row in self.ui.keyboard(old).inline_keyboard for b in row]
        self.assertIn(f"cr:receipt:{done['id']}:{done['revision']}",actions)
        self.assertFalse(any(v.startswith(('cr:method:','cr:prepare:','cr:execute:')) for v in actions))

    async def test_registered_receipt_arriving_after_restart_is_delivered_automatically(self):
        record=self.successful(proof=False);job=self.job()
        self.service.receipt=AsyncMock(side_effect=lambda r:self.store.get(r['id']))
        await self.handler.process(job['id'],self.context)
        self.assertEqual(self.handler.store.get(job['id'])['state'],'waiting')
        self.context.bot.send_photo.assert_not_awaited()
        self.store.update(record['id'],receipt_status='succeeded',receipt_json=registered_yoo_receipt(refund_receipt(),record))
        await ReceiptRequests(self.ui).process(job['id'],self.context)
        self.context.bot.send_photo.assert_awaited_once()
        self.assertEqual(self.handler.store.get(job['id'])['state'],'sent')

    async def test_telegram_failure_is_retried_without_redelivering_known_photo(self):
        self.successful();job=self.job()
        self.context.bot.send_photo.side_effect=TelegramError('temporary')
        await self.handler.process(job['id'],self.context)
        self.assertNotEqual(self.handler.store.get(job['id'])['state'],'sent')
        self.context.bot.send_photo.side_effect=None
        await ReceiptRequests(self.ui).process(job['id'],self.context)
        await ReceiptRequests(self.ui).process(job['id'],self.context)
        self.assertEqual(self.context.bot.send_photo.await_count,2)
        self.assertEqual(self.handler.store.get(job['id'])['state'],'sent')

    async def test_deleted_staff_message_falls_back_to_same_topic(self):
        self.successful();job=self.job()
        self.context.bot.send_photo.side_effect=[BadRequest('Replied message not found'),SimpleNamespace(message_id=900)]
        await self.handler.process(job['id'],self.context)
        args=self.context.bot.send_photo.await_args.kwargs
        self.assertEqual(args['chat_id'],SUPPORT_CHAT);self.assertEqual(args['message_thread_id'],SUPPORT_TOPIC)
        self.assertNotIn('reply_to_message_id',args)
        self.assertEqual(self.handler.store.get(job['id'])['state'],'sent')

    async def test_external_manual_yoo_refund_uses_registered_receipt_without_creating_cash_request(self):
        paid=payment();paid['refunded_amount']['value']='11112.00'
        self.service.payment.return_value=paid
        async def pages(path,params):return [result()] if path=='/refunds' else [refund_receipt()]
        self.service.yoo.pages.side_effect=pages
        job=self.job();await self.handler.process(job['id'],self.context)
        self.assertEqual(self.handler.store.get(job['id'])['state'],'sent')
        self.assertEqual(self.store.for_order(NUMBER),[])
        self.context.bot.send_photo.assert_awaited_once();self.context.bot.send_message.assert_not_awaited()
        self.service.yoo.call.assert_not_awaited()

    async def test_multiple_partial_refund_receipts_are_all_replied_and_not_sent_twice(self):
        paid=payment();paid['refunded_amount']['value']='11000.00';self.service.payment.return_value=paid
        history=[result(value='6000.00'),result(value='5000.00')]
        history[0]['id']='refund-one';history[1]['id']='refund-two'
        async def pages(path,params):
            if path=='/refunds':return history
            index=0 if params['refund_id']=='refund-one' else 1
            receipt=refund_receipt();receipt.update(id='receipt-'+str(index),refund_id=history[index]['id'])
            receipt['items'][0]['amount']['value']=history[index]['amount']['value']
            return [receipt]
        self.service.yoo.pages.side_effect=pages
        job=self.job();await self.handler.process(job['id'],self.context)
        await self.handler.process(job['id'],self.context)
        self.assertEqual(self.context.bot.send_photo.await_count,2)
        self.assertEqual(self.handler.store.get(job['id'])['state'],'sent')
        self.service.yoo.call.assert_not_awaited()

    async def test_yandex_pending_request_updates_same_confirmation_after_service_success(self):
        from tests.test_yandex_refunds import provider_data,OID,MERCHANT,operation
        oid=OID.split(':')[0]+':'+NUMBER
        data=provider_data();data['order']['orderId']=oid
        data['operations'].append(operation('stored-operation','PENDING'))
        for op in data['operations']:op['orderId']=oid
        self.service.crm_path=str(self.base/'crm.sqlite3')
        self.service.split.api.order_id=Mock(return_value=oid)
        self.service.split.api.order=AsyncMock(side_effect=lambda _:copy.deepcopy(data))
        job=self.job()
        with patch('database.Database') as db,patch('yandex_refunds.MERCHANT',MERCHANT):
            db.return_value.get_order_by_number.return_value={'payment_method':'Яндекс Сплит','email':'buyer@example.test'}
            await self.handler.process(job['id'],self.context)
            self.assertEqual(self.handler.store.get(job['id'])['state'],'waiting')
            self.context.bot.send_photo.assert_awaited_once()
            self.assertIn('Возврат в процессе',self.context.bot.send_photo.await_args.kwargs['caption'])
            data['operations'][-1]['status']='SUCCESS';data['order']['paymentStatus']='PARTIALLY_REFUNDED'
            await ReceiptRequests(self.ui).process(job['id'],self.context)
        self.assertEqual(self.handler.store.get(job['id'])['state'],'sent')
        self.context.bot.send_photo.assert_awaited_once();self.context.bot.edit_message_media.assert_awaited_once()
        self.service.split.api.refund.assert_not_awaited();self.service.yoo.call.assert_not_awaited()

    async def test_wrong_shop_or_wrong_refund_receipt_is_not_sent(self):
        paid=payment();paid['recipient']['account_id']='another-shop'
        self.service.payment.return_value=paid
        job=self.job();await self.handler.process(job['id'],self.context)
        self.context.bot.send_photo.assert_not_awaited()
        self.assertIsNone(self.store.for_order(NUMBER)[0]['attempted_at'])
        paid=payment();paid['refunded_amount']['value']='11112.00';self.service.payment.return_value=paid
        receipt=refund_receipt();receipt['payment_id']='another-payment'
        self.service.yoo.pages.side_effect=lambda path,params:[result()] if path=='/refunds' else [receipt]
        await self.handler.process(job['id'],self.context)
        self.context.bot.send_photo.assert_not_awaited()

    async def test_support_topic_and_bot_sender_are_checked_before_creating_receipt_job(self):
        tasks=[];self.context.bot.id=444
        self.context.application.create_task=lambda task,**kwargs:tasks.append(task)
        self.handler.process=AsyncMock()
        message=SimpleNamespace(chat=SimpleNamespace(id=SUPPORT_CHAT),message_thread_id=SUPPORT_TOPIC+1,
            from_user=SimpleNamespace(id=100,is_bot=False),message_id=50,text=self.text,caption=None)
        update=SimpleNamespace(effective_message=message)
        await self.ui.external(update,self.context)
        self.assertEqual(self.handler.store.pending(),[])
        message.message_thread_id=SUPPORT_TOPIC;message.from_user.is_bot=True
        with self.assertRaises(ApplicationHandlerStop):await self.ui.external(update,self.context)
        self.assertEqual(self.handler.store.pending(),[])
        message.from_user.is_bot=False
        with self.assertRaises(ApplicationHandlerStop):await self.ui.external(update,self.context)
        for task in tasks:await task
        self.handler.process.assert_awaited_once()
