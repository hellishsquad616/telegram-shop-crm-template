import copy
import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
from telegram import Chat, Message, Update, User
from telegram.ext import ApplicationHandlerStop
from telegram.error import TelegramError, BadRequest, RetryAfter

from customer_refunds import (
    OWNER, SUPPORT_CHAT, SUPPORT_TOPIC, RefundError, UncertainRefund, RefundStore,
    RefundService, document_items, make_receipt, names_match, parse_request,
    selected_document_items, GiftClarification,
    return_state, hashed, YooKassa,
    PaymentLookupError, YooKassaHTTPError,
)
from customer_refunds_ui import MENU_CUSTOMER_REFUNDS, RefundUI, card
from refund_receipt_image import render_receipt

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

NUMBER = "1010101004"
PID = "11111111-1111-4111-8111-000000000003"
TEXT = NUMBER + "\nname: Тестовый Покупатель\nemail: buyer@example.test\nphone: +79990000000\nПолная, за вещь"


def payment():
    return {"id":PID,"status":"succeeded","paid":True,"test":False,"amount":{"value":"11798.76","currency":"RUB"},
            "refunded_amount":{"value":"0.00","currency":"RUB"},"recipient":{"account_id":"382526"},
            "metadata":{"tilda_orderid":NUMBER,"email":"buyer@example.test","phone":"+79990000000"}}


def positions(shipping=False):
    goods = {"id":"position-a","quantity":1.0,"price":1111200.0,"discount":0,"assortment":{"name":"DIAMOND ZIP HOODIE (M, Хлопок 100 %, чёрный, белый, DZH-M)"}}
    ship = {"id":"position-b","quantity":1.0,"price":68676.0,"discount":0,"assortment":{"name":"СДЭК"}}
    return [goods,ship] if shipping else [goods]


def order(shipping=False, state="Возврат"):
    return {"id":"return-copy","name":"копия - "+NUMBER,"state":{"name":state},"updated":"2026-10-20 13:00:00.000",
            "moment":"2026-10-20 12:23:33.000","sum":1179876.0 if shipping else 1111200.0}


def receipts():
    return [{"id":"sale-receipt","type":"payment","status":"succeeded","tax_system_code":2,"internet":True,
             "receipt_industry_details":[{"federal_id":"030","document_date":"2023-11-21","document_number":"1944","value":"original-fiscal-value"}],
             "items":[{"description":"DIAMOND ZIP HOODIE, Размер=M -20%","quantity":1.0,"amount":{"value":"11112.00","currency":"RUB"},"vat_code":8,"payment_mode":"full_prepayment","payment_subject":"commodity","measure":"piece"},
                      {"description":"СДЭК","quantity":1.0,"amount":{"value":"686.76","currency":"RUB"},"vat_code":8,"payment_mode":"full_prepayment","payment_subject":"commodity","measure":"piece"}]}]


def result(status="succeeded", value="11112.00"):
    return {"id":"refund-result","payment_id":PID,"status":status,"receipt_registration":"succeeded", "amount":{"value":value,"currency":"RUB"}}


def refund_receipt(status="succeeded"):
    r=copy.deepcopy(receipts()[0]);r["items"]=r["items"][:1]
    r.update(type="refund",status=status,id="ra-test-receipt",refund_id="refund-result",payment_id=PID,
             fiscal_provider_id="cloud-receipt-id",fiscal_document_number="63597",fiscal_storage_number="7381440800413918",
             fiscal_attribute="1010101003",registered_at="2026-10-20T11:00:00Z")
    return r


class RefundDomainTests(unittest.TestCase):
    def test_named_item_formats_become_partial_and_old_goods_scope_stays(self):
        for suffix in ('за вещь DIAMOND ZIP HOODIE','за вещь\nDIAMOND ZIP HOODIE',
                       'за вещь\nТовар: DIAMOND ZIP HOODIE','за вещь\nНазвание вещи: DIAMOND ZIP HOODIE'):
            request=parse_request(TEXT.replace('за вещь',suffix))
            self.assertEqual(request['scope'],'part')
            self.assertEqual(request['item_selector'],'DIAMOND ZIP HOODIE')
            self.assertNotIn('item_selector_error',request)
        self.assertEqual(parse_request(TEXT)['scope'],'goods')
        request=parse_request(NUMBER+'\nЮKassa, за вещь\nпуховик демо коричневый')
        self.assertEqual(request['scope'],'part')
        self.assertEqual(request['item_selector'],'пуховик демо коричневый')

    def test_named_item_ambiguity_and_wrong_size_block_instead_of_choosing_first(self):
        rows=positions(True)
        another=copy.deepcopy(rows[0]);another['id']='another';another['assortment']['name']=another['assortment']['name'].replace('(M,','(S,')
        rows.append(another)
        request=parse_request(TEXT+'\nDIAMOND ZIP HOODIE')
        with self.assertRaisesRegex(RefundError,'однозначно'):selected_document_items(order(),rows,request)
        request=parse_request(TEXT+'\nDIAMOND ZIP HOODIE\nРазмер: S')
        raw,chosen=selected_document_items(order(),rows,request)
        self.assertEqual(len(raw),3);self.assertEqual(chosen[0]['id'],'another')
        request['item_selector']='DIAMOND ZIP HOODIE Размер=XS'
        with self.assertRaises(RefundError):selected_document_items(order(),rows,request)

    def test_conflicting_selectors_and_full_scope_with_named_item_require_clarification(self):
        for text in (TEXT+'\nТовар: DIAMOND ZIP\nВещь: DEMOZIP ZIP',TEXT.replace('за вещь','за всё')+'\nТовар: DIAMOND ZIP'):
            request=parse_request(text)
            with self.assertRaises(RefundError):selected_document_items(order(),positions(),request)

    def test_russian_product_and_color_names_match_one_precise_ms_item(self):
        rows=[{'id':'puffer','quantity':2,'price':1599000,'assortment':{'name':'DEMO PUFFER BROWN (XS, коричневый)'}}]
        request=parse_request(TEXT.replace('за вещь','за вещь пуховик коричневый\nРазмер: XS'))
        raw,chosen=selected_document_items(order(),rows,request)
        self.assertEqual(raw[0]['quantity'],2);self.assertEqual(chosen[0]['quantity'],1)

    def test_support_format_and_four_scopes(self):
        for label, scope in [("вещь","goods"),("доставку","delivery"),("всё","all"),("часть вещей","part")]:
            self.assertEqual(parse_request(TEXT.replace("за вещь","за "+label))["scope"],scope)
        self.assertIsNone(parse_request("Обсудим заявку"))
        self.assertEqual(parse_request(TEXT.replace("Полная","Подели"))["method"],"unsupported")

    def test_only_prepared_return_states_are_eligible(self):
        for name in ("Возврат","Отменён","Возврат-срочный","Возврат-принят"):
            self.assertTrue(return_state(name))
        for name in ("Оплачен","Обмен","Возврат-проведен","Возврат Сертификат","Отменен (Lamoda)"):
            self.assertFalse(return_state(name))
        with self.assertRaises(RefundError): document_items(order(state="Оплачен"),positions(),"goods")

    def test_return_copy_amount_and_composition_must_match_scope(self):
        self.assertEqual(document_items(order(),positions(),"goods")[0]["unit_cents"],1111200)
        with self.assertRaises(RefundError): document_items(order(True),positions(True),"goods")
        bad=order();bad["sum"]+=1
        self.assertEqual(document_items(bad,positions(),"goods")[0]['quantity'],1)

    def test_real_tilda_receipt_name_is_matched_with_correct_size(self):
        self.assertTrue(names_match(positions()[0]["assortment"]["name"],receipts()[0]["items"][0]["description"]))
        self.assertFalse(names_match(positions()[0]["assortment"]["name"],"DIAMOND ZIP HOODIE, Размер=S -20%"))

    def test_partial_receipt_preserves_discounted_price_vat_and_prepayment(self):
        fiscal=make_receipt(payment(),receipts(),[],document_items(order(),positions(),"goods"),{"email":"buyer@example.test"})
        self.assertEqual(fiscal["tax_system_code"],2)
        self.assertEqual(fiscal["items"][0]["amount"]["value"],"11112.00")
        self.assertEqual(fiscal["items"][0]["vat_code"],8)
        self.assertEqual(fiscal["items"][0]["payment_mode"],"full_prepayment")
        self.assertEqual(fiscal["receipt_industry_details"],receipts()[0]["receipt_industry_details"])
        self.assertEqual(len(fiscal["items"]),1)

    def test_same_item_cannot_be_refunded_again_using_another_copy(self):
        previous=copy.deepcopy(receipts()[0]);previous["type"]="refund";previous["items"]=previous["items"][:1]
        with self.assertRaises(RefundError): make_receipt(payment(),receipts(),[previous],document_items(order(),positions(),"goods"),{"email":"buyer@example.test"})

    def test_ambiguous_or_unregistered_original_receipt_is_blocked(self):
        with self.assertRaises(RefundError): make_receipt(payment(),receipts()*2,[],document_items(order(),positions(),"goods"),{"email":"buyer@example.test"})
        invalid=receipts();invalid[0]["status"]="pending"
        with self.assertRaises(RefundError): make_receipt(payment(),invalid,[],document_items(order(),positions(),"goods"),{"email":"buyer@example.test"})

    def test_different_paid_prices_for_identical_item_need_clarification(self):
        fiscal=receipts();duplicate=copy.deepcopy(fiscal[0]['items'][0])
        duplicate['amount']['value']='5000.00'
        fiscal[0]['items'].append(duplicate)
        paid=payment();paid['amount']['value']='16798.76'
        with self.assertRaisesRegex(RefundError,'однозначно'):
            make_receipt(paid,fiscal,[],document_items(order(),positions(),'part'),{'email':'buyer@example.test'})

    def test_full_scope_cannot_silently_include_goods_missing_from_ms_selection(self):
        fiscal=receipts();duplicate=copy.deepcopy(fiscal[0]['items'][0])
        duplicate['description']='DEMOZIP ZIP HOODIE BROWN, Размер=XS'
        duplicate['amount']['value']='5000.00'
        fiscal[0]['items'].append(duplicate)
        paid=payment();paid['amount']['value']='16798.76'
        with self.assertRaisesRegex(RefundError,'не все оставшиеся'):
            make_receipt(paid,fiscal,[],document_items(order(),positions(),'all'),{'email':'buyer@example.test'},'all')

    def test_document_total_is_not_the_source_of_the_refund_amount(self):
        invalid=order();invalid["sum"]+=0.1
        self.assertEqual(document_items(invalid,positions(),"goods")[0]['quantity'],1)

    def test_payment_order_metadata_cannot_be_overridden_by_description(self):
        p=payment();p["metadata"]["tilda_orderid"]="9999999999"
        p["description"]="Оплата заказа № "+NUMBER
        self.assertFalse(RefundService.payment_matches(p,NUMBER))





class RefundServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name)
        self.service=RefundService(self.base/"test.sqlite3",self.base,"fake-ms-token")
        self.service.customer=AsyncMock(return_value="customer-fixture")
        self.store=self.service.store
        self.record,_=self.store.ingest(SUPPORT_CHAT,1,TEXT,100)
        self.service.document=AsyncMock(return_value=(order(),positions()))
        self.service.candidates=AsyncMock(return_value=[order()])
        self.service.payment=AsyncMock(return_value=payment())
        async def pages(path,params):
            if path=="/refunds":return []
            return receipts()
        self.service.yoo.pages=AsyncMock(side_effect=pages)
        self.service.yoo.call=AsyncMock(return_value=result())
        self.service.sync_ms=AsyncMock()

    async def ready(self):
        snapshot=await self.service.preview(self.record,"return-copy")
        return self.store.prepare(self.record["id"],self.record["revision"],snapshot)

    async def test_missing_method_uses_exact_tilda_order_without_guessing_scope_or_sending_money(self):
        record,_=self.store.ingest(SUPPORT_CHAT,22,NUMBER+'\nName: Тестовый Покупатель\nEmail: split@example.test',100)
        self.service.crm_path=str(self.base/'crm.sqlite3')
        with patch('database.Database') as database:
            database.return_value.get_order_by_number.return_value={'payment_method':'Яндекс Сплит'}
            with self.assertRaisesRegex(RefundError,'за вещь'):
                await self.service.preview(record)
            database.return_value.get_order_by_number.assert_called_once_with(NUMBER)
        saved=RefundStore(self.store.path).get(record['id'])
        self.assertEqual(saved['request']['method'],'yandex_split')
        self.assertEqual(saved['request']['method_source'],'tilda')
        self.assertEqual(saved['request']['scope'],'')
        self.assertIsNone(saved['attempted_at']);self.assertIsNone(saved['snapshot'])
        self.assertIn('Оплата: Яндекс Сплит',card(saved))
        self.service.yoo.call.assert_not_awaited()

    async def test_yoo_not_found_corrects_provider_only_after_matching_tilda_and_yandex_api(self):
        from tests.test_yandex_refunds import provider_data,OID
        self.service.crm_path=str(self.base/'crm.sqlite3')
        self.service.payment.side_effect=PaymentLookupError('not found','not_found')
        oid=OID.split(':')[0]+':'+NUMBER
        data=provider_data();data['order']['orderId']=oid
        self.service.split.api.order_id=Mock(return_value=oid)
        self.service.split.api.order=AsyncMock(return_value=data)
        self.service.split.preview=AsyncMock(return_value={'verified':'split'})
        with patch('database.Database') as db:
            db.return_value.get_order_by_number.return_value={'payment_method':'Яндекс Сплит'}
            value=await self.service.preview(self.record,'return-copy')
        saved=self.store.get(self.record['id'])
        self.assertEqual(value,{'verified':'split'})
        self.assertEqual(saved['request']['method'],'yandex_split')
        self.assertEqual(saved['request']['method_source'],'yandex_api')
        self.assertEqual(saved['source_text'],TEXT);self.assertIsNone(saved['attempted_at'])
        self.service.split.preview.assert_awaited_once();self.service.yoo.call.assert_not_awaited()

    async def test_provider_is_not_corrected_on_ambiguous_yoo_or_wrong_yandex_shop(self):
        from tests.test_yandex_refunds import provider_data,OID
        self.service.crm_path=str(self.base/'crm.sqlite3')
        oid=OID.split(':')[0]+':'+NUMBER
        self.service.split.api.order_id=Mock(return_value=oid)
        data=provider_data();data['order'].update(merchantId='other-shop',orderId=oid)
        self.service.split.api.order=AsyncMock(return_value=data)
        self.service.split.preview=AsyncMock()
        with patch('database.Database') as db:
            db.return_value.get_order_by_number.return_value={'payment_method':'Яндекс Сплит'}
            self.service.payment.side_effect=PaymentLookupError('ambiguous','ambiguous')
            with self.assertRaises(RefundError):await self.service.preview(self.record,'return-copy')
            self.service.split.api.order.assert_not_awaited()
            self.service.payment.side_effect=PaymentLookupError('not found','not_found')
            with self.assertRaises(RefundError):await self.service.preview(self.record,'return-copy')
        self.assertEqual(self.store.get(self.record['id'])['request']['method'],'yookassa')
        self.service.split.preview.assert_not_awaited();self.service.yoo.call.assert_not_awaited()
        ready=self.store.prepare(self.record['id'],self.record['revision'],{'payment_id':PID,'amount_cents':1111200,'order':{'id':'return-copy'}})
        self.service.split.api.order.reset_mock()
        with self.assertRaises(RefundError):await self.service.preview(ready,'return-copy')
        self.service.split.api.order.assert_not_awaited()

    def test_unknown_tilda_label_does_not_guess_payment_from_contacts_or_default_to_yoo(self):
        self.service.crm_path=str(self.base/'crm.sqlite3')
        record,_=self.store.ingest(SUPPORT_CHAT,22,NUMBER+'\nEmail: yookassa@example.test\nза всё',100)
        for label in ('Оплачен','CARD','Другой метод',''):
            with patch('database.Database') as database:
                database.return_value.get_order_by_number.return_value={'payment_method':label}
                resolved=self.service.resolve_method(record)
                self.assertEqual(resolved['request']['method'],'unsupported')
                self.assertIn('Оплата: Не определена',card(resolved))
                self.assertNotIn('Оплата: ЮKassa',card(resolved))
        with patch('database.Database') as database:
            self.assertEqual(self.service.resolve_method(self.record)['request']['method'],'yookassa')
            database.return_value.get_order_by_number.assert_called_once_with(NUMBER)

    def test_full_payment_alias_and_contacts_are_resolved_without_changing_source_or_sending_money(self):
        record,_=self.store.ingest(SUPPORT_CHAT,22,'Нужен чек возврата — '+NUMBER,100)
        self.service.crm_path=str(self.base/'crm.sqlite3')
        with patch('database.Database') as database:
            database.return_value.get_order_by_number.return_value={'payment_method':'Полная оплата','customer_name':'Тестовый Покупатель','email':'buyer@example.test','phone':'+79990000000'}
            saved=self.service.resolve_method(record)
        self.assertEqual(saved['request']['method'],'yookassa')
        self.assertEqual(saved['request']['name'],'Тестовый Покупатель')
        self.assertEqual(saved['request']['email'],'buyer@example.test')
        self.assertEqual(saved['source_text'],record['source_text'])
        self.assertIsNone(saved['attempted_at']);self.assertIsNone(saved['snapshot'])
        self.service.yoo.call.assert_not_called()

    def test_owner_method_choice_is_durable_and_cannot_change_an_attempted_or_prepared_payment(self):
        record,_=self.store.ingest(SUPPORT_CHAT,22,'Нужен чек возврата — '+NUMBER,100)
        with self.assertRaises(RefundError):self.store.choose_method(record,'yookassa',OWNER+1)
        saved=self.store.choose_method(record,'yookassa',OWNER)
        self.assertEqual(saved['request']['method_source'],'owner')
        self.assertEqual(saved['revision'],record['revision']+1)
        self.assertEqual(RefundStore(self.store.path).get(saved['id'])['request']['method'],'yookassa')
        with self.assertRaises(RefundError):self.store.choose_method(record,'yandex_split',OWNER)
        with self.assertRaises(RefundError):self.store.choose_method(saved,'yandex_split',OWNER)

    def test_tilda_method_resolution_cannot_overwrite_edited_or_attempted_request(self):
        record,_=self.store.ingest(SUPPORT_CHAT,22,NUMBER+'\nза всё',100)
        edited,_=self.store.ingest(SUPPORT_CHAT,22,NUMBER+'\nСплит, за доставку',100)
        with self.assertRaises(RefundError):self.store.resolve_method(record,'yookassa','ЮKassa')
        self.assertEqual(self.store.get(record['id'])['request'],edited['request'])
        record,_=self.store.ingest(SUPPORT_CHAT,23,NUMBER+'\nза всё',100)
        from refund_storage import connect_database
        with connect_database(self.store.path) as connection:
            connection.execute("UPDATE customer_refund_requests SET attempted_at='already-attempted' WHERE id=?",(record['id'],))
        self.assertEqual(self.store.resolve_method(record,'yandex_split','Яндекс Сплит')['request']['method'],'unsupported')

    def test_owner_scope_clarification_is_audited_durable_and_never_claims_money(self):
        record,_=self.store.ingest(SUPPORT_CHAT,22,NUMBER+'\nСплит',100)
        with self.assertRaises(RefundError):self.store.clarify_scope(record,'delivery',OWNER+1)
        saved=self.store.clarify_scope(record,'delivery',OWNER)
        self.assertEqual(saved['request']['scope'],'delivery')
        self.assertEqual(saved['request']['scope_source'],'owner')
        self.assertEqual(saved['revision'],record['revision']+1)
        self.assertEqual(saved['source_text'],record['source_text'])
        self.assertIsNone(saved['attempted_at']);self.assertIsNone(saved['snapshot'])
        self.assertEqual(RefundStore(self.store.path).get(saved['id'])['request']['scope'],'delivery')
        with self.assertRaises(RefundError):self.store.clarify_scope(record,'all',OWNER)
        with self.assertRaises(RefundError):self.store.clarify_scope(saved,'all',OWNER)

    def enable(self):
        (self.base/".secrets").mkdir(exist_ok=True)
        enable_gate(self.base,"yookassa")

    def add_unpaid_scarf(self):
        scarf={'id':'scarf','quantity':1,'price':699000,'discount':0,'assortment':{'name':'DEMO SCARF GREY (ONESIZE, FW26-27-LSG-O)'}}
        self.service.document.return_value=(order(),positions()+[scarf])
        return scarf

    async def test_gift_choice_is_durable_owner_only_and_no_invents_payment_for_paid_answer(self):
        self.add_unpaid_scarf()
        with self.assertRaises(GiftClarification) as question:await self.service.preview(self.record)
        self.store.preparation_error(self.record['id'],self.record['revision'],question.exception)
        current=self.store.get(self.record['id'])
        self.assertEqual(current['state'],'needs_info');self.assertIsNone(current['snapshot'])
        with self.assertRaises(RefundError):self.store.choose_gift(current['id'],current['revision'],999,0,'gift')
        paid=self.store.choose_gift(current['id'],current['revision'],OWNER,0,'paid')
        with self.assertRaisesRegex(RefundError,'оплаченная позиция не найдена'):await self.service.preview(paid)
        with self.assertRaises(RefundError):self.store.choose_gift(current['id'],current['revision'],OWNER,0,'gift')
        restarted=RefundStore(self.store.path)
        self.assertEqual(restarted.get(paid['id'])['gift_review']['items'][0]['decision'],'paid')
        gift=restarted.choose_gift(paid['id'],paid['revision'],OWNER,0,'gift')
        snapshot=await self.service.preview(gift)
        self.assertEqual(snapshot['amount_cents'],1111200)
        self.assertEqual(snapshot['excluded_gifts'][0]['id'],'scarf')
        self.assertEqual(len(snapshot['order']['items']),2)
        self.assertEqual(len(snapshot['financial_items']),1)
        ready=self.store.prepare(gift['id'],gift['revision'],snapshot)
        self.store.claim(ready['id'],ready['revision'],OWNER)
        with self.assertRaises(RefundError):self.store.choose_gift(ready['id'],ready['revision'],OWNER,0,'paid')
        self.service.yoo.call.assert_not_awaited()

    async def test_new_scarf_after_approval_saves_question_without_any_money_attempt(self):
        ready=await self.ready()
        self.add_unpaid_scarf()
        with self.assertRaises(GiftClarification):await self.service.execute(ready['id'],ready['revision'],OWNER)
        saved=self.store.get(ready['id'])
        self.assertEqual(saved['state'],'needs_info')
        self.assertTrue(saved['gift_review']);self.assertIsNone(saved['snapshot'])
        self.assertIsNone(saved['attempted_at'])
        self.service.yoo.call.assert_not_awaited()

    async def test_paid_scarf_with_missing_onesize_in_receipt_is_returned_at_its_paid_price(self):
        self.add_unpaid_scarf()
        fiscal=receipts();scarf=copy.deepcopy(fiscal[0]['items'][0]);scarf.update(description='DEMO SCARF GREY',amount={'value':'1990.00','currency':'RUB'})
        fiscal[0]['items'].append(scarf)
        paid=payment();paid['amount']['value']='13788.76';self.service.payment.return_value=paid
        async def pages(path,params):return [] if path=='/refunds' else fiscal
        self.service.yoo.pages=AsyncMock(side_effect=pages)
        snapshot=await self.service.preview(self.record)
        self.assertEqual(snapshot['amount_cents'],1310200)
        self.assertFalse(snapshot.get('excluded_gifts'))
        self.assertEqual(snapshot['financial_items'][1]['unit_cents'],199000)
        self.service.yoo.call.assert_not_awaited()

    async def test_gift_answer_is_invalidated_if_paid_receipt_changes(self):
        self.add_unpaid_scarf()
        with self.assertRaises(GiftClarification) as question:await self.service.preview(self.record)
        self.store.preparation_error(self.record['id'],self.record['revision'],question.exception)
        current=self.store.get(self.record['id'])
        current=self.store.choose_gift(current['id'],current['revision'],OWNER,0,'gift')
        fiscal=receipts();scarf=copy.deepcopy(fiscal[0]['items'][0]);scarf.update(description='DEMO SCARF GREY',amount={'value':'1990.00','currency':'RUB'})
        fiscal[0]['items'].append(scarf)
        paid=payment();paid['amount']['value']='13788.76';self.service.payment.return_value=paid
        async def pages(path,params):return [] if path=='/refunds' else fiscal
        self.service.yoo.pages=AsyncMock(side_effect=pages)
        with self.assertRaises(GiftClarification) as revised:await self.service.preview(current)
        self.assertIsNone(revised.exception.review['items'][0]['decision'])
        self.assertNotEqual(revised.exception.review['basis'],current['gift_review']['basis'])
        self.service.yoo.call.assert_not_awaited()

    async def test_zero_price_gift_from_registered_receipt_is_excluded_after_owner_choice(self):
        self.add_unpaid_scarf()
        fiscal=receipts();scarf=copy.deepcopy(fiscal[0]['items'][0]);scarf.update(description='DEMO SCARF GREY',amount={'value':'0.00','currency':'RUB'})
        fiscal[0]['items'].append(scarf)
        async def pages(path,params):return [] if path=='/refunds' else fiscal
        self.service.yoo.pages=AsyncMock(side_effect=pages)
        with self.assertRaises(GiftClarification) as question:await self.service.preview(self.record)
        record=copy.deepcopy(self.record);record['gift_review']=question.exception.review
        record['gift_review']['items'][0]['decision']='gift'
        snapshot=await self.service.preview(record)
        self.assertEqual(snapshot['amount_cents'],1111200)
        self.assertEqual(len(snapshot['body']['receipt']['items']),1)
        self.service.yoo.call.assert_not_awaited()

    async def test_preview_does_not_send_money_and_uses_partial_target_document(self):
        ready=await self.ready()
        self.assertEqual(ready["amount_cents"],1111200)
        self.assertEqual(ready["snapshot"]["body"]["receipt"]["items"][0]["vat_code"],8)
        self.service.yoo.call.assert_not_awaited()

    async def test_ms_price_does_not_override_paid_price_and_discount(self):
        rows=positions();rows[0]['price']+=200000
        self.service.document.return_value=(order(),rows)
        snapshot=await self.service.preview(self.record,'return-copy')
        self.assertEqual(snapshot['amount_cents'],1111200)
        self.assertEqual(snapshot['financial_items'][0]['unit_cents'],1111200)
        self.assertEqual(snapshot['order']['items'][0]['unit_cents'],1311200)
        self.assertEqual(snapshot['body']['amount']['value'],'11112.00')
        self.service.yoo.call.assert_not_awaited()

    async def test_named_single_item_from_multi_item_document_uses_only_its_paid_price_and_receipt(self):
        rows=positions(True)
        extra=copy.deepcopy(rows[0]);extra['id']='other';extra['assortment']['name']='DEMOZIP ZIP HOODIE BROWN (XS, коричневый)';extra['price']=799000
        rows.append(extra)
        fiscal=receipts();extra_fiscal=copy.deepcopy(fiscal[0]['items'][0]);extra_fiscal['description']='DEMOZIP ZIP HOODIE BROWN, Размер=XS';extra_fiscal['amount']['value']='7990.00'
        fiscal[0]['items'].append(extra_fiscal)
        paid=payment();paid['amount']['value']='19788.76'
        self.service.payment.return_value=paid
        self.service.document.return_value=(order(True),rows)
        async def pages(path,params):return [] if path=='/refunds' else fiscal
        self.service.yoo.pages=AsyncMock(side_effect=pages)
        record,_=self.store.ingest(SUPPORT_CHAT,30,TEXT+'\nDEMOZIP ZIP HOODIE BROWN\nРазмер: XS',100)
        snapshot=await self.service.preview(record,'return-copy')
        self.assertEqual(snapshot['amount_cents'],799000)
        self.assertEqual(snapshot['body']['amount']['value'],'7990.00')
        self.assertEqual(len(snapshot['order']['items']),3)
        self.assertEqual(snapshot['selected_items'][0]['id'],'other')
        self.assertEqual(len(snapshot['body']['receipt']['items']),1)
        self.assertIn('DEMOZIP',snapshot['body']['receipt']['items'][0]['description'])
        self.service.yoo.call.assert_not_awaited()

    async def test_named_partial_return_ms_sync_preserves_other_items_and_adds_one_explanation(self):
        record,_=self.store.ingest(SUPPORT_CHAT,30,TEXT+'\nDIAMOND ZIP HOODIE',100)
        doc=order(True);doc['description']='Заметка сотрудника';rows=positions(True)
        self.service.document=AsyncMock(side_effect=lambda oid:(copy.deepcopy(doc),copy.deepcopy(rows)))
        snapshot=await self.service.preview(record,'return-copy')
        ready=self.store.prepare(record['id'],record['revision'],snapshot)
        saved=self.store.update(ready['id'],state='succeeded',succeeded_at='2026-10-20T10:10:10+00:00')
        state={'name':'Возврат-проведен','meta':{'href':'state','type':'state'}}
        date={'name':'Дата проведения возврата средств','type':'time','meta':{'href':'date','type':'attributemetadata'}}
        async def ms(method,path,**kwargs):
            if path.endswith('/metadata'):return {'states':[state]}
            if path.endswith('/attributes'):return {'rows':[date]}
            if method=='PUT':
                self.assertNotIn('positions',kwargs['body'])
                doc.update(state=state,description=kwargs['body']['description'],attributes=[{'name':date['name'],'value':kwargs['body']['attributes'][0]['value']}])
            return copy.deepcopy(doc)
        self.service.ms=AsyncMock(side_effect=ms)
        await RefundService.sync_ms(self.service,saved)
        await RefundService.sync_ms(self.service,self.store.get(saved['id']))
        self.assertIn('Заметка сотрудника',doc['description'])
        self.assertIn('Частичный возврат',doc['description'])
        self.assertIn('11 112,00 ₽',doc['description'])
        self.assertEqual(len(rows),2)
        self.assertEqual(sum(c.args[0]=='PUT' for c in self.service.ms.await_args_list),1)
        self.service.yoo.call.assert_not_awaited()

    async def test_named_item_returns_one_of_two_identical_paid_units(self):
        rows=positions(True);rows[0]['quantity']=2
        fiscal=receipts();fiscal[0]['items'][0]['quantity']=2
        paid=payment();paid['amount']['value']='22910.76'
        self.service.payment.return_value=paid
        self.service.document.return_value=(order(True),rows)
        async def pages(path,params):return [] if path=='/refunds' else fiscal
        self.service.yoo.pages=AsyncMock(side_effect=pages)
        record,_=self.store.ingest(SUPPORT_CHAT,30,TEXT+'\nDIAMOND ZIP HOODIE',100)
        snapshot=await self.service.preview(record,'return-copy')
        self.assertEqual(snapshot['amount_cents'],1111200)
        self.assertEqual(snapshot['body']['receipt']['items'][0]['quantity'],'1')
        self.assertEqual(snapshot['order']['items'][0]['quantity'],2)
        self.service.yoo.call.assert_not_awaited()

    async def test_named_item_already_returned_cannot_be_selected_again(self):
        paid=payment();paid['refunded_amount']['value']='11112.00'
        self.service.payment.return_value=paid
        old=copy.deepcopy(receipts()[0]);old['type']='refund';old['items']=old['items'][:1]
        previous={'id':'old-refund','payment_id':PID,'status':'succeeded','amount':{'value':'11112.00','currency':'RUB'}}
        async def pages(path,params):
            if path=='/refunds':return [previous]
            return [old] if params.get('refund_id') else receipts()
        self.service.yoo.pages=AsyncMock(side_effect=pages)
        record,_=self.store.ingest(SUPPORT_CHAT,30,TEXT+'\nDIAMOND ZIP HOODIE',100)
        with self.assertRaises(RefundError):await self.service.preview(record,'return-copy')
        self.service.yoo.call.assert_not_awaited()

    async def test_full_order_includes_paid_delivery_omitted_in_ms(self):
        record,_=self.store.ingest(SUPPORT_CHAT,12,TEXT.replace('за вещь','за всё'),100)
        snapshot=await self.service.preview(record,'return-copy')
        self.assertEqual(snapshot['amount_cents'],1179876)
        self.assertEqual(len(snapshot['financial_items']),2)
        self.assertEqual(len(snapshot['order']['items']),1)
        self.service.yoo.call.assert_not_awaited()

    async def test_delivery_amount_comes_from_payment_even_with_wrong_ms_price(self):
        record,_=self.store.ingest(SUPPORT_CHAT,12,TEXT.replace('за вещь','за доставку'),100)
        rows=positions(True)[1:];rows[0]['price']=200000
        self.service.document.return_value=(order(),rows)
        snapshot=await self.service.preview(record,'return-copy')
        self.assertEqual(snapshot['amount_cents'],68676)
        self.assertEqual(snapshot['body']['receipt']['items'][0]['description'],'СДЭК')
        self.service.yoo.call.assert_not_awaited()

    async def test_no_funds_without_owner_and_live_gate(self):
        ready=await self.ready()
        with self.assertRaises(RefundError): await self.service.execute(ready["id"],ready["revision"],999)
        with self.assertRaises(RefundError): await self.service.execute(ready["id"],ready["revision"],OWNER)
        self.service.yoo.call.assert_not_awaited()
        self.assertIsNone(self.store.get(ready["id"])["attempted_at"])

    async def test_owner_confirmation_posts_once_and_then_syncs_ms(self):
        ready=await self.ready();self.enable()
        saved=await self.service.execute(ready["id"],ready["revision"],OWNER)
        self.assertEqual(saved["state"],"succeeded")
        self.service.yoo.call.assert_awaited_once()
        self.assertEqual(self.service.yoo.call.await_args.args,("POST","/refunds"))
        self.assertTrue(self.service.yoo.call.await_args.kwargs["key"])
        self.service.sync_ms.assert_awaited_once()
        with self.assertRaises(RefundError): await self.service.execute(ready["id"],ready["revision"],OWNER)
        self.service.yoo.call.assert_awaited_once()

    async def test_pending_refund_does_not_change_moysklad(self):
        ready=await self.ready();self.enable();self.service.yoo.call.return_value=result("pending")
        saved=await self.service.execute(ready["id"],ready["revision"],OWNER)
        self.assertEqual(saved["state"],"pending")
        self.service.sync_ms.assert_not_awaited()

    async def test_timeout_is_reconciled_by_get_without_another_post(self):
        ready=await self.ready();self.enable()
        self.service.yoo.call.side_effect=UncertainRefund("timeout")
        with self.assertRaises(UncertainRefund): await self.service.execute(ready["id"],ready["revision"],OWNER)
        uncertain=self.store.get(ready["id"])
        self.assertEqual(uncertain["state"],"uncertain")
        recovered=result();recovered["metadata"]={"shopcrm_refund_request":str(ready["id"]),"brand":"shopcrm","operation_key":self.store.get(ready["id"])["idempotence_key"]}
        self.service.yoo.pages.side_effect=None;self.service.yoo.pages.return_value=[recovered]
        saved=await self.service.check(uncertain)
        self.assertEqual(saved["state"],"succeeded")
        self.service.yoo.call.assert_awaited_once()

    async def funds_failure(self):
        ready=await self.ready()
        claimed=self.store.claim(ready['id'],ready['revision'],OWNER)
        canceled=result('canceled');canceled['cancellation_details']={'reason':'insufficient_funds','party':'yoo_money'}
        failed=await self.service.accept_result(claimed,canceled)
        return failed,canceled

    async def test_funds_failure_creates_durable_outbox_once_and_enters_waiting_list(self):
        failed,canceled=await self.funds_failure()
        await self.service.accept_result(failed,canceled)
        reopened=RefundStore(self.store.path)
        self.assertEqual([r['id'] for r in reopened.waiting_for_funds()],[failed['id']])
        notices=reopened.pending_decision_notices()
        self.assertEqual(len(notices),1)
        self.assertEqual(notices[0]['state'],'insufficient_funds')
        self.assertIn('Он будет проведен позднее',notices[0]['note'])
        self.service.yoo.call.assert_not_awaited();self.service.sync_ms.assert_not_awaited()

    async def test_funds_retry_rechecks_old_operation_archives_attempt_and_requires_new_confirmation(self):
        failed,canceled=await self.funds_failure()
        self.service.yoo.get=AsyncMock(return_value=canceled)
        ready=await self.service.retry_after_funds(failed['id'],failed['revision'],OWNER)
        self.assertEqual(ready['state'],'ready');self.assertIsNone(ready['attempted_at'])
        self.assertIsNone(ready['idempotence_key']);self.assertIsNone(ready['refund_id'])
        self.assertEqual(self.store.waiting_for_funds(),[])
        self.service.yoo.get.assert_awaited_once_with('/refunds/'+failed['refund_id'])
        self.service.yoo.call.assert_not_awaited()
        from refund_storage import connect_database
        with connect_database(self.store.path) as connection:
            archived=connection.execute('SELECT * FROM customer_refund_attempt_history').fetchall()
        self.assertEqual(len(archived),1)
        self.assertEqual(archived[0]['operation_key'],failed['idempotence_key'])
        self.assertEqual(json.loads(archived[0]['record_json'])['refund_id'],failed['refund_id'])
        with self.assertRaises(RefundError):await self.service.retry_after_funds(failed['id'],failed['revision'],OWNER)
        with self.assertRaises(RefundError):self.store.claim(ready['id'],failed['revision'],OWNER)
        new_claim=self.store.claim(ready['id'],ready['revision'],OWNER)
        self.assertNotEqual(new_claim['idempotence_key'],failed['idempotence_key'])

    async def test_funds_retry_never_restarts_pending_unknown_or_non_owner_operation(self):
        failed,canceled=await self.funds_failure()
        self.service.yoo.get=AsyncMock(side_effect=RefundError('network unavailable'))
        with self.assertRaises(RefundError):await self.service.retry_after_funds(failed['id'],failed['revision'],OWNER+1)
        self.service.yoo.get.assert_not_awaited()
        with self.assertRaises(RefundError):await self.service.retry_after_funds(failed['id'],failed['revision'],OWNER)
        self.assertEqual(self.store.get(failed['id'])['idempotence_key'],failed['idempotence_key'])
        self.service.yoo.get.side_effect=None;self.service.yoo.get.return_value=result('pending')
        with self.assertRaises(RefundError):await self.service.retry_after_funds(failed['id'],failed['revision'],OWNER)
        self.assertEqual(self.store.get(failed['id'])['state'],'pending')
        self.assertEqual(self.store.get(failed['id'])['idempotence_key'],failed['idempotence_key'])
        self.service.yoo.call.assert_not_awaited()

    async def test_post_403_recovers_real_canceled_operation_and_reports_insufficient_funds(self):
        ready=await self.ready();self.enable()
        posted=False
        async def post(*args,**kwargs):
            nonlocal posted
            posted=True
            raise YooKassaHTTPError(403,{'code':'forbidden','description':'Refund request rejected'})
        async def pages(path,params):
            if path=='/refunds':
                if not posted:return []
                operation=result('canceled')
                operation['cancellation_details']={'party':'yoo_money','reason':'insufficient_funds'}
                operation['metadata']={'shopcrm_refund_request':str(ready['id']),'brand':'shopcrm','operation_key':self.store.get(ready['id'])['idempotence_key']}
                return [operation]
            return receipts()
        self.service.yoo.call.side_effect=post;self.service.yoo.pages.side_effect=pages
        saved=await self.service.execute(ready['id'],ready['revision'],OWNER)
        self.assertEqual(saved['state'],'failed');self.assertEqual(saved['refund_id'],'refund-result')
        self.assertEqual(saved['refund_status'],'canceled')
        self.assertIn('недостаточно средств магазина',saved['last_error']);self.assertIn('insufficient_funds',saved['last_error'])
        self.assertNotIn('403',saved['last_error'])
        self.service.sync_ms.assert_not_awaited();self.service.yoo.call.assert_awaited_once()
        with self.assertRaises(RefundError):await self.service.execute(ready['id'],ready['revision'],OWNER)
        self.service.yoo.call.assert_awaited_once()

    async def test_post_403_without_visible_operation_keeps_details_and_never_retries_post(self):
        ready=await self.ready();self.enable()
        self.service.yoo.call.side_effect=YooKassaHTTPError(403,{'code':'forbidden','id':'11111111-1111-4111-8111-000000000001','description':'Permission denied'})
        saved=await self.service.execute(ready['id'],ready['revision'],OWNER)
        self.assertEqual(saved['state'],'uncertain');self.assertIn('forbidden',saved['last_error'])
        self.assertIn('11111111',saved['last_error']);self.assertIsNone(saved['refund_id'])
        saved=await self.service.check(saved)
        self.assertIn('forbidden',saved['last_error'])
        self.service.yoo.call.assert_awaited_once();self.service.sync_ms.assert_not_awaited()

    async def test_old_failed_403_is_reconciled_by_exact_request_and_key_after_restart(self):
        ready=await self.ready();claimed=self.store.claim(ready['id'],ready['revision'],OWNER)
        failed=self.store.update(ready['id'],state='failed',last_error='ЮKassa: HTTP 403. Возврат не подтверждён.')
        matched=result('canceled');matched['cancellation_details']={'reason':'insufficient_funds'}
        matched['metadata']={'shopcrm_refund_request':str(ready['id']),'brand':'shopcrm','operation_key':claimed['idempotence_key']}
        wrong=copy.deepcopy(matched);wrong['metadata']['operation_key']='another-operation'
        self.service.yoo.pages.side_effect=None;self.service.yoo.pages.return_value=[wrong,matched]
        saved=await self.service.check(failed)
        self.assertEqual(saved['refund_id'],'refund-result');self.assertIn('insufficient_funds',saved['last_error'])
        self.service.yoo.call.assert_not_awaited();self.service.sync_ms.assert_not_awaited()

    async def test_independent_refund_and_receipt_reads_start_together(self):
        started=set();both=asyncio.Event()
        async def pages(path,params):
            started.add(path)
            if len(started)==2:both.set()
            await asyncio.wait_for(both.wait(),timeout=0.5)
            return [] if path=='/refunds' else receipts()
        self.service.yoo.pages.side_effect=pages
        snapshot=await self.service.preview(self.record,'return-copy')
        self.assertEqual(snapshot['amount_cents'],1111200)

    async def test_changed_document_after_preview_blocks_money(self):
        ready=await self.ready();self.enable()
        modified=order();modified["updated"]="2026-10-20 14:00:00.000"
        self.service.document.return_value=(modified,positions())
        with self.assertRaises(RefundError): await self.service.execute(ready["id"],ready["revision"],OWNER)
        self.service.yoo.call.assert_not_awaited()

    async def test_repreparation_invalidates_an_old_confirmation_card(self):
        first=await self.ready()
        snapshot=await self.service.preview(first,"return-copy")
        second=self.store.prepare(first["id"],first["revision"],snapshot)
        self.assertGreater(second["revision"],first["revision"])
        self.enable()
        with self.assertRaises(RefundError): await self.service.execute(first["id"],first["revision"],OWNER)
        self.service.yoo.call.assert_not_awaited()

    async def test_simultaneous_refunds_of_one_payment_are_blocked(self):
        first=await self.ready()
        second,_=self.store.ingest(SUPPORT_CHAT,2,TEXT,100)
        snapshot=await self.service.preview(second,"return-copy")
        second=self.store.prepare(second["id"],second["revision"],snapshot)
        self.store.claim(first["id"],first["revision"],OWNER)
        with self.assertRaises(RefundError): self.store.claim(second["id"],second["revision"],OWNER)

    async def test_source_edits_cannot_reuse_old_prepared_version(self):
        ready=await self.ready()
        edited,changed=self.store.ingest(SUPPORT_CHAT,1,TEXT+"\nуточнение",100)
        self.assertTrue(changed);self.assertGreater(edited["revision"],ready["revision"])
        self.assertIsNone(edited["snapshot"])
        with self.assertRaises(RefundError): self.store.claim(edited["id"],ready["revision"],OWNER)

    async def test_failed_ms_sync_does_not_start_a_second_refund(self):
        ready=await self.ready();self.enable()
        self.service.sync_ms.side_effect=RefundError("MS unavailable")
        saved=await self.service.execute(ready["id"],ready["revision"],OWNER)
        self.assertEqual(saved["state"],"succeeded");self.assertFalse(saved["ms_synced"])
        retried=await self.service.check(saved)
        self.assertEqual(retried["state"],"succeeded")
        self.assertIn("MS unavailable",retried["last_error"])
        self.service.yoo.call.assert_awaited_once()

    async def test_contact_mismatch_warns_but_wrong_merchant_is_blocked(self):
        invalid=payment();invalid["metadata"]["email"]="other@example.test"
        self.service.payment.return_value=invalid
        snapshot=await self.service.preview(self.record,'return-copy')
        self.assertTrue(snapshot['customer_warnings'])
        self.assertEqual(snapshot['receipt_contact']['email'],'other@example.test')
        invalid=payment();invalid["recipient"]["account_id"]="other-shop"
        self.service.payment.return_value=invalid
        with self.assertRaises(RefundError): await self.ready()

    async def test_wrong_or_missing_submitted_contacts_use_registered_receipt_contact(self):
        fiscal=receipts();fiscal[0]['customer']={'email':'fiscal@example.test','phone':'+79991112233'}
        async def pages(path,params):return [] if path=='/refunds' else fiscal
        self.service.yoo.pages=AsyncMock(side_effect=pages)
        for name,email,number in (('Ошибка','непочта','123'),('','','')):
            record=copy.deepcopy(self.record)
            record['request'].update(name=name,email=email,phone=number)
            snapshot=await self.service.preview(record,'return-copy')
            self.assertEqual(snapshot['receipt_contact'],{'email':'fiscal@example.test','phone':'79991112233'})
            self.assertEqual(snapshot['body']['receipt']['customer'],snapshot['receipt_contact'])
            self.assertEqual(snapshot['amount_cents'],1111200)
            self.assertTrue(snapshot['customer_warnings'])
        self.service.yoo.call.assert_not_awaited()

    async def test_customer_contact_changes_do_not_invalidate_financial_confirmation(self):
        ready=await self.ready();self.enable()
        self.service.customer_details=AsyncMock(return_value={'customer':{'name':'Исправленное ФИО'},'warnings':['Замечание']})
        result=await self.service.execute(ready['id'],ready['revision'],OWNER)
        self.assertEqual(result['state'],'succeeded')
        self.service.yoo.call.assert_awaited_once()

    async def test_explicit_payment_for_another_order_stays_blocked_with_matching_contacts(self):
        wrong=payment();wrong['metadata']['tilda_orderid']='9999999999'
        self.service.payment.return_value=wrong
        with self.assertRaisesRegex(RefundError,'связь платежа с номером заказа'):
            await self.service.preview(self.record,'return-copy')
        self.service.yoo.call.assert_not_awaited()

    async def test_full_return_uses_entire_payment_but_not_a_second_receipt(self):
        r,_=self.store.ingest(SUPPORT_CHAT,3,TEXT.replace("за вещь","за всё"),100)
        self.service.document.return_value=(order(True),positions(True))
        snapshot=await self.service.preview(r,"return-copy")
        self.assertEqual(snapshot["amount_cents"],1179876)
        self.assertNotIn("receipt",snapshot["body"])

    async def test_remaining_balance_after_prior_refund_still_requires_partial_receipt(self):
        paid=payment();paid["refunded_amount"]={"value":"686.76","currency":"RUB"}
        self.service.payment.return_value=paid
        previous={"id":"old-shipping-refund","payment_id":PID,"status":"succeeded","amount":{"value":"686.76","currency":"RUB"}}
        old=copy.deepcopy(receipts()[0]);old.update(type="refund",status="succeeded");old["items"]=old["items"][1:]
        async def pages(path,params):
            if path=="/refunds":return [previous]
            if params.get("refund_id"):return [old]
            return receipts()
        self.service.yoo.pages=AsyncMock(side_effect=pages)
        snapshot=await self.service.preview(self.record,"return-copy")
        self.assertEqual(snapshot["amount_cents"],snapshot["available_cents"])
        self.assertIn("receipt",snapshot["body"])
        self.assertEqual(len(snapshot["body"]["receipt"]["items"]),1)
        self.assertEqual(snapshot["body"]["receipt"]["items"][0]["description"],receipts()[0]["items"][0]["description"])

    async def test_delivery_only_uses_original_delivery_receipt_item(self):
        r,_=self.store.ingest(SUPPORT_CHAT,4,TEXT.replace("за вещь","за доставку"),100)
        delivery_order=order();delivery_order["sum"]=68676
        self.service.document.return_value=(delivery_order,positions(True)[1:])
        snapshot=await self.service.preview(r,"return-copy")
        self.assertEqual(snapshot["amount_cents"],68676)
        self.assertEqual(snapshot["body"]["receipt"]["items"][0]["description"],"СДЭК")
        self.assertEqual(snapshot["body"]["receipt"]["items"][0]["vat_code"],8)

    async def test_resending_successful_document_cannot_refund_more_quantity(self):
        first=await self.ready()
        self.store.update(first["id"],state="succeeded")
        second,_=self.store.ingest(SUPPORT_CHAT,5,TEXT,100)
        snapshot=await self.service.preview(second,"return-copy")
        second=self.store.prepare(second["id"],second["revision"],snapshot)
        with self.assertRaises(RefundError): self.store.claim(second["id"],second["revision"],OWNER)

    async def test_ms_sync_updates_only_selected_copy_and_verifies_date(self):
        ready=await self.ready()
        saved=self.store.update(ready["id"],state="succeeded",succeeded_at="2026-10-20T10:10:10+00:00")
        state={"name":"Возврат-проведен","meta":{"href":"status-href","type":"state"}}
        date={"name":"Дата проведения возврата средств","type":"time","meta":{"href":"date-href","type":"attributemetadata"}}
        async def ms(method,path,**kwargs):
            if path.endswith("/metadata"): return {"states":[state]}
            if path.endswith("/attributes"): return {"rows":[date]}
            self.assertEqual(path,"/entity/customerorder/return-copy")
            if method=="PUT":
                self.assertEqual(kwargs["body"],{"state":{"meta":state["meta"]},"attributes":[{"meta":date["meta"],"value":"2026-10-20 13:10:00"}]})
                return {}
            return {"state":{"name":"Возврат-проведен"},"attributes":[{"name":date["name"],"value":"2026-10-20 13:10:00.000"}]}
        self.service.ms=AsyncMock(side_effect=ms)
        await RefundService.sync_ms(self.service,saved)
        self.assertTrue(self.store.get(saved["id"])["ms_synced"])
        self.assertEqual(sum(c.args[0]=="PUT" for c in self.service.ms.await_args_list),1)
        self.service.yoo.call.assert_not_awaited()

    async def test_ms_sync_recovers_a_saved_minute_without_rewriting_document(self):
        ready=await self.ready()
        saved=self.store.update(ready["id"],state="succeeded",succeeded_at="2026-10-20T10:10:41.994451+00:00")
        document=order();document["state"]={"name":"Возврат-проведен"}
        document["attributes"]=[{"name":"Дата проведения возврата средств","value":"2026-10-20 13:10:00.000"}]
        self.service.document.return_value=(document,positions())
        self.service.ms=AsyncMock(side_effect=AssertionError('No write or metadata call for a verified completed document'))
        await RefundService.sync_ms(self.service,saved)
        self.assertTrue(self.store.get(saved["id"])["ms_synced"])
        self.service.ms.assert_not_awaited()
        self.service.yoo.call.assert_not_awaited()

    async def test_ms_sync_needs_success_and_does_not_accept_unverified_update(self):
        ready=await self.ready()
        with self.assertRaises(RefundError): await RefundService.sync_ms(self.service,ready)
        saved=self.store.update(ready["id"],state="succeeded",succeeded_at="2026-10-20T10:10:10+00:00")
        self.service.ms=AsyncMock(side_effect=[{"states":[{"name":"Возврат-проведен","meta":{}}]},
            {"rows":[{"name":"Дата проведения возврата средств","type":"time","meta":{}}]}, {}, {"state":{"name":"Возврат"}}])
        with self.assertRaises(RefundError): await RefundService.sync_ms(self.service,saved)
        self.assertFalse(self.store.get(saved["id"])["ms_synced"])

    async def test_failed_receipt_does_not_undo_money_result(self):
        ready=await self.ready();self.enable()
        response=result();response["receipt_registration"]="canceled"
        self.service.yoo.call.return_value=response
        saved=await self.service.execute(ready["id"],ready["revision"],OWNER)
        self.assertEqual(saved["state"],"succeeded")
        self.assertIn("ОШИБКА регистрации",card(saved))
        self.service.yoo.call.assert_awaited_once()

    async def test_actual_registered_receipt_is_saved_and_pending_delivery_is_retried(self):
        ready=await self.ready()
        saved=self.store.update(ready["id"],state="succeeded",refund_id="refund-result",receipt_status="succeeded",ms_synced=1)
        self.assertIn(saved["id"],[r["id"] for r in self.store.list(pending=True)])
        self.service.yoo.pages=AsyncMock(return_value=[refund_receipt()])
        saved=await self.service.receipt(saved)
        self.assertEqual(saved["receipt_json"]["id"],"ra-test-receipt")
        self.assertEqual(saved["receipt_json"]["fiscal_document_number"],"63597")
        self.assertEqual(saved["receipt_json"]["contact"],"buyer@example.test")
        self.service.yoo.pages.assert_awaited_once_with("/receipts",{"refund_id":"refund-result"})
        self.service.yoo.call.assert_not_awaited()
        self.store.update(saved["id"],completed_message_id=8,receipt_support_message_id=8,owner_message_id=6)
        self.assertNotIn(saved["id"],[r["id"] for r in self.store.list(pending=True)])

    async def test_unregistered_or_wrong_refund_receipt_cannot_become_a_picture(self):
        ready=await self.ready()
        saved=self.store.update(ready["id"],state="pending",refund_id="refund-result")
        self.service.yoo.pages=AsyncMock(return_value=[refund_receipt()])
        self.assertIsNone((await self.service.receipt(saved))["receipt_json"])
        self.service.yoo.pages.assert_not_awaited()
        saved=self.store.update(saved["id"],state="succeeded")
        self.service.yoo.pages.return_value=[refund_receipt("pending")]
        pending=await self.service.receipt(saved)
        self.assertIsNone(pending["receipt_json"])
        self.assertEqual(pending["receipt_status"],"pending")
        wrong=refund_receipt();wrong["refund_id"]="different-refund"
        self.service.yoo.pages.return_value=[wrong]
        self.assertIsNone((await self.service.receipt(saved))["receipt_json"])
        for field,value in (("payment_id","other-payment"),("fiscal_attribute","")):
            invalid=refund_receipt();invalid[field]=value
            self.service.yoo.pages.return_value=[invalid]
            with self.assertRaises(RefundError): await self.service.receipt(saved)
        invalid=refund_receipt();invalid["items"][0]["amount"]["value"]="11111.00"
        self.service.yoo.pages.return_value=[invalid]
        with self.assertRaises(RefundError): await self.service.receipt(saved)

    async def test_receipt_image_supports_long_contacts_without_clipping(self):
        from PIL import Image
        r=refund_receipt()
        r.update(amount_cents=1111200,contact="long-contact-"*8+"@example.test")
        picture=render_receipt(r)
        with Image.open(picture) as image:
            self.assertEqual(image.width,956)
            self.assertGreaterEqual(image.height,1360)
            self.assertEqual(image.format,"PNG")
        picture.close()


class YooKassaTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_403_preserves_provider_error_fields_and_redacts_secret(self):
        secret='test-only-secret-key'
        def respond(request):return httpx.Response(403,json={'code':'forbidden','id':'11111111-1111-4111-8111-000000000001','parameter':'payment_id','description':'Access denied '+secret})
        original=httpx.AsyncClient;client=YooKassa('/unused')
        client.credentials=lambda:{'shop_id':'382526','secret_key':secret}
        with patch('customer_refunds.httpx.AsyncClient',side_effect=lambda **kw:original(transport=httpx.MockTransport(respond),**kw)):
            with self.assertRaises(YooKassaHTTPError) as failure:await client.get('/refunds')
        self.assertEqual(failure.exception.status,403)
        self.assertIn('forbidden',str(failure.exception));self.assertIn('11111111',str(failure.exception))
        self.assertNotIn(secret,str(failure.exception))
    async def test_timeout_server_error_and_invalid_post_response_are_never_retried(self):
        for failure in ("timeout","server","invalid"):
            calls=[]
            def respond(request):
                calls.append(request)
                if failure=="timeout": raise httpx.ReadTimeout("timeout",request=request)
                return httpx.Response(503 if failure=="server" else 200,content=b"invalid")
            original=httpx.AsyncClient
            client=YooKassa("/unused")
            client.enabled=lambda:None
            client.credentials=lambda:{"shop_id":"382526","secret_key":"test-only"}
            with patch("customer_refunds.httpx.AsyncClient",side_effect=lambda **kw:original(transport=httpx.MockTransport(respond),**kw)):
                with self.assertRaises(UncertainRefund): await client.call("POST","/refunds",body={},key="stable-test-key")
            self.assertEqual(len(calls),1)




class RefundUITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        base=Path(self.tmp.name)
        self.ui=RefundUI(base/"test.sqlite3",base,"fake",lambda t:t.startswith("MENU"))
        self.context=SimpleNamespace(user_data={},bot=SimpleNamespace(id=444,send_message=AsyncMock(return_value=SimpleNamespace(message_id=77)),send_photo=AsyncMock(return_value=SimpleNamespace(message_id=88)),edit_message_text=AsyncMock(),edit_message_caption=AsyncMock()),application=SimpleNamespace(create_task=lambda *args,**kwargs:None))

    def update(self,uid=OWNER,chat="private",data="cr:home"):
        message=SimpleNamespace(reply_text=AsyncMock())
        return SimpleNamespace(effective_user=SimpleNamespace(id=uid),effective_chat=SimpleNamespace(type=chat),effective_message=message,callback_query=SimpleNamespace(data=data,answer=AsyncMock(),edit_message_text=AsyncMock()))

    def gift_question_record(self):
        record,_=self.ui.store.ingest(SUPPORT_CHAT,1,TEXT,100)
        review={'document_id':'return-copy','basis':'verified-source','items':[{'id':'scarf','name':'DEMO SCARF GREY (ONESIZE)','quantity':1,'decision':None}]}
        self.ui.store.preparation_error(record['id'],record['revision'],GiftClarification(review))
        return self.ui.store.get(record['id'])

    def gift_snapshot(self,record):
        return {'payment_id':PID,'order':{'id':'return-copy','name':'Возврат-'+NUMBER,'state':'Возврат','items':document_items(order(),positions(),'goods')},
            'amount_cents':1111200,'payment_amount_cents':1179876,'available_cents':1179876,
            'gift_review':record['gift_review'],'excluded_gifts':[{'id':'scarf','name':'DEMO SCARF GREY','quantity':1}]}

    async def funds_failure(self):
        record,_=self.ui.store.ingest(SUPPORT_CHAT,1,TEXT,100)
        snapshot=self.gift_snapshot(record);snapshot['previous_refunds']=[]
        ready=self.ui.store.prepare(record['id'],record['revision'],snapshot)
        claimed=self.ui.store.claim(ready['id'],ready['revision'],OWNER)
        canceled=result('canceled');canceled['cancellation_details']={'reason':'insufficient_funds'}
        return await self.ui.service.accept_result(claimed,canceled)

    async def test_funds_notification_has_exact_text_reply_and_independent_durable_delivery(self):
        record=await self.funds_failure()
        failures=[True]
        async def deliver(**kwargs):
            if kwargs['chat_id']==SUPPORT_CHAT and failures:
                failures.pop();raise TelegramError('temporary failure')
            return SimpleNamespace(message_id=77 if kwargs['chat_id']==OWNER else 88)
        self.context.bot.send_message.side_effect=deliver
        await self.ui.send_decision_notices(self.context)
        await self.ui.send_decision_notices(self.context)
        await self.ui.send_decision_notices(self.context)
        calls=self.context.bot.send_message.await_args_list
        self.assertEqual(len([c for c in calls if c.kwargs['chat_id']==OWNER]),1)
        support=[c.kwargs for c in calls if c.kwargs['chat_id']==SUPPORT_CHAT]
        self.assertEqual(len(support),2)
        self.assertEqual(support[-1]['reply_to_message_id'],record['source_message'])
        self.assertEqual(support[-1]['message_thread_id'],SUPPORT_TOPIC)
        self.assertIn('Возврат средств не проведен, так как на Юкассе недостаточно средств для проведения возврата. Он будет проведен позднее',support[-1]['text'])
        self.assertEqual(self.ui.store.pending_decision_notices(),[])

    async def test_funds_button_filters_list_and_card_offers_preparation_not_direct_execution(self):
        record=await self.funds_failure()
        update=self.update()
        await self.ui.home(update,self.context)
        markup=update.callback_query.edit_message_text.await_args.kwargs['reply_markup']
        self.assertIn('Ожидает денежных средств · 1',[b.text for row in markup.inline_keyboard for b in row])
        await self.ui.dispatch_safe(self.update(data='cr:funds'),self.context)
        await self.ui.home(update,self.context,funds=True)
        self.assertIn('Ожидает денежных средств',update.callback_query.edit_message_text.await_args.args[0])
        actions=[b.callback_data for row in self.ui.keyboard(record).inline_keyboard for b in row]
        self.assertTrue(any(a.startswith('cr:retryfunds:') for a in actions))
        self.assertFalse(any(a.startswith(('cr:execute:','cr:confirm:')) for a in actions))
        self.assertIn('Ожидает денежных средств',card(record))

    async def test_gift_buttons_recalculate_only_and_require_a_new_owner_confirmation(self):
        record=self.gift_question_record()
        labels=[b.text for row in self.ui.keyboard(record).inline_keyboard for b in row]
        self.assertIn('Да, подарок',labels);self.assertIn('Нет, куплен',labels)
        self.assertIn('Это подарок?',card(record))
        self.ui.service.preview=AsyncMock(side_effect=lambda r,doc:self.gift_snapshot(r))
        self.ui.service.execute=AsyncMock();self.ui.send_card=AsyncMock()
        bits=['cr','gift',str(record['id']),str(record['revision']),'0','gift']
        await self.ui.dispatch(self.update(),self.context,bits)
        ready=self.ui.store.get(record['id'])
        self.assertEqual(ready['state'],'ready');self.assertIsNone(ready['attempted_at'])
        self.assertEqual(ready['gift_review']['items'][0]['decision'],'gift')
        self.assertEqual(ready['amount_cents'],1111200)
        with self.assertRaises(RefundError):await self.ui.dispatch(self.update(),self.context,bits)
        self.ui.service.execute.assert_not_awaited()
        self.context.bot.send_message.assert_not_awaited()

    async def test_gift_answer_recovers_after_restart_with_the_previously_selected_document(self):
        record=self.gift_question_record()
        record=self.ui.store.choose_gift(record['id'],record['revision'],OWNER,0,'gift')
        restarted=RefundUI(self.ui.store.path,self.ui.service.base,'fake',lambda t:False)
        restarted.service.candidates=AsyncMock(side_effect=AssertionError('Use the selected document'))
        restarted.service.preview=AsyncMock(side_effect=lambda r,doc:self.gift_snapshot(r))
        restarted.send_card=AsyncMock()
        await restarted.reconcile_pending(self.context)
        saved=restarted.store.get(record['id'])
        self.assertEqual(saved['state'],'ready')
        self.assertEqual(restarted.service.preview.await_args.args[1],'return-copy')
        restarted.service.candidates.assert_not_awaited()
        self.assertIsNone(saved['attempted_at'])

    async def test_staff_or_group_callback_cannot_execute_refund(self):
        for update in (self.update(999),self.update(OWNER,"supergroup")):
            with self.assertRaises(ApplicationHandlerStop): await self.ui.callback(update,self.context)
            self.assertTrue(update.callback_query.answer.await_args.kwargs["show_alert"])
        self.context.bot.send_message.assert_not_awaited()

    async def test_source_deduplication_and_only_owner_gets_customer_details(self):
        record,changed=self.ui.store.ingest(SUPPORT_CHAT,1,TEXT,100)
        self.assertTrue(changed)
        again,changed=self.ui.store.ingest(SUPPORT_CHAT,1,TEXT,100)
        self.assertFalse(changed);self.assertEqual(again["id"],record["id"])
        await self.ui.send_card(record,self.context)
        self.assertEqual(self.context.bot.send_message.await_args.kwargs["chat_id"],OWNER)

    async def test_list_click_opens_current_version_in_the_clicked_message(self):
        record,_=self.ui.store.ingest(SUPPORT_CHAT,1,TEXT,100)
        self.ui.store.update(record['id'],owner_message_id=22)
        self.ui.store.ingest(SUPPORT_CHAT,1,TEXT+'\nуточнение',100)
        update=self.update()
        update.effective_message.message_id=99
        # A stale view button is safe; mutable gift decisions remain versioned.
        await self.ui.dispatch(update,self.context,['cr','view',str(record['id']),str(record['revision'])])
        update.callback_query.edit_message_text.assert_awaited_once()
        self.context.bot.edit_message_text.assert_not_awaited()
        self.context.bot.send_message.assert_not_awaited()
        self.assertEqual(self.ui.store.get(record['id'])['owner_message_id'],99)
        with self.assertRaises(RefundError):
            await self.ui.dispatch(update,self.context,['cr','gift',str(record['id']),str(record['revision']),'0','gift'])

    async def test_background_does_not_replace_list_after_leaving_a_card(self):
        record,_=self.ui.store.ingest(SUPPORT_CHAT,1,TEXT,100)
        self.ui.store.show_owner_card(record['id'],99)
        update=self.update();update.effective_message.message_id=99
        await self.ui.home(update,self.context)
        self.assertEqual(self.ui.store.get(record['id'])['owner_message_id'],0)
        await self.ui.send_card(record,self.context)
        self.context.bot.edit_message_text.assert_not_awaited()
        self.context.bot.send_message.assert_not_awaited()
        self.assertNotIn(record['id'],[r['id'] for r in self.ui.store.list(pending=True) if r['owner_message_id'] is None])
        await self.ui.dispatch(update,self.context,['cr','view',str(record['id']),str(record['revision'])])
        self.assertEqual(self.ui.store.get(record['id'])['owner_message_id'],99)

    async def test_unchanged_owner_card_is_not_edited_every_worker_scan_or_after_restart(self):
        record,_=self.ui.store.ingest(SUPPORT_CHAT,1,TEXT,100)
        await self.ui.send_card(record,self.context)
        await self.ui.send_card(self.ui.store.get(record['id']),self.context)
        restarted=RefundUI(self.ui.store.path,self.ui.service.base,'fake',lambda _:False)
        await restarted.send_card(restarted.store.get(record['id']),self.context)
        self.context.bot.send_message.assert_awaited_once()
        self.context.bot.edit_message_text.assert_not_awaited()
        changed=self.ui.store.update(record['id'],last_error='Нужна сверка')
        await restarted.send_card(changed,self.context)
        self.context.bot.edit_message_text.assert_awaited_once()

    async def test_telegram_rate_limit_is_deferred_and_durable_card_outbox_recovers(self):
        record,_=self.ui.store.ingest(SUPPORT_CHAT,1,TEXT,100)
        record=self.ui.store.update(record['id'],state='needs_info',owner_message_id=99)
        self.context.bot.edit_message_text.side_effect=RetryAfter(30)
        await self.ui.send_card(record,self.context)
        await self.ui.send_card(self.ui.store.get(record['id']),self.context)
        self.context.bot.edit_message_text.assert_awaited_once()
        self.assertTrue(self.ui.store.get(record['id'])['owner_card_pending'])
        self.assertIn(record['id'],[r['id'] for r in self.ui.store.list(pending=True)])
        self.context.bot.edit_message_text.side_effect=None
        restarted=RefundUI(self.ui.store.path,self.ui.service.base,'fake',lambda _:False)
        # The service deadline survives restart: no premature edit attempt.
        await restarted.reconcile_pending(self.context)
        self.context.bot.edit_message_text.assert_awaited_once()
        with patch('customer_refunds_ui.time.time',return_value=self.ui.store.owner_retry_until()+1):
            await restarted.reconcile_pending(self.context)
        self.assertFalse(self.ui.store.get(record['id'])['owner_card_pending'])
        self.context.bot.send_message.assert_not_awaited()

    async def test_deleted_telegram_card_is_hidden_without_recreating_the_message(self):
        record,_=self.ui.store.ingest(SUPPORT_CHAT,1,TEXT,100)
        record=self.ui.store.update(record['id'],owner_message_id=99)
        self.context.bot.edit_message_text.side_effect=BadRequest('Message to edit not found')
        await self.ui.send_card(record,self.context)
        self.assertEqual(self.ui.store.get(record['id'])['owner_message_id'],0)
        await self.ui.send_card(self.ui.store.get(record['id']),self.context)
        self.context.bot.send_message.assert_not_awaited()
        self.assertIsNone(self.ui.store.get(record['id'])['attempted_at'])

    async def test_delete_button_does_not_wait_for_provider_lock_or_delete_financial_history(self):
        record,_=self.ui.store.ingest(SUPPORT_CHAT,1,TEXT,100)
        self.ui.store.show_owner_card(record['id'],99)
        update=self.update(data=f"cr:delete:{record['id']}:0")
        update.effective_message.message_id=99;update.callback_query.delete_message=AsyncMock()
        lock=self.ui.locks.setdefault(record['id'],asyncio.Lock())
        await lock.acquire()
        try:await asyncio.wait_for(self.ui.dispatch_safe(update,self.context),timeout=1)
        finally:lock.release()
        update.callback_query.delete_message.assert_awaited_once()
        saved=self.ui.store.get(record['id'])
        self.assertEqual(saved['owner_message_id'],0);self.assertEqual(saved['source_text'],TEXT)
        self.assertIsNone(saved['attempted_at'])
        await self.ui.send_card(saved,self.context)
        self.context.bot.send_message.assert_not_awaited()

    async def test_view_and_confirm_are_responsive_while_background_holds_provider_lock(self):
        record=self.confirmation_record()
        lock=self.ui.locks.setdefault(record['id'],asyncio.Lock());await lock.acquire()
        try:
            for action in ('view','confirm'):
                update=self.update(data=f"cr:{action}:{record['id']}:{record['revision']}")
                update.effective_message.message_id=99
                with patch('customer_refunds_ui.card',return_value='Проверенная карточка'):
                    await asyncio.wait_for(self.ui.dispatch_safe(update,self.context),timeout=0.2)
                update.callback_query.edit_message_text.assert_awaited_once()
        finally:lock.release()

    async def test_callback_answer_rate_limit_does_not_prevent_authorized_button_dispatch(self):
        record,_=self.ui.store.ingest(SUPPORT_CHAT,1,TEXT,100)
        update=self.update(data=f"cr:view:{record['id']}:{record['revision']}")
        update.callback_query.answer.side_effect=RetryAfter(2)
        self.ui.open_card=AsyncMock()
        tasks=[];self.context.application.create_task=lambda task,**kw:tasks.append(task)
        with self.assertRaises(ApplicationHandlerStop):await self.ui.callback(update,self.context)
        for task in tasks:await task
        self.ui.open_card.assert_awaited_once()

    async def test_persisted_telegram_pause_explains_the_delay_and_cannot_execute_money(self):
        import time
        record,_=self.ui.store.ingest(SUPPORT_CHAT,1,TEXT,100)
        self.ui.store.show_owner_card(record['id'],99)
        self.ui.store.defer_owner_until(time.time()+3600)
        restarted=RefundUI(self.ui.store.path,self.ui.service.base,'fake',lambda _:False)
        update=self.update(data=f"cr:execute:{record['id']}:{record['revision']}")
        self.context.application.create_task=Mock()
        with self.assertRaises(ApplicationHandlerStop):await restarted.callback(update,self.context)
        self.assertIn('Telegram ограничил',update.callback_query.answer.await_args.args[0])
        self.assertTrue(update.callback_query.answer.await_args.kwargs['show_alert'])
        self.context.application.create_task.assert_not_called()
        self.assertIsNone(self.ui.store.get(record['id'])['attempted_at'])
        self.assertTrue(self.ui.store.get(record['id'])['owner_card_pending'])

    async def test_switching_cards_detaches_the_previous_record_from_that_message(self):
        first,_=self.ui.store.ingest(SUPPORT_CHAT,1,TEXT,100)
        second,_=self.ui.store.ingest(SUPPORT_CHAT,2,TEXT,100)
        self.ui.store.show_owner_card(first['id'],99)
        self.ui.store.show_owner_card(second['id'],99)
        self.assertEqual(self.ui.store.get(first['id'])['owner_message_id'],0)
        self.assertEqual(self.ui.store.get(second['id'])['owner_message_id'],99)

    async def test_explicit_rejection_is_sent_with_reason_and_resubmission_instructions_once(self):
        record,_=self.ui.store.ingest(SUPPORT_CHAT,1,TEXT,100)
        note='В документе отсутствует доставка. Для полного возврата добавьте её.'
        rejected=self.ui.store.decision(record['id'],record['revision'],OWNER,'rejected',note)
        self.ui.store.decision(record['id'],record['revision'],OWNER,'rejected',note)
        await self.ui.send_decision_notices(self.context)
        await self.ui.send_decision_notices(self.context)
        self.context.bot.send_message.assert_awaited_once()
        args=self.context.bot.send_message.await_args.kwargs
        self.assertEqual((args['chat_id'],args['message_thread_id'],args['reply_to_message_id']),(SUPPORT_CHAT,SUPPORT_TOPIC,1))
        for value in (NUMBER,note,'новую заявку','ФИО','email','телефон','способ оплаты','за весь заказ'):
            self.assertIn(value,args['text'])
        self.assertNotIn('buyer@example.test',args['text'])
        self.assertIsNone(self.ui.store.get(record['id'])['attempted_at'])
        restarted=RefundUI(self.ui.store.path,self.ui.service.base,'fake',lambda t:False)
        await restarted.send_decision_notices(self.context)
        self.context.bot.send_message.assert_awaited_once()

    def failed_refund(self):
        record,_=self.ui.store.ingest(SUPPORT_CHAT,1,TEXT,100)
        snapshot={'payment_id':PID,'order':{'id':'return-copy','name':order()['name'],'state':'Отменен',
            'items':document_items(order(),positions(),'goods')},'amount_cents':1111200,
            'available_cents':1179876,'payment_amount_cents':1179876,'previous_refunds':[]}
        record=self.ui.store.prepare(record['id'],record['revision'],snapshot)
        record=self.ui.store.claim(record['id'],record['revision'],OWNER)
        return self.ui.store.update(record['id'],state='failed',refund_id='refund-result',
            refund_status='canceled',last_error='ЮKassa отменила возврат: rejected_by_payee',owner_message_id=99)

    async def test_chargeback_is_an_explicit_owner_notice_sent_once_without_financial_changes(self):
        record=self.failed_refund()
        keys=('state','refund_id','refund_status','payment_id','idempotence_key','attempted_at','succeeded_at','ms_synced','last_error','snapshot','amount_cents')
        before={k:record[k] for k in keys}
        self.assertIn('Чарджбек',[b.text for row in self.ui.keyboard(record).inline_keyboard for b in row])
        self.assertEqual(self.ui.store.pending_decision_notices(),[])
        self.ui.service.check=AsyncMock();self.ui.service.execute=AsyncMock();self.ui.service.sync_ms=AsyncMock()
        bits=['cr','chargeback',str(record['id']),str(record['revision'])]
        await self.ui.dispatch(self.update(),self.context,bits)
        await self.ui.dispatch(self.update(),self.context,bits)
        self.context.bot.send_message.assert_awaited_once()
        args=self.context.bot.send_message.await_args.kwargs
        self.assertEqual((args['chat_id'],args['message_thread_id'],args['reply_to_message_id']),(SUPPORT_CHAT,SUPPORT_TOPIC,1))
        self.assertIn('Клиент обратился в банк, поэтому с нашей стороны возврат провести нельзя.',args['text'])
        self.assertIn(NUMBER,args['text'])
        self.assertNotIn('новую заявку',args['text'])
        saved=self.ui.store.get(record['id'])
        self.assertEqual(before,{k:saved[k] for k in keys})
        self.assertTrue(saved['chargeback_at'])
        self.assertIn('отметка владельца',card(saved))
        self.assertNotIn('Чарджбек',[b.text for row in self.ui.keyboard(saved).inline_keyboard for b in row])
        self.ui.service.check.assert_not_awaited();self.ui.service.execute.assert_not_awaited();self.ui.service.sync_ms.assert_not_awaited()
        restarted=RefundUI(self.ui.store.path,self.ui.service.base,'fake',lambda t:False)
        restarted.store.chargeback(record['id'],record['revision'],OWNER)
        await restarted.send_decision_notices(self.context)
        self.context.bot.send_message.assert_awaited_once()

    async def test_chargeback_delivery_retries_after_restart(self):
        record=self.failed_refund()
        self.ui.store.chargeback(record['id'],record['revision'],OWNER)
        self.context.bot.send_message.side_effect=TelegramError('Temporary failure')
        await self.ui.send_decision_notices(self.context)
        self.assertEqual(len(self.ui.store.pending_decision_notices()),1)
        restarted=RefundUI(self.ui.store.path,self.ui.service.base,'fake',lambda t:False)
        self.context.bot.send_message.side_effect=None
        await restarted.reconcile_pending(self.context)
        self.assertEqual(restarted.store.pending_decision_notices(),[])
        self.assertEqual(self.context.bot.send_message.await_count,2)

    async def test_chargeback_rejects_non_owner_stale_cards_and_unconfirmed_or_successful_refunds(self):
        record=self.failed_refund()
        for actor,revision in ((999,record['revision']),(OWNER,record['revision']-1)):
            with self.assertRaises(RefundError):self.ui.store.chargeback(record['id'],revision,actor)
        for state in ('new','ready','needs_info','submitting','uncertain','pending','succeeded','rejected'):
            saved=self.ui.store.update(record['id'],state=state)
            with self.assertRaises(RefundError):self.ui.store.chargeback(record['id'],record['revision'],OWNER)
            self.assertNotIn('Чарджбек',[b.text for row in self.ui.keyboard(saved).inline_keyboard for b in row])
        self.ui.store.update(record['id'],state='failed',refund_status='pending')
        with self.assertRaises(RefundError):self.ui.store.chargeback(record['id'],record['revision'],OWNER)
        self.assertEqual(self.ui.store.pending_decision_notices(),[])
        for update in (self.update(999,data='cr:chargeback:1:2'),self.update(OWNER,'supergroup',data='cr:chargeback:1:2')):
            with self.assertRaises(ApplicationHandlerStop):await self.ui.callback(update,self.context)
        self.context.bot.send_message.assert_not_awaited()

    async def test_failed_rejection_delivery_is_retried_after_restart_without_owner_card_dependency(self):
        record,_=self.ui.store.ingest(SUPPORT_CHAT,1,TEXT,100)
        self.ui.store.update(record['id'],owner_message_id=0)
        self.ui.store.decision(record['id'],record['revision'],OWNER,'rejected','Уточните вариант возврата')
        self.context.bot.send_message.side_effect=TelegramError('Temporary Telegram failure')
        await self.ui.send_decision_notices(self.context)
        self.assertEqual(len(self.ui.store.pending_decision_notices()),1)
        restarted=RefundUI(self.ui.store.path,self.ui.service.base,'fake',lambda t:False)
        self.context.bot.send_message.side_effect=None
        await restarted.reconcile_pending(self.context)
        self.assertEqual(restarted.store.pending_decision_notices(),[])
        self.assertEqual(self.context.bot.send_message.await_args.kwargs['chat_id'],SUPPORT_CHAT)
        self.assertEqual(self.context.bot.send_message.await_count,2)

    async def test_explicit_clarification_reaches_support_but_automatic_validation_does_not(self):
        record,_=self.ui.store.ingest(SUPPORT_CHAT,1,TEXT,100)
        self.ui.store.preparation_error(record['id'],record['revision'],'Нет документа')
        await self.ui.send_decision_notices(self.context)
        self.context.bot.send_message.assert_not_awaited()
        self.ui.store.decision(record['id'],record['revision'],OWNER,'needs_info','Добавьте доставку в документ')
        await self.ui.send_decision_notices(self.context)
        self.context.bot.send_message.assert_awaited_once()
        self.assertIn('Нужно уточнение',self.context.bot.send_message.await_args.kwargs['text'])

    async def test_deleted_source_message_still_delivers_reason_to_the_same_topic(self):
        record,_=self.ui.store.ingest(SUPPORT_CHAT,1,TEXT,100)
        self.ui.store.decision(record['id'],record['revision'],OWNER,'rejected','Исправьте состав')
        self.context.bot.send_message.side_effect=[BadRequest('Message to be replied not found'),SimpleNamespace(message_id=88)]
        await self.ui.send_decision_notices(self.context)
        args=self.context.bot.send_message.await_args.kwargs
        self.assertEqual((args['chat_id'],args['message_thread_id']),(SUPPORT_CHAT,SUPPORT_TOPIC))
        self.assertNotIn('reply_to_message_id',args)
        self.assertEqual(self.ui.store.pending_decision_notices(),[])

    async def test_owner_reason_input_creates_and_delivers_a_notice(self):
        record,_=self.ui.store.ingest(SUPPORT_CHAT,1,TEXT,100)
        self.ui.store.update(record['id'],owner_message_id=99)
        self.ui.pending.add(OWNER)
        self.ui.note_requests[OWNER]={'id':record['id'],'revision':record['revision'],'action':'reject'}
        update=self.update();update.effective_message.text='Не все позиции для полного возврата'
        with self.assertRaises(ApplicationHandlerStop):await self.ui.input(update,self.context)
        self.assertEqual(self.ui.store.get(record['id'])['state'],'rejected')
        self.context.bot.send_message.assert_awaited_once()
        self.assertEqual(self.context.bot.send_message.await_args.kwargs['chat_id'],SUPPORT_CHAT)

    async def test_support_does_not_receive_intermediate_statuses_or_lookup_messages(self):
        record,_=self.ui.store.ingest(SUPPORT_CHAT,1,TEXT,100)
        for state in ['new','ready','needs_info','rejected','pending','uncertain','failed','succeeded']:
            saved=self.ui.store.update(record['id'],state=state,ms_synced=0,last_error='Нужна сверка')
            await self.ui.topic_result(saved,self.context)
            await self.ui.lookup_result(saved,self.context)
        self.context.bot.send_message.assert_not_awaited()
        self.context.bot.send_photo.assert_not_awaited()

    async def test_other_topic_is_ignored_and_right_topic_is_captured_once(self):
        self.ui.prepare_incoming=AsyncMock()
        tasks=[]
        self.context.application.create_task=lambda task,**kw:tasks.append(task)
        message=SimpleNamespace(chat=SimpleNamespace(id=SUPPORT_CHAT),message_thread_id=999,message_id=10,
                                text=TEXT,caption=None,from_user=SimpleNamespace(id=100,is_bot=False))
        update=SimpleNamespace(effective_message=message)
        await self.ui.external(update,self.context)
        self.assertEqual(self.ui.store.list(),[])
        message.message_thread_id=SUPPORT_TOPIC
        for _ in range(2):
            with self.assertRaises(ApplicationHandlerStop): await self.ui.external(update,self.context)
        for task in tasks: await task
        self.assertEqual(len(self.ui.store.list()),1)
        self.ui.prepare_incoming.assert_awaited_once()
        self.context.bot.send_message.assert_awaited_once()

    async def test_support_gets_one_combined_completion_and_owner_receipt_only_on_click(self):
        record,_=self.ui.store.ingest(SUPPORT_CHAT,1,TEXT,100)
        snapshot={"receipt_contact":{"email":"buyer@example.test"}}
        record=self.ui.store.update(record["id"],state="succeeded",snapshot=snapshot,refund_id="refund-result",payment_id=PID,amount_cents=1111200,ms_synced=1,succeeded_at="2026-10-20T10:10:41+00:00",owner_message_id=6)
        self.ui.service.yoo.pages=AsyncMock(return_value=[refund_receipt()])
        # Exercise retrieval and delivery without calling the financial service.
        record=await self.ui.service.receipt(record)
        await self.ui.send_receipt(record,self.context)
        await self.ui.send_receipt(self.ui.store.get(record["id"]),self.context)
        self.context.bot.send_photo.assert_not_awaited()
        await self.ui.topic_result(record,self.context)
        await self.ui.topic_result(record,self.context)
        self.assertEqual(self.context.bot.send_photo.await_count,1)
        calls=self.context.bot.send_photo.await_args_list
        self.assertEqual((calls[0].kwargs["chat_id"],calls[0].kwargs["message_thread_id"],calls[0].kwargs["reply_to_message_id"]),(SUPPORT_CHAT,SUPPORT_TOPIC,1))
        self.assertIn('Возврат-проведен',calls[0].kwargs['caption'])
        self.assertIn('20.10.2026 13:10',calls[0].kwargs['caption'])
        self.assertIn('Чек возврата зарегистрирован',calls[0].kwargs['caption'])
        self.assertNotIn('buyer@example.test',calls[0].kwargs['caption'])
        self.assertTrue(self.ui.store.get(record["id"])["completed_message_id"])
        self.assertNotIn(record['id'],[r['id'] for r in self.ui.store.list(pending=True)])
        self.ui.store.update(record['id'],last_error='Transient error',receipt_status='pending')
        restarted=RefundUI(self.ui.store.path,self.ui.service.base,'fake',lambda t:False)
        await restarted.topic_result(restarted.store.get(record['id']),self.context)
        self.assertEqual(self.context.bot.send_photo.await_count,1)
        await self.ui.send_receipt(self.ui.store.get(record["id"]),self.context,force_owner=True)
        self.assertEqual(self.context.bot.send_photo.await_count,2)
        self.assertEqual(self.context.bot.send_photo.await_args.kwargs["chat_id"],OWNER)

    async def test_final_support_message_waits_for_both_ms_and_receipt(self):
        record,_=self.ui.store.ingest(SUPPORT_CHAT,1,TEXT,100)
        record=self.ui.store.update(record['id'],state='succeeded',ms_synced=1)
        await self.ui.topic_result(record,self.context)
        record=self.ui.store.update(record['id'],ms_synced=0,receipt_json={'id':'registered'})
        await self.ui.topic_result(record,self.context)
        self.context.bot.send_photo.assert_not_awaited()
        self.context.bot.send_message.assert_not_awaited()

    async def test_existing_support_receipt_is_updated_in_place_after_ms_recovery(self):
        record,_=self.ui.store.ingest(SUPPORT_CHAT,1,TEXT,100)
        record=self.ui.store.update(record['id'],state='succeeded',ms_synced=1,receipt_json={'id':'registered'},
            amount_cents=10000,succeeded_at='2026-10-20T10:10:41+00:00',receipt_support_message_id=123)
        await self.ui.topic_result(record,self.context)
        await self.ui.topic_result(record,self.context)
        self.context.bot.edit_message_caption.assert_awaited_once()
        self.context.bot.send_photo.assert_not_awaited()
        self.assertEqual(self.ui.store.get(record['id'])['completed_message_id'],123)

    async def test_yes_updates_confirmation_in_place_and_refresh_checks_existing_operation(self):
        record,_=self.ui.store.ingest(SUPPORT_CHAT,1,TEXT,100)
        record=self.ui.store.update(record['id'],state='ready',amount_cents=10000)
        update=self.update(data=f"cr:execute:{record['id']}:{record['revision']}")
        update.effective_message.message_id=99
        # Simulate the persisted pending provider result, without a financial API.
        async def execute(*args):
            update.callback_query.edit_message_text.assert_awaited_once()
            return self.ui.store.update(record['id'],state='pending')
        self.ui.service.execute=AsyncMock(side_effect=execute)
        await self.ui.dispatch(update,self.context,['cr','execute',str(record['id']),str(record['revision'])])
        self.assertEqual(self.ui.store.get(record['id'])['owner_message_id'],99)
        self.context.bot.send_message.assert_not_awaited()
        self.context.bot.send_photo.assert_not_awaited()
        self.assertIn('Возврат оформлен, в процессе',self.context.bot.edit_message_text.await_args.kwargs['text'])
        keyboard=self.context.bot.edit_message_text.await_args.kwargs['reply_markup']
        self.assertIn('🔄 Обновить',[b.text for row in keyboard.inline_keyboard for b in row])
        self.ui.service.check=AsyncMock(return_value=self.ui.store.get(record['id']))
        await self.ui.dispatch(update,self.context,['cr','check',str(record['id']),str(record['revision'])])
        self.ui.service.check.assert_awaited_once()
        self.ui.service.execute.assert_awaited_once()

    async def test_confirmation_reuses_the_card_instead_of_sending_another_message(self):
        record,_=self.ui.store.ingest(SUPPORT_CHAT,1,TEXT,100)
        record=self.ui.store.update(record['id'],state='ready',snapshot={'verified':True},amount_cents=10000)
        update=self.update()
        with patch('customer_refunds_ui.card',return_value='Проверенный возврат'):
            await self.ui.dispatch(update,self.context,['cr','confirm',str(record['id']),str(record['revision'])])
        update.callback_query.edit_message_text.assert_awaited_once()
        update.effective_message.reply_text.assert_not_awaited()
        self.context.bot.send_message.assert_not_awaited()

    def confirmation_record(self,message_id=1,amount=10000):
        record,_=self.ui.store.ingest(SUPPORT_CHAT,message_id,TEXT,100)
        snapshot=self.gift_snapshot(record);snapshot['amount_cents']=amount;snapshot['previous_refunds']=[]
        return self.ui.store.prepare(record['id'],record['revision'],snapshot)

    async def test_old_proceed_button_immediately_shows_latest_amount_for_approval(self):
        record,_=self.ui.store.ingest(SUPPORT_CHAT,1,TEXT,100)
        current=self.confirmation_record(amount=12345)
        update=self.update();update.effective_message.message_id=99
        self.ui.service.execute=AsyncMock()
        with patch('customer_refunds_ui.card',return_value='Актуальная карточка'):
            await self.ui.dispatch(update,self.context,['cr','confirm',str(record['id']),str(record['revision'])])
        keyboard=update.callback_query.edit_message_text.await_args.kwargs['reply_markup']
        yes=keyboard.inline_keyboard[0][0]
        self.assertIn('123,45',yes.text)
        self.assertEqual(yes.callback_data.split(':')[3],str(current['revision']))
        self.assertLessEqual(len(yes.callback_data.encode()),64)
        self.assertEqual(self.ui.store.get(record['id'])['owner_message_id'],99)
        self.ui.service.execute.assert_not_awaited()
        update.effective_message.reply_text.assert_not_awaited()

    async def test_yes_survives_revision_change_when_verified_financial_data_are_identical(self):
        record=self.confirmation_record()
        update=self.update();update.effective_message.message_id=99
        with patch('customer_refunds_ui.card',return_value='Карточка'):
            await self.ui.dispatch(update,self.context,['cr','confirm',str(record['id']),str(record['revision'])])
        bits=update.callback_query.edit_message_text.await_args.kwargs['reply_markup'].inline_keyboard[0][0].callback_data.split(':')
        current=self.ui.store.prepare(record['id'],record['revision'],record['snapshot'])
        async def execute(number,revision,owner):
            self.assertEqual((number,revision,owner),(record['id'],current['revision'],OWNER))
            self.ui.store.claim(number,revision,owner)
            return self.ui.store.update(number,state='pending')
        self.ui.service.execute=AsyncMock(side_effect=execute)
        self.ui.refresh_outcome=AsyncMock()
        await self.ui.dispatch(update,self.context,bits)
        self.ui.service.execute.assert_awaited_once()
        self.ui.service.check=AsyncMock(return_value=self.ui.store.get(record['id']))
        await self.ui.dispatch(update,self.context,bits)
        self.ui.service.execute.assert_awaited_once()
        self.ui.service.check.assert_awaited_once()
        update.effective_message.reply_text.assert_not_awaited()

    async def test_changed_money_items_payment_or_source_require_fresh_approval_in_place(self):
        for index,field in enumerate(('amount_cents','items','payment_id','document','source_hash'),1):
            record=self.confirmation_record(index)
            update=self.update()
            with patch('customer_refunds_ui.card',return_value='Карточка'):
                await self.ui.dispatch(update,self.context,['cr','confirm',str(record['id']),str(record['revision'])])
                bits=update.callback_query.edit_message_text.await_args.kwargs['reply_markup'].inline_keyboard[0][0].callback_data.split(':')
                snapshot=record['snapshot']
                if field=='document':snapshot['order']['id']='different-document'
                elif field=='items':snapshot['order']['items'][0]['quantity']=2
                elif field=='amount_cents':snapshot[field]=20000
                else:snapshot[field]='changed'
                self.ui.store.prepare(record['id'],record['revision'],snapshot)
                self.ui.service.execute=AsyncMock()
                await self.ui.dispatch(update,self.context,bits)
            self.ui.service.execute.assert_not_awaited()
            self.assertIn('Данные возврата изменились',update.callback_query.edit_message_text.await_args.args[0])
            update.effective_message.reply_text.assert_not_awaited()

    async def test_legacy_stale_yes_refreshes_confirmation_without_sending_money(self):
        record,_=self.ui.store.ingest(SUPPORT_CHAT,1,TEXT,100)
        self.confirmation_record()
        update=self.update();self.ui.service.execute=AsyncMock()
        with patch('customer_refunds_ui.card',return_value='Карточка'):
            await self.ui.dispatch(update,self.context,['cr','execute',str(record['id']),str(record['revision'])])
        update.callback_query.edit_message_text.assert_awaited_once()
        self.ui.service.execute.assert_not_awaited()
        update.effective_message.reply_text.assert_not_awaited()

    def awaiting_receipt(self):
        record,_=self.ui.store.ingest(SUPPORT_CHAT,1,TEXT,100)
        return self.ui.store.update(record['id'],state='succeeded',ms_synced=1,
            owner_message_id=6,snapshot={'receipt_contact':{'email':'buyer@example.test'}},
            refund_id='refund-result',payment_id=PID,amount_cents=1111200,
            succeeded_at='2026-10-20T10:10:41+00:00',receipt_status='pending')

    async def test_background_sends_receipt_as_it_becomes_ready_without_any_button(self):
        record=self.awaiting_receipt()
        self.ui.poll_interval=0.01
        self.ui.service.check=AsyncMock(side_effect=lambda r,**kwargs:r)
        self.ui.service.yoo.pages=AsyncMock(side_effect=[[refund_receipt('pending')],[refund_receipt()]])
        self.ui.send_card=AsyncMock()
        delivered=asyncio.Event()
        async def send_photo(**kwargs):
            self.assertEqual(kwargs['chat_id'],SUPPORT_CHAT)
            delivered.set()
            return SimpleNamespace(message_id=88)
        self.context.bot.send_photo.side_effect=send_photo
        task=asyncio.create_task(self.ui.worker(self.context))
        try:
            await asyncio.wait_for(delivered.wait(),timeout=2)
        finally:
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)
        self.context.bot.send_photo.assert_awaited_once()
        self.assertEqual(self.ui.service.yoo.pages.await_count,2)
        self.assertTrue(self.ui.store.get(record['id'])['completed_message_id'])
        restarted=RefundUI(self.ui.store.path,self.ui.service.base,'fake',lambda t:False)
        restarted.service.check=AsyncMock()
        await restarted.reconcile_pending(self.context)
        restarted.service.check.assert_not_awaited()
        self.context.bot.send_photo.assert_awaited_once()

    async def test_manual_refresh_updates_only_owner_and_leaves_publication_to_worker(self):
        record=self.awaiting_receipt()
        self.ui.service.check=AsyncMock(side_effect=lambda r,**kwargs:r)
        self.ui.service.yoo.pages=AsyncMock(return_value=[refund_receipt()])
        self.ui.send_card=AsyncMock()
        await self.ui.dispatch(self.update(),self.context,['cr','check',str(record['id']),str(record['revision'])])
        self.context.bot.send_photo.assert_not_awaited()
        self.context.bot.send_message.assert_not_awaited()
        self.ui.send_card.assert_awaited_once()
        self.assertIsNone(self.ui.store.get(record['id'])['completed_message_id'])
        await self.ui.reconcile_pending(self.context)
        self.context.bot.send_photo.assert_awaited_once()
        self.assertEqual(self.context.bot.send_photo.await_args.kwargs['chat_id'],SUPPORT_CHAT)

    async def test_private_card_error_does_not_block_background_support_completion(self):
        record=self.awaiting_receipt()
        self.ui.service.check=AsyncMock(side_effect=lambda r,**kwargs:r)
        self.ui.service.yoo.pages=AsyncMock(return_value=[refund_receipt()])
        self.ui.send_card=AsyncMock(side_effect=TelegramError('Private card unavailable'))
        await self.ui.reconcile_pending(self.context)
        self.context.bot.send_photo.assert_awaited_once()
        self.assertTrue(self.ui.store.get(record['id'])['completed_message_id'])

    async def test_background_wakes_immediately_and_survives_a_scan_error(self):
        first=asyncio.Event();second=asyncio.Event()
        async def reconcile(context):
            if not first.is_set():
                first.set()
                raise RuntimeError('Temporary scan error')
            second.set()
        self.ui.reconcile_pending=AsyncMock(side_effect=reconcile)
        self.ui.poll_interval=3600
        task=asyncio.create_task(self.ui.worker(self.context))
        try:
            await asyncio.wait_for(first.wait(),timeout=1)
            self.ui.wakeup.set()
            await asyncio.wait_for(second.wait(),timeout=1)
        finally:
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)
        self.assertEqual(self.ui.reconcile_pending.await_count,2)




if __name__=="__main__":unittest.main()
