import copy
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from PIL import Image

from customer_refunds import OWNER, SUPPORT_CHAT, RefundError, RefundService, UncertainRefund, GiftClarification, parse_request
from customer_refunds_ui import RefundUI, card
from refund_receipt_image import render_yandex_confirmation
from yandex_refunds import MERCHANT, YandexPay, YandexAPIError, cart_items, refund_body, same_item

OWNER, SUPPORT_CHAT, SUPPORT_TOPIC = 12345, -1009999999999, 123
MERCHANT = "00000000-0000-4000-8000-000000000001"
_PATCHES = []
def setUpModule():
    for target,value in (("customer_refunds.OWNER",OWNER),("customer_refunds.SUPPORT_CHAT",SUPPORT_CHAT),("customer_refunds.SUPPORT_TOPIC",SUPPORT_TOPIC),("customer_refunds.SHOP_ID","382526"),("customer_refunds_ui.OWNER",OWNER),("customer_refunds_ui.SUPPORT_CHAT",SUPPORT_CHAT),("customer_refunds_ui.SUPPORT_TOPIC",SUPPORT_TOPIC),("yandex_refunds.OWNER",OWNER),("yandex_refunds.MERCHANT",MERCHANT)):
        item=patch(target,value);item.start();_PATCHES.append(item)
def tearDownModule():
    for item in reversed(_PATCHES):item.stop()

def enable_gate(base,provider):
    from refund_setup import gate_fingerprint
    directory=base/".secrets";directory.mkdir(exist_ok=True)
    for name in ("refunds_config.json","moysklad.json","yookassa.json" if provider=="yookassa" else "yandex_pay.json"):
        if not (directory/name).exists():(directory/name).write_text("{}")
    (directory/(provider+"_refunds_enabled")).write_text(json.dumps({"fingerprint":gate_fingerprint(base,provider)}))

NUMBER = "1010101005"
OID = "4299052:" + NUMBER
AUTH = "11111111-1111-4111-8111-000000000004"
OP = "11111111-1111-4111-8111-000000000006"
TEXT = NUMBER + "\nname: Тестовый Покупатель\nemail: buyer@example.test\nphone: +79990000000\nСплит, за вещь"


def item(pid="0", title="DIAMOND CAMO JEANS KHAKI, Размер=XS -20%", amount="10392.00", count="1"):
    return {"productId": pid, "title": title, "total": amount, "finalPrice": amount,
            "quantity": {"count": count}, "receipt": {"tax": 7, "paymentMethodType": 1, "paymentSubjectType": 1, "title": title}}


def provider_data():
    return {"order": {"merchantId": MERCHANT, "orderId": OID, "currencyCode": "RUB", "paymentStatus": "CAPTURED",
                       "paymentMethod": {"methodType": "SPLIT"}, "orderAmount": "11073.09", "fiscalContact": "buyer@example.test",
                       "created": "2026-10-10T20:59:02+03:00", "shippingMethod": None,
                       "cart": {"items": [item(), item("1", "СДЭК", "681.09")], "total": {"amount": "11073.09"}}},
            "operations": [{"operationId": AUTH, "operationType": "AUTHORIZE", "orderId": OID, "amount": "11073.09", "status": "SUCCESS"}], "delivery": None}


def position(shipping=False):
    return {"id": "pos-shipping" if shipping else "pos-goods", "quantity": 1, "price": 68109 if shipping else 1039200,
            "discount": 0, "assortment": {"name": "СДЭК" if shipping else "DIAMOND CAMO JEANS KHAKI (XS, хаки, Хлопок 100%, test-sku)"}}


def document():
    return {"id": "prepared-return", "name": "Возврат-" + NUMBER, "state": {"name": "Возврат"},
            "sum": 1039200, "updated": "2026-10-20 16:00:00.000"}


def operation(external_id, status="PENDING", amount="10392.00"):
    return {"operationId": OP, "externalOperationId": external_id, "operationType": "REFUND", "orderId": OID,
            "amount": amount, "status": status, "created": "2026-10-20T16:02:00+03:00", "updated": "2026-10-20T16:03:00+03:00"}


class YandexCartTests(unittest.TestCase):
    def test_customer_email_and_name_cannot_choose_the_payment_provider(self):
        text = TEXT.replace("buyer@example.test", "split@example.test").replace("Тестовый Покупатель", "Yandex Pay").replace("Сплит, за вещь", "Полная, за вещь")
        self.assertEqual(parse_request(text)["method"], "yookassa")
        self.assertEqual(parse_request(TEXT.replace("buyer@example.test", "yookassa@example.test"))["method"], "yandex_split")

    def test_method_and_explicit_yandex_id_are_not_routed_to_yookassa(self):
        for name in ("Сплит", "Яндекс Сплит", "Yandex Pay", "Яндекс Пэй", "Split", "Яндекс СБП", "Yandex SBP"):
            self.assertEqual(parse_request(TEXT.replace("Сплит", name))["method"], "yandex_split")
        self.assertEqual(parse_request(TEXT + "\nyandex_order_id: " + OID)["yandex_order_id"], OID)

    def test_item_matching_checks_full_model_color_and_size(self):
        name = position()["assortment"]["name"]
        self.assertTrue(same_item(name, item()["title"]))
        for title in ("AIR PUFFER PINK, Размер=S", "DIAMOND CAMO JEANS KHAKI, Размер=S", "DIAMOND CAMO JEANS WHITE, Размер=XS", "DIAMOND CAMO JEANS KHAKI"):
            self.assertFalse(same_item(name, title))

    def test_goods_shipping_and_full_refunds_have_exact_original_product_ids(self):
        goods = {"name": position()["assortment"]["name"], "quantity": 1, "unit_cents": 1039200}
        shipping = {"name": "СДЭК", "quantity": 1, "unit_cents": 68109}
        order = provider_data()["order"]
        self.assertEqual(refund_body(order, [goods]), {"refundAmount": "10392.00", "refundCart": {"items": [{"productId": "0", "quantityCount": "1"}]}})
        self.assertEqual(refund_body(order, [shipping])["refundCart"]["items"][0]["productId"], "1")
        self.assertEqual(refund_body(order, [goods, shipping]), {"refundAmount": "11073.09"})

    def test_quantity_prices_after_discounts_and_partial_item_returns(self):
        order = provider_data()["order"]
        order["cart"]["items"][0].update(quantity={"count": "2"}, total="20784.00")
        order["cart"]["total"]["amount"] = order["orderAmount"] = "21465.09"
        goods = {"name": position()["assortment"]["name"], "quantity": 1, "unit_cents": 1039200}
        self.assertEqual(cart_items(order)[0]["unit_cents"], 1039200)
        self.assertEqual(refund_body(order, [goods])["refundCart"]["items"][0]["quantityCount"], "1")
        with self.assertRaises(RefundError): refund_body(order, [{**goods, "quantity": 3}])

    def test_wrong_totals_points_and_checkout_are_blocked(self):
        mutations = [lambda o: o.update(orderAmount="11073.08"), lambda o: o["cart"]["items"][0].update(finalPrice="10391.99"),
                     lambda o: o["cart"]["total"].update(externalAmount="500"),
                     lambda o: o["cart"]["total"].update(pointsAmount="500"), lambda o: o.update(shippingMethod={"amount": "500"}),
                     lambda o: o["cart"]["items"][1].update(productId="0")]
        for mutate in mutations:
            o = provider_data()["order"]; mutate(o)
            with self.assertRaises(RefundError): cart_items(o)


class YandexServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        secret = self.base / ".secrets"; secret.mkdir()
        (secret / "yandex_pay.json").write_text(json.dumps({"merchant_id": MERCHANT, "environment": "production", "api_key": "only-fake-test-key", "sample_order_id": OID, "fiscalization_verified": True}))
        enable_gate(self.base,"yandex_pay")
        self.service = RefundService(self.base / "test.sqlite3", self.base, "fake-ms")
        self.service.customer = AsyncMock(return_value="customer-fixture")
        self.store = self.service.store
        self.record, _ = self.store.ingest(SUPPORT_CHAT, 10, TEXT, 100)
        self.doc, self.positions, self.data = document(), [position()], provider_data()
        self.service.document = AsyncMock(side_effect=lambda oid: (copy.deepcopy(self.doc), copy.deepcopy(self.positions)))
        self.service.candidates = AsyncMock(return_value=[self.doc])
        self.service.sync_ms = AsyncMock(side_effect=lambda r: self.store.update(r["id"], ms_synced=1))
        self.service.book_pending_split = AsyncMock(side_effect=lambda r: self.store.update(r["id"], ms_synced=1,split_booked_at=r["attempted_at"]))
        self.api = self.service.split.api
        self.api.order = AsyncMock(side_effect=lambda oid: copy.deepcopy(self.data))
        self.api.refund = AsyncMock(side_effect=self.respond)
        self.api.operation = AsyncMock(side_effect=self.get_operation)
        self.service.yoo.call = AsyncMock(side_effect=AssertionError("Split must not call YooKassa"))
        self.service.yoo.get = AsyncMock(side_effect=AssertionError("Split must not call YooKassa"))
        self.service.yoo.pages = AsyncMock(side_effect=AssertionError("Split must not call YooKassa"))
        self.next_status = "SUCCESS"
        self.last_operation = None

    def prepayment_submit_history(self):
        base=copy.deepcopy(self.data['operations'][0])
        prepay={**base,'operationType':'PREPAYMENT','created':'2026-10-10T13:00:00+03:00'}
        submit={**base,'operationId':str(uuid.uuid4()),'operationType':'SUBMIT','created':'2026-10-10T13:01:00+03:00'}
        self.data['operations']=[prepay,submit]

    async def test_split_prepayment_submit_without_authorize_uses_one_paid_amount(self):
        self.prepayment_submit_history()
        snapshot=await self.service.preview(self.record,self.doc['id'])
        self.assertEqual(snapshot['payment_amount_cents'],1107309)
        self.assertEqual(snapshot['amount_cents'],1039200)
        self.assertEqual(snapshot['previous_refunds'],[])
        self.api.refund.assert_not_awaited()

    async def test_split_prepayment_submit_still_subtracts_prior_successful_refunds(self):
        self.prepayment_submit_history()
        prior=operation(str(uuid.uuid4()),'SUCCESS','681.09')
        self.data['operations'].append(prior)
        self.data['order']['orderAmount']='10392.00'
        self.data['order']['cart']['total']['amount']='10392.00'
        self.data['order']['cart']['items']=self.data['order']['cart']['items'][:1]
        self.data['order']['paymentStatus']='PARTIALLY_REFUNDED'
        snapshot=await self.service.preview(self.record,self.doc['id'])
        self.assertEqual(snapshot['available_cents'],1039200)
        self.assertEqual(snapshot['payment_amount_cents'],1107309)
        self.assertEqual(snapshot['previous_refunds'],[prior['operationId']])

    async def test_prepayment_alone_conflicting_submit_or_duplicate_submit_are_not_paid_proof(self):
        for variant in ('no_submit','wrong_amount','duplicate'):
            self.prepayment_submit_history()
            if variant=='no_submit':self.data['operations']=self.data['operations'][:1]
            elif variant=='wrong_amount':self.data['operations'][1]['amount']='1.00'
            else:self.data['operations'].append({**self.data['operations'][1],'operationId':str(uuid.uuid4())})
            with self.assertRaises(RefundError):await self.service.preview(self.record,self.doc['id'])
            self.data=provider_data()
        self.api.refund.assert_not_awaited()

    async def test_partial_capture_uses_captured_not_authorized_amount(self):
        capture={**self.data['operations'][0],'operationId':str(uuid.uuid4()),'operationType':'CAPTURE'}
        self.data['operations'][0]['amount']='15000.00'
        self.data['operations'].append(capture)
        snapshot=await self.service.preview(self.record,self.doc['id'])
        self.assertEqual(snapshot['payment_amount_cents'],1107309)

    async def respond(self, oid, body, external):
        self.assertEqual(oid, OID)
        uuid.UUID(external)
        self.last_operation = operation(external, self.next_status, body["refundAmount"])
        self.last_operation["operationId"] = str(uuid.uuid4())
        self.data["operations"].append(copy.deepcopy(self.last_operation))
        return {"operation": copy.deepcopy(self.last_operation)}

    async def get_operation(self, external):
        if self.last_operation is None: raise YandexAPIError(404)
        self.assertEqual(external, self.last_operation["externalOperationId"])
        return {"operation": copy.deepcopy(self.last_operation)}

    async def ready(self):
        snapshot = await self.service.preview(self.record)
        return self.store.prepare(self.record["id"], self.record["revision"], snapshot)

    async def test_preview_is_get_only_and_uses_the_full_paid_order_not_installment(self):
        ready = await self.ready()
        self.assertEqual(ready["amount_cents"], 1039200)
        self.assertEqual(ready["payment_id"], "yandex:" + OID)
        self.assertEqual(ready["snapshot"]["payment_amount_cents"], 1107309)
        self.api.refund.assert_not_awaited(); self.service.sync_ms.assert_not_awaited()
        self.assertIn("Яндекс Сплит", card(ready))
        self.assertNotIn("Платёж ЮKassa", card(ready))

    async def test_owner_only_exact_confirmation_posts_once_and_syncs_after_success(self):
        ready = await self.ready()
        with self.assertRaises(RefundError): await self.service.execute(ready["id"], ready["revision"], OWNER + 1)
        result = await self.service.execute(ready["id"], ready["revision"], OWNER)
        self.assertEqual(result["state"], "succeeded"); self.assertEqual(result["ms_synced"], 1)
        self.assertEqual(result["succeeded_at"], "2026-10-20T16:03:00+03:00")
        self.assertEqual(result["receipt_json"]["provider"], "yandex_split")
        self.assertEqual(result["receipt_json"]["order_created_at"],self.data["order"]["created"])
        self.assertNotIn("fiscal_document_number", result["receipt_json"])
        self.assertNotIn("зарегистрирован", card(result))
        with self.assertRaises(RefundError): await self.service.execute(ready["id"], ready["revision"], OWNER)
        self.api.refund.assert_awaited_once(); self.service.sync_ms.assert_awaited_once()

    async def test_payment_method_change_invalidates_owner_confirmation(self):
        ready = await self.ready()
        for method in ("CARD",):
            self.data["order"]["paymentMethod"] = {"methodType": method}
            with self.assertRaises(RefundError):
                await self.service.execute(ready["id"], ready["revision"], OWNER)
            self.api.refund.assert_not_awaited()
            self.assertIsNone(self.store.get(ready["id"])["attempted_at"])

    async def test_card_refund_requires_owner_and_recovers_timeout_without_second_post(self):
        self.data['order']['paymentMethod']={'methodType':'CARD'}
        self.next_status='PENDING'
        ready=await self.ready()
        self.assertEqual(ready['snapshot']['payment_method'],'CARD')
        self.assertIn('Оплата: Яндекс Пэй · Карта',card(ready))
        self.assertNotIn('Оплата: Яндекс Сплит',card(ready))
        with self.assertRaises(RefundError):await self.service.execute(ready['id'],ready['revision'],OWNER+1)
        self.api.refund.assert_not_awaited()
        async def timeout(oid,body,external):
            await self.respond(oid,body,external)
            raise UncertainRefund('timeout')
        self.api.refund.side_effect=timeout
        with self.assertRaises(UncertainRefund):await self.service.execute(ready['id'],ready['revision'],OWNER)
        pending=await self.service.check(self.store.get(ready['id']))
        self.assertEqual(pending['state'],'pending')
        self.assertTrue(pending['split_booked_at'])
        self.last_operation['status']='SUCCESS';self.data['operations'][-1]['status']='SUCCESS'
        done=await self.service.check(pending)
        self.assertEqual(done['state'],'succeeded')
        self.assertEqual(done['snapshot']['payment_method'],'CARD')
        self.api.refund.assert_awaited_once();self.service.yoo.call.assert_not_awaited()
        with self.assertRaises(RefundError):await self.service.execute(done['id'],done['revision'],OWNER)
        self.api.refund.assert_awaited_once()

    async def test_sbp_refund_timeout_recovers_existing_operation_and_preserves_method(self):
        self.data['order']['paymentMethod']={'methodType':'SBP'}
        self.next_status='PENDING'
        ready=await self.ready()
        self.assertIn('Оплата: Яндекс Пэй · СБП',card(ready))
        self.assertEqual(ready['snapshot']['payment_method'],'SBP')
        async def timeout(oid,body,external):
            await self.respond(oid,body,external)
            raise UncertainRefund('timeout')
        self.api.refund.side_effect=timeout
        with self.assertRaises(UncertainRefund):await self.service.execute(ready['id'],ready['revision'],OWNER)
        pending=await self.service.check(self.store.get(ready['id']))
        self.assertEqual(pending['state'],'pending')
        self.service.split.pending_identity(pending)
        self.assertTrue(pending['split_booked_at'])
        self.last_operation['status']='SUCCESS';self.data['operations'][-1]['status']='SUCCESS'
        done=await self.service.check(pending)
        self.assertEqual(done['state'],'succeeded')
        self.assertEqual(done['snapshot']['payment_method'],'SBP')
        self.api.refund.assert_awaited_once()
        self.service.yoo.call.assert_not_awaited()

    async def test_sbp_still_blocks_another_shop_or_order_and_unsupported_methods(self):
        self.data['order']['paymentMethod']={'methodType':'SBP'}
        original=copy.deepcopy(self.data)
        for key,value in (('merchantId','other'),('orderId','other'),('paymentStatus','REFUNDED'),('paymentMethod',{'methodType':'UNKNOWN'})):
            self.data=copy.deepcopy(original);self.data['order'][key]=value
            with self.assertRaises(RefundError):await self.service.preview(self.record)
        self.api.refund.assert_not_awaited()

    async def test_sbp_without_fiscal_fields_uses_original_cart_ids_for_partial_and_full_refunds(self):
        self.data['order']['paymentMethod']={'methodType':'SBP'}
        for row in self.data['order']['cart']['items']:row['receipt']=None
        partial=await self.service.preview(self.record,'prepared-return')
        self.assertEqual(partial['body']['refundAmount'],'10392.00')
        self.assertEqual(partial['body']['refundCart'],{'items':[{'productId':'0','quantityCount':'1'}]})
        self.assertNotIn('receipt',partial['body'])
        record=copy.deepcopy(self.record);record['request']['scope']='all'
        full=await self.service.preview(record,'prepared-return')
        self.assertEqual(full['body']['refundAmount'],'11073.09')
        self.assertNotIn('refundCart',full['body'])
        self.assertEqual(len(full['financial_items']),2)
        self.data['order']['cart']['items'][0]['title']=''
        with self.assertRaises(RefundError):await self.service.preview(record,'prepared-return')
        self.api.refund.assert_not_awaited()

    async def test_split_missing_fiscal_calculation_fields_uses_v2_refund_without_inventing_them(self):
        for row in self.data['order']['cart']['items']:
            row['receipt'].pop('paymentMethodType');row['receipt'].pop('paymentSubjectType')
        ready=await self.ready()
        done=await self.service.execute(ready['id'],ready['revision'],OWNER)
        self.assertEqual(done['state'],'succeeded')
        body=self.api.refund.await_args.args[1]
        self.assertEqual(body['refundCart']['items'],[{'productId':'0','quantityCount':'1'}])
        self.assertNotIn('receipt',body)
        self.api.refund.assert_awaited_once()

    async def test_unpaid_scarf_requires_owner_gift_choice_for_split_and_sbp(self):
        scarf={'id':'scarf','quantity':1,'price':699000,'discount':0,'assortment':{'name':'DEMO SCARF GREY (ONESIZE, FW26-27-LSG-O)'}}
        self.positions.append(scarf)
        with self.assertRaises(GiftClarification) as question:await self.service.preview(self.record)
        self.store.preparation_error(self.record['id'],self.record['revision'],question.exception)
        current=self.store.get(self.record['id'])
        current=self.store.choose_gift(current['id'],current['revision'],OWNER,0,'gift')
        for method in ('SPLIT','SBP'):
            self.data['order']['paymentMethod']={'methodType':method}
            snapshot=await self.service.preview(current)
            self.assertEqual(snapshot['amount_cents'],1039200)
            self.assertEqual(len(snapshot['excluded_gifts']),1)
            self.assertEqual(snapshot['body']['refundCart']['items'],[{'productId':'0','quantityCount':'1'}])
        self.api.refund.assert_not_awaited()

    async def test_zero_price_scarf_gift_is_not_added_to_full_return_amount(self):
        self.positions.append({'id':'scarf','quantity':1,'price':699000,'assortment':{'name':'DEMO SCARF GREY (ONESIZE, grey)'}})
        self.data['order']['cart']['items'].append(item('gift','DEMO SCARF GREY','0.00'))
        record=copy.deepcopy(self.record);record['request']['scope']='all'
        with self.assertRaises(GiftClarification) as question:await self.service.preview(record)
        record['gift_review']=question.exception.review
        record['gift_review']['items'][0]['decision']='gift'
        snapshot=await self.service.preview(record)
        self.assertEqual(snapshot['amount_cents'],1107309)
        self.assertEqual(snapshot['body']['refundAmount'],'11073.09')
        self.assertFalse(any('SCARF' in i['name'] for i in snapshot['financial_items']))
        self.api.refund.assert_not_awaited()

    async def test_gate_and_fiscalization_are_required_independently_of_yookassa(self):
        ready = await self.ready()
        path = self.base / ".secrets/yandex_pay_refunds_enabled"; path.unlink()
        with self.assertRaises(RefundError): await self.service.execute(ready["id"], ready["revision"], OWNER)
        path.write_text("yes")
        file = self.base / ".secrets/yandex_pay.json"; data = json.loads(file.read_text()); data["fiscalization_verified"] = False; file.write_text(json.dumps(data))
        with self.assertRaises(RefundError): await self.service.execute(ready["id"], ready["revision"], OWNER)
        self.api.refund.assert_not_awaited()
        self.assertIsNone(self.store.get(ready["id"])["attempted_at"])

    async def test_pending_is_booked_but_not_marked_as_financial_success_and_fail_is_not_booked(self):
        for status, state in (("PENDING", "pending"), ("FAIL", "failed")):
            self.service.book_pending_split.reset_mock()
            self.next_status = status
            ready = await self.ready()
            result = await self.service.execute(ready["id"], ready["revision"], OWNER)
            self.assertEqual(result["state"], state)
            self.service.sync_ms.assert_not_awaited()
            if status == "PENDING":
                self.service.book_pending_split.assert_awaited_once()
                self.assertIsNone(result['succeeded_at'])
                self.assertEqual(result['receipt_json']['status'],'PENDING')
                self.assertTrue(result['split_booked_at'])
                self.data["operations"][-1]["status"] = "FAIL"
                self.last_operation["status"] = "FAIL"
                await self.service.check(result)
            else:self.service.book_pending_split.assert_not_awaited()
            self.record, _ = self.store.ingest(SUPPORT_CHAT, 11, TEXT, 100)

    async def test_timeout_restarts_reconcile_by_stable_external_id_without_second_post(self):
        async def timeout(oid, body, external):
            await self.respond(oid, body, external)
            raise UncertainRefund("timeout")
        self.api.refund.side_effect = timeout
        ready = await self.ready()
        with self.assertRaises(UncertainRefund): await self.service.execute(ready["id"], ready["revision"], OWNER)
        saved = self.store.get(ready["id"])
        self.assertEqual(saved["state"], "uncertain")
        result = await self.service.check(saved)
        self.assertEqual(result["state"], "succeeded")
        await self.service.check(result)
        self.api.refund.assert_awaited_once()

    async def pending(self):
        self.next_status='PENDING'
        ready=await self.ready()
        return await self.service.execute(ready['id'],ready['revision'],OWNER)

    async def test_split_uses_paid_prices_and_full_scope_adds_missing_delivery(self):
        self.positions[0]['price']+=200000
        goods=await self.service.preview(self.record,'prepared-return')
        self.assertEqual(goods['amount_cents'],1039200)
        self.assertEqual(goods['financial_items'][0]['unit_cents'],1039200)
        self.assertEqual(goods['order']['items'][0]['unit_cents'],1239200)
        record,_=self.store.ingest(SUPPORT_CHAT,22,TEXT.replace('за вещь','за всё'),100)
        full=await self.service.preview(record,'prepared-return')
        self.assertEqual(full['amount_cents'],1107309)
        self.assertEqual(full['body']['refundAmount'],'11073.09')
        self.assertNotIn('refundCart',full['body'])
        self.assertEqual(len(full['financial_items']),2)
        self.api.refund.assert_not_awaited()

    async def test_split_delivery_uses_service_price_even_when_ms_has_another_price(self):
        self.positions[:]=[position(shipping=True)]
        self.positions[0]['price']=200000
        record,_=self.store.ingest(SUPPORT_CHAT,22,TEXT.replace('за вещь','за доставку'),100)
        snapshot=await self.service.preview(record,'prepared-return')
        self.assertEqual(snapshot['amount_cents'],68109)
        self.assertEqual(snapshot['body']['refundCart']['items'],[{'productId':'1','quantityCount':'1'}])
        self.api.refund.assert_not_awaited()

    async def test_named_item_from_full_split_or_sbp_order_returns_one_unit_without_other_goods_or_delivery(self):
        for method in ('SPLIT','SBP'):
            self.data=provider_data();self.data['order']['paymentMethod']={'methodType':method}
            self.positions=[position(),position(shipping=True)]
            extra=position();extra['id']='other';extra['assortment']['name']='DEMOZIP ZIP HOODIE BROWN (XS, коричневый)';extra['price']=999000
            self.positions.append(extra)
            self.data['order']['cart']['items'].append(item('2','DEMOZIP ZIP HOODIE BROWN, Размер=XS','7990.00','1'))
            self.data['order']['cart']['total']['amount']=self.data['order']['orderAmount']='19063.09'
            self.data['operations'][0]['amount']='19063.09'
            record=copy.deepcopy(self.record);record['request']=parse_request(TEXT+'\nDEMOZIP ZIP HOODIE BROWN\nРазмер: XS')
            snapshot=await self.service.preview(record,'prepared-return')
            self.assertEqual(snapshot['amount_cents'],799000)
            self.assertEqual(snapshot['body']['refundCart']['items'],[{'productId':'2','quantityCount':'1'}])
            self.assertEqual(len(snapshot['order']['items']),3)
            self.assertEqual(len(snapshot['financial_items']),1)
            self.assertEqual(snapshot['financial_items'][0]['quantity'],1)
        self.api.refund.assert_not_awaited()

    async def test_accepted_pending_books_ms_with_explanation_but_keeps_actual_provider_state(self):
        self.doc['description']='Описание сотрудника'
        state={'name':'Возврат-проведен','meta':{'href':'state','type':'state'}}
        date={'name':'Дата проведения возврата средств','type':'time','meta':{'href':'date','type':'attributemetadata'}}
        async def ms(method,path,**kwargs):
            if path.endswith('/metadata'):return {'states':[state]}
            if path.endswith('/attributes'):return {'rows':[date]}
            if method=='PUT':
                self.doc['state']=state
                self.doc['description']=kwargs['body']['description']
                self.doc['attributes']=[{'name':date['name'],'value':kwargs['body']['attributes'][0]['value']+'.000'}]
                return copy.deepcopy(self.doc)
            return copy.deepcopy(self.doc)
        self.service.ms=AsyncMock(side_effect=ms)
        async def book(record):
            return await RefundService.book_pending_split(self.service,record)
        self.service.book_pending_split=AsyncMock(side_effect=book)
        result=await self.pending()
        self.assertEqual(result['state'],'pending')
        self.assertIsNone(result['succeeded_at'])
        self.assertTrue(result['split_booked_at'])
        self.assertEqual(self.doc['state']['name'],'Возврат-проведен')
        self.assertIn('Описание сотрудника',self.doc['description'])
        self.assertIn('PENDING',self.doc['description'])
        self.assertIn('пока не подтверждены',self.doc['description'])
        self.assertEqual(sum(c.args[0]=='PUT' for c in self.service.ms.await_args_list),1)
        self.last_operation['status']='SUCCESS';self.data['operations'][-1]['status']='SUCCESS'
        done=await self.service.check(result)
        self.assertEqual(done['state'],'succeeded')
        self.assertEqual(done['split_booked_at'],result['split_booked_at'])
        self.service.sync_ms.assert_not_awaited()
        self.assertEqual(sum(c.args[0]=='PUT' for c in self.service.ms.await_args_list),1)
        self.api.refund.assert_awaited_once()

    async def test_unconfirmed_operation_cannot_book_ms_or_produce_pending_document(self):
        ready=await self.ready()
        for state in ('submitting','uncertain','ready'):
            invalid=dict(ready,state=state,refund_status='PENDING')
            with self.assertRaises(RefundError):await RefundService.book_pending_split(self.service,invalid)
        self.service.sync_ms.assert_not_awaited()
        self.api.refund.assert_not_awaited()

    async def test_pending_ms_failure_retries_booking_without_another_money_request(self):
        self.service.book_pending_split.side_effect=RefundError('MS temporarily unavailable')
        result=await self.pending()
        self.assertEqual(result['state'],'pending');self.assertFalse(result['ms_synced'])
        self.assertEqual(result['receipt_json']['status'],'PENDING')
        self.service.book_pending_split.side_effect=lambda r:self.store.update(r['id'],ms_synced=1,split_booked_at=r['attempted_at'])
        recovered=await self.service.receipt(result)
        self.assertTrue(recovered['ms_synced'])
        self.assertEqual(recovered['state'],'pending')
        self.api.refund.assert_awaited_once()

    async def test_pending_proof_must_match_owner_operation_and_amount(self):
        record=await self.pending()
        invalid=copy.deepcopy(record)
        invalid['approved_by']=OWNER+1
        with self.assertRaises(RefundError):self.service.split.pending_identity(invalid)
        for field,value in (('operation_id',str(uuid.uuid4())),('external_operation_id',str(uuid.uuid4())),
                            ('amount_cents',record['amount_cents']+1),('order_id','other')):
            invalid=copy.deepcopy(record)
            invalid['receipt_json'][field]=value
            with self.assertRaises(RefundError):self.service.split.pending_identity(invalid)

    async def test_support_gets_pending_document_even_when_yandex_status_checks_are_down(self):
        result=await self.pending()
        ui=RefundUI(self.base/'other.sqlite3',self.base,'fake',lambda t:False)
        ui.service=self.service;ui.store=self.store;ui.send_card=AsyncMock()
        bot=SimpleNamespace(send_photo=AsyncMock(return_value=SimpleNamespace(message_id=901)),edit_message_media=AsyncMock())
        self.api.operation=AsyncMock(side_effect=YandexAPIError(503))
        await ui.reconcile_pending(SimpleNamespace(bot=bot))
        await ui.reconcile_pending(SimpleNamespace(bot=bot))
        bot.send_photo.assert_awaited_once()
        self.assertIn('завершение возврата ещё не подтверждено',bot.send_photo.await_args.kwargs['caption'])
        saved=self.store.get(result['id'])
        self.assertEqual(saved['state'],'pending')
        self.assertIsNone(saved['completed_message_id'])
        self.assertEqual(saved['split_notice_status'],'PENDING')
        self.api.refund.assert_awaited_once()

    async def test_success_updates_the_pending_support_photo_without_a_second_message(self):
        result=await self.pending()
        ui=RefundUI(self.base/'other.sqlite3',self.base,'fake',lambda t:False)
        ui.service=self.service;ui.store=self.store
        bot=SimpleNamespace(send_photo=AsyncMock(return_value=SimpleNamespace(message_id=901)),edit_message_media=AsyncMock())
        context=SimpleNamespace(bot=bot)
        await ui.topic_result(result,context)
        self.last_operation['status']='SUCCESS';self.data['operations'][-1]['status']='SUCCESS'
        done=await self.service.check(result)
        await ui.topic_result(done,context)
        await ui.topic_result(done,context)
        bot.send_photo.assert_awaited_once();bot.edit_message_media.assert_awaited_once()
        saved=self.store.get(result['id'])
        self.assertEqual(saved['completed_message_id'],901)
        self.assertEqual(saved['split_notice_status'],'SUCCESS')
        self.assertEqual(saved['receipt_json']['status'],'SUCCESS')
        self.api.refund.assert_awaited_once()

    async def test_failed_operation_updates_the_existing_pending_document_honestly(self):
        result=await self.pending()
        ui=RefundUI(self.base/'other.sqlite3',self.base,'fake',lambda t:False)
        ui.service=self.service;ui.store=self.store
        bot=SimpleNamespace(send_photo=AsyncMock(return_value=SimpleNamespace(message_id=901)),edit_message_media=AsyncMock())
        context=SimpleNamespace(bot=bot)
        await ui.topic_result(result,context)
        self.last_operation['status']='FAIL';self.data['operations'][-1]['status']='FAIL'
        failed=await self.service.check(result)
        await ui.topic_result(failed,context)
        bot.send_photo.assert_awaited_once();bot.edit_message_media.assert_awaited_once()
        self.assertIn('Яндекс не выполнил возврат',bot.edit_message_media.await_args.kwargs['media'].caption)
        self.assertEqual(self.store.get(result['id'])['split_notice_status'],'FAIL')
        self.api.refund.assert_awaited_once()

    async def test_unknown_operation_404_never_reposts(self):
        ready = await self.ready()
        claimed = self.store.claim(ready["id"], ready["revision"], OWNER)
        result = await self.service.check(claimed)
        self.assertEqual(result["state"], "uncertain")
        self.api.refund.assert_not_awaited(); self.service.sync_ms.assert_not_awaited()

    async def test_success_must_also_appear_in_the_correct_order_history(self):
        ready = await self.ready()
        claimed = self.store.claim(ready["id"], ready["revision"], OWNER)
        op = operation(claimed["idempotence_key"], "SUCCESS")
        result = await self.service.split.accept(claimed, op)
        self.assertEqual(result["state"], "uncertain")
        self.service.sync_ms.assert_not_awaited()

    async def test_changed_cart_or_prepared_document_invalidates_confirmation(self):
        ready = await self.ready()
        self.doc["updated"] = "2026-10-20 16:05:00.000"
        with self.assertRaises(RefundError): await self.service.execute(ready["id"], ready["revision"], OWNER)
        self.api.refund.assert_not_awaited()

    async def test_merchant_unpaid_and_real_sample_item_mismatch_are_blocked(self):
        for field, value in (("merchantId", "other"), ("paymentStatus", "REFUNDED"), ("paymentMethod", {"methodType": "UNKNOWN"})):
            self.data = provider_data(); self.data["order"][field] = value
            with self.assertRaises(RefundError): await self.service.preview(self.record)
        self.data = provider_data()
        self.positions[0]["assortment"]["name"] = "AIR PUFFER PINK (S, розовый, Полиэстер 100%, FW26-27-APP-S)"
        with self.assertRaisesRegex(RefundError, "не совпали"): await self.service.preview(self.record)
        self.api.refund.assert_not_awaited()

    async def test_split_contact_typos_warn_and_use_actual_order_fiscal_contact(self):
        record=copy.deepcopy(self.record)
        record['request'].update(name='Ошибка',email='непочта',phone='123')
        snapshot=await self.service.preview(record,'prepared-return')
        self.assertEqual(snapshot['amount_cents'],1039200)
        self.assertEqual(snapshot['receipt_contact'],{'email':'buyer@example.test'})
        self.assertTrue(snapshot['customer_warnings'])
        self.data['order']['fiscalContact']='+79991112233'
        snapshot=await self.service.preview(record,'prepared-return')
        self.assertEqual(snapshot['receipt_contact'],{'phone':'79991112233'})
        self.api.refund.assert_not_awaited()

    async def test_previous_refund_reduces_cart_and_remaining_amount_and_blocks_duplicate_goods(self):
        self.data["operations"].append(operation(str(uuid.uuid4()), "SUCCESS"))
        self.data["order"].update(paymentStatus="PARTIALLY_REFUNDED", orderAmount="681.09")
        self.data["order"]["cart"]["total"]["amount"] = "681.09"
        self.data["order"]["cart"]["items"] = self.data["order"]["cart"]["items"][1:]
        with self.assertRaises(RefundError): await self.service.preview(self.record)
        self.record, _ = self.store.ingest(SUPPORT_CHAT, 12, TEXT.replace("за вещь", "за доставку"), 100)
        self.doc["sum"] = 68109; self.positions = [position(True)]
        ready = await self.ready()
        self.assertEqual(ready["snapshot"]["body"]["refundAmount"], "681.09")
        self.assertEqual(ready["snapshot"]["available_cents"], 68109)
        self.assertEqual(ready["snapshot"]["payment_amount_cents"], 1107309)

    async def test_wrong_amount_order_external_id_or_type_cannot_confirm_money(self):
        ready = await self.ready(); claimed = self.store.claim(ready["id"], ready["revision"], OWNER)
        for field, value in (("amount", "10391.99"), ("orderId", "another"), ("externalOperationId", str(uuid.uuid4())), ("operationType", "CAPTURE")):
            op = operation(claimed["idempotence_key"], "SUCCESS"); op[field] = value
            with self.assertRaises(RefundError): await self.service.split.accept(claimed, op)
        self.service.sync_ms.assert_not_awaited()

    async def test_ms_failure_retries_ms_only_and_does_not_repeat_financial_operation(self):
        self.service.sync_ms.side_effect = RefundError("MS unavailable")
        ready = await self.ready()
        result = await self.service.execute(ready["id"], ready["revision"], OWNER)
        self.assertEqual(result["state"], "succeeded"); self.assertEqual(result["ms_synced"], 0)
        self.service.sync_ms.side_effect = lambda r: self.store.update(r["id"], ms_synced=1)
        result = await self.service.check(result)
        self.assertEqual(result["ms_synced"], 1)
        self.api.refund.assert_awaited_once()

    async def test_provider_mismatch_and_wrong_explicit_yandex_id_are_never_routed_to_yoo(self):
        ready = await self.ready()
        self.store.update(ready["id"], payment_id="yandex:another-order")
        with self.assertRaises(RefundError): await self.service.execute(ready["id"], ready["revision"], OWNER)
        self.assertEqual(self.service.yoo.call.await_count, 0)
        request = parse_request(TEXT + "\nyandex_order_id: 9999:1010101005")
        with self.assertRaises(RefundError): self.api.order_id(request)


class YandexTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_one_post_on_timeout_5xx_409_or_invalid_response(self):
        original = httpx.AsyncClient
        for failure in ("timeout", "server", "conflict", "invalid"):
            calls = []
            def respond(request):
                calls.append(request)
                self.assertEqual(request.url.host, "pay.yandex.ru")
                self.assertEqual(request.headers["authorization"], "Api-Key only-fake-test-key")
                self.assertEqual(json.loads(request.content)["externalOperationId"], "stable-external-id")
                if failure == "timeout": raise httpx.ReadTimeout("timeout", request=request)
                return httpx.Response({"server": 503, "conflict": 409, "invalid": 200}[failure], content=b"invalid")
            api = YandexPay("/unused")
            api.credentials = lambda: {"api_key": "only-fake-test-key"}
            api.enabled = lambda: None
            with patch("yandex_refunds.httpx.AsyncClient", side_effect=lambda **kw: original(transport=httpx.MockTransport(respond), **kw)):
                with self.assertRaises(UncertainRefund): await api.refund(OID, {"refundAmount": "10392.00"}, "stable-external-id")
            self.assertEqual(len(calls), 1)


class YandexConfirmationTests(unittest.IsolatedAsyncioTestCase):
    async def test_true_confirmation_png_is_deduplicated_and_has_no_fiscal_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            ui = RefundUI(Path(tmp) / "test.sqlite3", Path(tmp), "fake-ms", lambda text: False)
            record, _ = ui.store.ingest(SUPPORT_CHAT, 44, TEXT, 100)
            proof = {"provider": "yandex_split", "amount_cents": 1039200, "order_id": OID, "operation_id": OP,
                     "external_operation_id": str(uuid.uuid4()), "status": "SUCCESS", "completed_at": "2026-10-20T16:03:00+03:00", "contact": "buyer@example.test"}
            record = ui.store.update(record["id"], state="succeeded", amount_cents=1039200, receipt_json=proof,
                ms_synced=1,succeeded_at="2026-10-20T13:03:00+00:00")
            bot = SimpleNamespace(send_photo=AsyncMock(side_effect=[SimpleNamespace(message_id=101), SimpleNamespace(message_id=102)]))
            await ui.send_receipt(record, SimpleNamespace(bot=bot))
            bot.send_photo.assert_not_awaited()
            await ui.topic_result(record, SimpleNamespace(bot=bot))
            await ui.topic_result(record, SimpleNamespace(bot=bot))
            await ui.send_receipt(ui.store.get(record["id"]), SimpleNamespace(bot=bot),force_owner=True)
            self.assertEqual(bot.send_photo.await_count, 2)
            self.assertEqual(bot.send_photo.await_args_list[0].kwargs["message_thread_id"], 123)
            self.assertIn("Не является фискальным чеком", bot.send_photo.await_args_list[0].kwargs["caption"])
            self.assertIn("Подтверждение возврата Яндекс Пэй", bot.send_photo.await_args_list[1].kwargs["caption"])
            picture = render_yandex_confirmation(proof)
            with Image.open(picture) as image:
                self.assertEqual(image.width, 956); self.assertGreaterEqual(image.height, 1200)
            picture.close()
            with self.assertRaises(ValueError): render_yandex_confirmation({**proof, "status": "UNKNOWN"})


if __name__ == "__main__": unittest.main()
