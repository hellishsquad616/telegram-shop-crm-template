import copy
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram.ext import ApplicationHandlerStop

from customer_refunds import RefundError, SETTINGS
from database import Database
from gift_scarves import (CHAT_KEY, TOPIC_KEY, COLORS, GiftStore, GiftService, GiftUI,
                          GiftClarification, GiftConfirmation, parse_gift, position_identity)

ORG, DOC, STORE, PRODUCT, PAID = [str(uuid.UUID(int=n)) for n in range(1,6)]
VARIANTS = {color:str(uuid.UUID(int=10+i)) for i,color in enumerate(COLORS)}


def position(ident, color=None, price=0, reserve=1, quantity=1):
    return {"id":ident,"price":price,"quantity":quantity,"reserve":reserve,"discount":0,
            "assortment":{"id":VARIANTS[color] if color else PRODUCT,
                          "name":"DEMO SCARF "+color+" (ONESIZE, Шерсть 50%)" if color else "DEMO PUFFER BLACK (S)",
                          "meta":{"type":"variant"}}}


class FakeMS:
    def __init__(self):
        self.order = {"id":DOC,"name":"order-1010101007","organization":{"id":ORG},
                      "state":{"name":"Оплачен"},"store":{"id":STORE},"applicable":True,"sum":1399000}
        self.positions = [position(PAID,price=1399000)]
        self.products = [{"id":ident,"name":"DEMO SCARF "+color+" (ONESIZE, Шерсть 50%)",
                          "meta":{"type":"variant"},"stock":10,"reserve":1,"quantity":9}
                         for color,ident in VARIANTS.items()]
        self.calls = []
        self.timeout_after_append = False
        self.timeout_before_append = False
        self.ignore_initial_reserve = False
        self.extra_docs = []

    def check_brand(self, order):
        if order.get("organization",{}).get("id") != ORG: raise RefundError("Other brand")

    async def ms(self, method, path, params=None, body=None):
        self.calls.append((method,path,copy.deepcopy(body)))
        if path == "/entity/customerorder": return {"rows":copy.deepcopy([self.order]+self.extra_docs)}
        if path == "/entity/customerorder/"+DOC: return copy.deepcopy(self.order)
        if path == "/entity/assortment": return {"rows":copy.deepcopy(self.products)}
        prefix = "/entity/customerorder/"+DOC+"/positions"
        if method == "GET" and path == prefix: return {"rows":copy.deepcopy(self.positions)}
        if method == "POST" and path == prefix:
            if self.timeout_before_append: raise RefundError("Response timeout")
            color = next(color for color,ident in VARIANTS.items() if body["assortment"]["meta"]["href"].endswith(ident))
            self.positions.append(position(str(uuid.uuid4()),color,price=body["price"],quantity=body["quantity"],reserve=0 if self.ignore_initial_reserve else body["reserve"]))
            next(p for p in self.products if p["id"]==VARIANTS[color])["reserve"] += body["reserve"]
            if self.timeout_after_append: raise RefundError("Response timeout")
            return copy.deepcopy(self.positions[-1])
        if method == "PUT" and path.startswith(prefix+"/"):
            assert set(body)=={"reserve"}
            p = next(p for p in self.positions if p["id"]==path.rsplit("/",1)[1])
            p.update(body); return copy.deepcopy(p)
        raise AssertionError((method,path,body))


class GiftScarfTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = Database(self.root/"crm.sqlite3"); self.db.initialize()
        self.db.set_setting(CHAT_KEY,"-1001"); self.db.set_setting(TOPIC_KEY,"1767")
        self.settings = patch.dict(SETTINGS,{"ms_organization_id":ORG},clear=True)
        self.settings.start(); self.addCleanup(self.settings.stop)
        self.ms = FakeMS()
        self.store = GiftStore(self.root/"gifts.sqlite3")
        self.service = GiftService(self.ms,self.db,self.store)

    def message(self, number=1, text="Order #1010101007\nКрасный шарф", **fields):
        data = dict(chat=SimpleNamespace(id=-1001),message_id=number,message_thread_id=1767,
                    from_user=SimpleNamespace(id=22,is_bot=False),text=text,reply_to_message=None,reply_text=AsyncMock())
        data.update(fields); return SimpleNamespace(**data)

    def request(self, text="Order #1010101007\nКрасный шарф", number=1):
        return self.store.ingest(self.message(number,text),parse_gift(text),text)

    def mutations(self):
        return [c for c in self.ms.calls if c[0] != "GET"]

    def test_parser_supports_colors_before_and_after_scarf_and_exact_second_marker(self):
        for text,expected in [("Красный шарф","RED"),("Шарф (коричневый)","BROWN"),
                              ("Шарф BLACK","BLACK"),("Розовый шарф","PINK"),("Шарф серый","GREY"),
                              ("Шарф, любой цвет",None),("Шарф",None),("Любой шарф",None),("Подарочный шарф",None)]:
            with self.subTest(text=text):
                result = parse_gift("Order #1010101007\nСергеева Анна\n"+text)
                self.assertEqual(result,{"order_number":"1010101007","color":expected,"slot":1})
        self.assertEqual(parse_gift("1010101007\nВторой шарф в подарок - шарф")["slot"],2)
        self.assertEqual(parse_gift("Второй шарф в подарок - красный шарф","Order #1010101007")["color"],"RED")
        self.assertIsNone(parse_gift("Order #1010101007\nПодарок"))
        for bad in ["Шарф бежевый","Бежевый шарф","Голубой шарф","Не красный шарф","Шарф черный или красный","2 шарфа"]:
            with self.subTest(bad=bad), self.assertRaises(GiftClarification):
                parse_gift("1010101007\n"+bad)
        with self.assertRaises(GiftClarification): parse_gift("Order #1010101007 и Order #1010101002\nШарф")

    async def test_adds_zero_price_variant_and_reserve_without_changing_existing_goods(self):
        before = copy.deepcopy(self.ms.positions)
        row = self.request(); saved = await self.service.process(row["id"])
        self.assertEqual(saved["state"],"done")
        self.assertIn("DEMO SCARF RED",saved["result"])
        self.assertEqual(self.ms.positions[:-1],before)
        gift = self.ms.positions[-1]
        self.assertEqual((gift["price"],gift["quantity"],gift["reserve"]),(0,1,1))
        self.assertEqual(len(self.mutations()),1)
        self.assertEqual(self.mutations()[0][0],"POST")
        self.assertTrue(self.mutations()[0][1].endswith("/positions"))
        self.assertEqual(self.ms.order["sum"],1399000)
        self.assertEqual(self.store.path.stat().st_mode & 0o777,0o600)

    async def test_repeat_same_message_and_different_message_do_not_duplicate_first_gift(self):
        row = self.request(); await self.service.process(row["id"])
        again = self.request(); await self.service.process(again["id"])
        other = self.request(number=2); result = await self.service.process(other["id"])
        self.assertEqual(row["id"],again["id"])
        self.assertEqual(len(self.ms.positions),2)
        self.assertEqual(len(self.mutations()),1)
        self.assertIn("уже",result["result"])

    async def test_explicit_second_gift_adds_one_more_even_when_same_color(self):
        first = self.request(); await self.service.process(first["id"])
        second = self.request("Order #1010101007\nВторой шарф в подарок - красный шарф",2)
        pending = await self.service.process(second["id"])
        self.assertEqual(pending["state"],"awaiting_confirmation")
        self.assertTrue(self.store.decide(second["id"],pending["confirmation"]["token"],"yes",22))
        result = await self.service.process(second["id"])
        self.assertEqual(result["state"],"done")
        self.assertIn("дополнительный",result["result"])
        self.assertEqual(len(self.ms.positions),3)
        again = self.request("1010101007\nВторой шарф в подарок - красный шарф",3)
        await self.service.process(again["id"])
        self.assertEqual(len(self.mutations()),2)
        self.assertEqual(sum(p["reserve"] for p in self.ms.positions if p["price"]==0),2)

    async def test_second_gift_requires_first_and_paid_scarf_does_not_count_as_gift(self):
        row = self.request("1010101007\nВторой шарф в подарок - шарф")
        result = await self.service.process(row["id"])
        self.assertEqual(result["state"],"needs_info"); self.assertEqual(self.mutations(),[])
        self.ms.positions.append(position(str(uuid.uuid4()),"RED",price=100000))
        regular = self.request(number=2)
        pending = await self.service.process(regular["id"])
        self.assertEqual(pending["state"],"awaiting_confirmation")
        self.assertTrue(self.store.decide(regular["id"],pending["confirmation"]["token"],"yes",22))
        result = await self.service.process(regular["id"])
        self.assertEqual(result["state"],"done")
        self.assertEqual(len(self.ms.positions),3)
        self.assertEqual(self.ms.positions[-2]["price"],100000)

    async def test_any_color_uses_physical_free_stock_on_order_store_not_incoming_quantity(self):
        for p in self.ms.products: p.update(stock=0,reserve=0,quantity=1000)
        next(p for p in self.ms.products if p["id"]==VARIANTS["BROWN"]).update(stock=5,reserve=2)
        self.ms.products.append({"id":str(uuid.uuid4()),"name":"DEMO SCARF RED","meta":{"type":"product"},"stock":1000,"reserve":0})
        result = await self.service.process(self.request("1010101007\nШарф")["id"])
        self.assertEqual(result["state"],"done"); self.assertIn("BROWN",result["result"])
        self.assertEqual(len(self.mutations()),1)

    async def test_specified_unavailable_color_is_not_replaced_with_another(self):
        next(p for p in self.ms.products if p["id"]==VARIANTS["RED"]).update(stock=1,reserve=1,quantity=100)
        result = await self.service.process(self.request()["id"])
        self.assertEqual(result["state"],"needs_info"); self.assertEqual(self.mutations(),[])
        self.assertIn("нет в свободном остатке",result["result"])

    async def test_existing_free_scarf_prompts_without_changing_it(self):
        gift = position(str(uuid.uuid4()),"RED",reserve=0)
        self.ms.positions.append(gift)
        result = await self.service.process(self.request()["id"])
        self.assertEqual(result["state"],"awaiting_confirmation")
        self.assertEqual(len(self.ms.positions),2)
        self.assertEqual(self.mutations(),[])
        self.assertEqual(gift["reserve"],0)

    async def test_post_timeout_reconciles_success_without_second_post_even_after_restart(self):
        self.ms.timeout_after_append = True
        row = self.request(); result = await self.service.process(row["id"])
        self.assertEqual(result["state"],"done")
        restarted = GiftService(self.ms,self.db,GiftStore(self.store.path))
        result = await restarted.process(row["id"])
        self.assertEqual(result["state"],"done")
        self.assertEqual(len(self.mutations()),1)

    async def test_unconfirmed_post_never_retries_and_duplicate_message_is_blocked_by_durable_slot(self):
        self.ms.timeout_before_append = True
        row = self.request(); result = await self.service.process(row["id"])
        self.assertEqual(result["state"],"uncertain")
        restarted = GiftService(self.ms,self.db,GiftStore(self.store.path))
        await restarted.process(row["id"])
        duplicate = self.request(number=2); result = await restarted.process(duplicate["id"])
        self.assertEqual(result["state"],"needs_info")
        self.assertEqual(len(self.mutations()),1)

    async def test_reserve_ignored_on_create_is_repaired_with_absolute_position_update(self):
        self.ms.ignore_initial_reserve = True
        result = await self.service.process(self.request()["id"])
        self.assertEqual(result["state"],"done")
        self.assertEqual([c[0] for c in self.mutations()],["POST","PUT"])
        self.assertEqual(self.ms.positions[-1]["reserve"],1)

    async def test_wrong_brand_return_delivered_and_missing_store_are_not_mutated(self):
        original = copy.deepcopy(self.ms.order)
        for changes in [{"organization":{"id":STORE}}, {"name":"Возврат-1010101007"},
                        {"state":{"name":"Выдан в шоу-руме"}}, {"state":{"name":"Возврат-проведен"}},
                        {"applicable":False}, {"store":{}}]:
            with self.subTest(changes=changes):
                self.ms.order = dict(original,**changes)
                row = self.request(number=20+len(self.store.queue()))
                result = await self.service.process(row["id"])
                self.assertEqual(result["state"],"needs_info")
                self.assertEqual(self.mutations(),[])

    async def test_ambiguous_documents_or_color_variants_require_clarification(self):
        self.ms.extra_docs = [dict(self.ms.order,id=str(uuid.uuid4()))]
        result = await self.service.process(self.request()["id"])
        self.assertEqual(result["state"],"needs_info"); self.assertEqual(self.mutations(),[])
        self.ms.extra_docs = []
        self.ms.products.append(dict(next(p for p in self.ms.products if p["id"]==VARIANTS["RED"]),id=str(uuid.uuid4())))
        result = await self.service.process(self.request(number=2)["id"])
        self.assertEqual(result["state"],"needs_info"); self.assertEqual(self.mutations(),[])

    async def test_source_edit_before_claim_cannot_append_stale_request(self):
        record = self.request(); plan,_ = await self.service.prepare(record)
        self.request("1010101007\nШарф розовый")
        with self.assertRaises(GiftClarification): self.store.claim(record,plan)
        self.assertEqual(self.mutations(),[])
        result = await self.service.process(record["id"])
        self.assertIn("PINK",result["result"])

    async def test_source_edit_after_attempt_never_changes_existing_gift(self):
        record = self.request(); await self.service.process(record["id"])
        updated = self.request("1010101007\nШарф розовый")
        self.assertEqual(updated["request"]["color"],"RED")
        self.assertTrue(updated["edited_after_attempt"])
        await self.service.process(record["id"])
        self.assertEqual(len(self.mutations()),1)

    def test_snapshot_numeric_format_is_stable(self):
        first = position(PAID,price=1399000)
        other = dict(first,price=1399000.0,quantity=1.0,discount=0.0)
        self.assertEqual(position_identity(first),position_identity(other))

    async def test_ui_processes_only_gift_topic_and_immediately_defers_work(self):
        ui = GiftUI(self.ms,self.db,self.root/"ui.sqlite3")
        tasks=[]
        context = SimpleNamespace(application=SimpleNamespace(create_task=lambda c,**k:tasks.append(c)),bot=SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=100)),edit_message_text=AsyncMock()))
        for message in [self.message(message_thread_id=102), self.message(chat=SimpleNamespace(id=-1002)),
                        self.message(from_user=SimpleNamespace(id=1,is_bot=True))]:
            await ui.external(SimpleNamespace(message=message,edited_message=None),context)
        self.assertEqual(tasks,[])
        with self.assertRaises(ApplicationHandlerStop):
            await ui.external(SimpleNamespace(message=self.message(),edited_message=None),context)
        self.assertEqual(self.ms.calls,[])
        await tasks[0]
        self.assertEqual(context.bot.send_message.await_count,1)
        self.assertEqual(context.bot.send_message.call_args.kwargs["message_thread_id"],1767)
        self.assertEqual(context.bot.send_message.call_args.kwargs["reply_to_message_id"],1)
        # Confirmation is delivered only after reading the created position,
        # rather than trusting the response to POST alone.
        post_index = next(i for i,c in enumerate(self.ms.calls) if c[0]=="POST")
        self.assertTrue(any(c[0]=="GET" and c[1].endswith("/positions") for c in self.ms.calls[post_index+1:]))
        record=ui.store.get(1)
        await ui.notify(record,context.bot)
        self.assertEqual(context.bot.send_message.await_count,1)
        self.assertEqual(context.bot.edit_message_text.await_count,1)

    async def test_internal_owner_check_has_no_telegram_delivery(self):
        ui = GiftUI(self.ms,self.db,self.root/"ui.sqlite3")
        msg = self.message(chat=SimpleNamespace(id=0),message_thread_id=0)
        row = ui.store.ingest(msg,parse_gift(msg.text),msg.text)
        bot = SimpleNamespace(send_message=AsyncMock(),edit_message_text=AsyncMock())
        await ui.run(row["id"],bot)
        self.assertEqual(ui.store.get(row["id"])["state"],"done")
        self.assertEqual(ui.store.get(row["id"])["notify_pending"],0)
        bot.send_message.assert_not_awaited()

    async def test_refresh_of_manually_added_gift_rechecks_live_without_readding_if_removed(self):
        manual = position(str(uuid.uuid4()),"RED")
        self.ms.positions.append(manual)
        row = self.request()
        self.store.finish(row,state="done",result="Historical verified gift",notify_pending=0)
        result = await self.service.process(row["id"])
        self.assertEqual(result["state"],"done")
        self.assertIsNone(result["attempted_at"])
        self.ms.calls.clear()
        self.store.retry(row["id"])
        result = await self.service.process(row["id"])
        self.assertTrue(any(c[0]=="GET" and c[1].endswith("/positions") for c in self.ms.calls))
        self.assertIn("резерв подтверждён",result["result"])
        self.ms.positions.remove(manual)
        for _ in range(2):
            self.store.retry(row["id"])
            result = await self.service.process(row["id"])
            self.assertIn("не подтверждены",result["result"])
            self.assertEqual(self.mutations(),[])

    async def test_missing_reserve_cannot_produce_success_reply(self):
        self.ms.ignore_initial_reserve = True
        original = self.ms.ms
        async def ignore_reserve(method,path,**kwargs):
            if method=="PUT": return {}
            return await original(method,path,**kwargs)
        self.ms.ms = ignore_reserve
        result = await self.service.process(self.request()["id"])
        self.assertEqual(result["state"],"uncertain")
        self.assertIn("резерв ещё не подтверждён",result["result"])
        self.assertNotIn("резерв подтверждён",result["result"])

    async def test_auto_color_matches_first_item_even_when_pink_has_more_stock(self):
        next(p for p in self.ms.products if p['id']==VARIANTS['PINK'])['stock'] = 1000
        self.ms.positions[0]['assortment']['name'] = 'DEMOZIP ZIP BROWN (M, КОРИЧНЕВЫЙ)'
        self.ms.positions.append(dict(position(str(uuid.uuid4()), price=1000),assortment={'id':PRODUCT,'name':'DEMO PUFFER RED (S)','meta':{'type':'variant'}}))
        result = await self.service.process(self.request('1010101007\nШарф')['id'])
        self.assertEqual(result['state'],'done')
        self.assertIn('DEMO SCARF BROWN',result['result'])
        self.assertEqual(result['plan']['color_choice'],'item')

    async def test_auto_color_skips_services_and_scarves_and_stops_at_first_unknown_color(self):
        self.ms.positions.insert(0,position(str(uuid.uuid4()),'RED',price=500))
        self.ms.positions.insert(0,dict(position(str(uuid.uuid4()),price=500),assortment={'id':PRODUCT,'name':'CDEK','meta':{'type':'service'}}))
        record=self.request('1010101007\nШарф')
        pending=await self.service.process(record['id'])
        self.store.decide(record['id'],pending['confirmation']['token'],'yes',22)
        plan,_=await self.service.prepare(self.store.get(record['id']))
        self.assertEqual(plan['label'],'DEMO SCARF BLACK')
        self.ms.positions[2]['assortment']['name']='DEMO PUFFER BURGUNDY (M)'
        self.ms.positions.append(dict(position(str(uuid.uuid4()),price=500),assortment={'id':PRODUCT,'name':'DEMO PUFFER BROWN (S)','meta':{'type':'variant'}}))
        plan,_=await self.service.prepare(record)
        self.assertEqual(plan['color_choice'],'rotation')
        self.assertEqual(plan['label'],'DEMO SCARF BLACK')

    async def test_fallback_rotation_survives_restart_skips_unavailable_and_preview_does_not_advance(self):
        self.ms.positions[0]['assortment']['name']='DEMO PUFFER WHITE (S)'
        next(p for p in self.ms.products if p['id']==VARIANTS['GREY']).update(stock=0,reserve=0)
        record=self.request('1010101007\nШарф')
        plan,_=await self.service.prepare(record)
        again,_=await self.service.prepare(record)
        self.assertEqual(plan,again)
        self.assertIsNone(self.store.rotation_color(STORE))
        saved=await self.service.process(record['id'])
        self.assertIn('BLACK',saved['result'])
        restarted=GiftService(self.ms,self.db,GiftStore(self.store.path))
        second=self.request('1010101007\nВторой шарф в подарок - шарф',2)
        pending=await restarted.process(second['id'])
        self.store.decide(second['id'],pending['confirmation']['token'],'yes',22)
        saved=await restarted.process(second['id'])
        self.assertIn('DEMO SCARF RED',saved['result'])
        self.assertEqual(self.store.rotation_color(STORE),'RED')
        self.assertEqual(len(self.mutations()),2)
        self.assertEqual([p['assortment']['name'].split(' (')[0] for p in self.ms.positions[1:]],['DEMO SCARF BLACK','DEMO SCARF RED'])

    async def test_missing_matching_stock_rotates_and_explicit_color_still_wins(self):
        next(p for p in self.ms.products if p['id']==VARIANTS['BLACK']).update(stock=1,reserve=1)
        plan,_=await self.service.prepare(self.request('1010101007\nШарф'))
        self.assertEqual(plan['color_choice'],'rotation')
        self.assertEqual(plan['label'],'DEMO SCARF GREY')
        plan,_=await self.service.prepare(self.request('1010101007\nШарф розовый',2))
        self.assertEqual(plan['label'],'DEMO SCARF PINK')
        self.assertEqual(plan['color_choice'],'explicit')

    async def test_unknown_post_accepts_one_manually_added_other_color_without_second_post(self):
        self.ms.timeout_before_append=True
        row=self.request('1010101007\nШарф')
        self.assertEqual((await self.service.process(row['id']))['state'],'uncertain')
        self.ms.positions.append(position(str(uuid.uuid4()),'BROWN',reserve=0))
        result=await self.service.process(row['id'])
        self.assertEqual(result['state'],'done')
        self.assertIn('DEMO SCARF BROWN',result['result'])
        self.assertEqual(self.ms.positions[-1]['reserve'],1)
        self.assertEqual([c[0] for c in self.mutations()],['POST','PUT'])
        await self.service.process(row['id'])
        self.assertEqual(len(self.mutations()),2)

    async def test_unknown_post_does_not_accept_wrong_explicit_color_or_two_new_gifts(self):
        self.ms.timeout_before_append=True
        row=self.request()
        await self.service.process(row['id'])
        self.ms.positions.append(position(str(uuid.uuid4()),'BROWN'))
        result=await self.service.process(row['id'])
        self.assertEqual(result['state'],'uncertain')
        self.assertIn('Найдены подарки',result['result'])
        self.ms.positions=self.ms.positions[:1]
        row=self.request('1010101007\nШарф',2)
        # A separate isolated journal models an old uncertain automatic request.
        isolated=GiftStore(self.root/'isolated.sqlite3')
        svc=GiftService(self.ms,self.db,isolated)
        row=isolated.ingest(self.message(),parse_gift('1010101007\nШарф'),'1010101007\nШарф')
        await svc.process(row['id'])
        self.ms.positions += [position(str(uuid.uuid4()),'BROWN'),position(str(uuid.uuid4()),'RED')]
        result=await svc.process(row['id'])
        self.assertEqual(result['state'],'uncertain')
        self.assertEqual(len([c for c in self.mutations() if c[0]=='POST']),2)

    async def test_refresh_callback_updates_same_message_with_progress_and_new_check_number(self):
        ui=GiftUI(self.ms,self.db,self.root/'ui.sqlite3')
        tasks=[]
        bot=SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=100)),edit_message_text=AsyncMock())
        context=SimpleNamespace(application=SimpleNamespace(create_task=lambda c,**k:tasks.append(c)),bot=bot)
        row=ui.store.ingest(self.message(),parse_gift(self.message().text),self.message().text)
        await ui.run(row['id'],bot)
        query=SimpleNamespace(data='gift:check:'+str(row['id']),message=self.message(number=100),answer=AsyncMock())
        for _ in range(2):
            before=len(self.ms.calls)
            with self.assertRaises(ApplicationHandlerStop): await ui.callback(SimpleNamespace(callback_query=query),context)
            self.assertEqual(len(self.ms.calls),before)
            await tasks.pop()
            self.assertIn('проверяю подарок',bot.edit_message_text.call_args_list[-2].kwargs['text'])
            self.assertIn('резерв подтверждён',bot.edit_message_text.call_args.kwargs['text'])
        self.assertIn('Проверка №3',bot.edit_message_text.call_args.kwargs['text'])
        self.assertEqual(bot.send_message.await_count,1)
        self.assertEqual(len(self.mutations()),1)
        ui.inflight.add(row['id'])
        with self.assertRaises(ApplicationHandlerStop): await ui.callback(SimpleNamespace(callback_query=query),context)
        self.assertEqual(tasks,[])
        self.assertIn('уже выполняется',query.answer.call_args.args[0])

    async def test_unchanged_background_check_is_silent_and_deleted_reply_is_replaced(self):
        from telegram.error import BadRequest
        ui=GiftUI(self.ms,self.db,self.root/'ui.sqlite3')
        bot=SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=100)),edit_message_text=AsyncMock())
        self.ms.timeout_before_append=True
        row=ui.store.ingest(self.message(),parse_gift(self.message().text),self.message().text)
        await ui.run(row['id'],bot)
        await ui.run(row['id'],bot)
        bot.edit_message_text.assert_not_awaited()
        ui.store.retry(row['id'])
        bot.edit_message_text.side_effect=BadRequest('Message to edit not found')
        await ui.run(row['id'],bot)
        self.assertEqual(bot.send_message.await_count,2)
        self.assertEqual(ui.store.get(row['id'])['notify_pending'],0)
        self.assertEqual(len(self.mutations()),1)

    def test_pending_replies_are_not_starved_by_twenty_old_uncertain_rows(self):
        for number in range(1,23):
            row=self.request(number=number)
            self.store.update(row['id'],state='uncertain',notify_pending=0)
        latest=self.request(number=23)
        self.assertEqual(self.store.queue()[0]['id'],latest['id'])



    async def test_unavailable_explicit_color_requests_customer_agreement_without_substitution(self):
        next(p for p in self.ms.products if p['id']==VARIANTS['RED']).update(stock=10,reserve=10,quantity=100)
        row=self.request()
        result=await self.service.process(row['id'])
        self.assertEqual(result['state'],'needs_info')
        self.assertIn('красный (RED)',result['result'])
        self.assertIn('Напишите клиенту',result['result'])
        self.assertIn('Доступны:',result['result'])
        self.assertIn('новую заявку',result['result'])
        self.assertNotIn('Доступны: красный',result['result'])
        self.assertEqual(self.mutations(),[])
        changed=self.request('1010101007\nШарф серый')
        result=await self.service.process(changed['id'])
        self.assertEqual(result['state'],'done')
        self.assertIn('DEMO SCARF GREY',result['result'])
        self.assertEqual(len(self.mutations()),1)

    async def test_existing_wrong_gift_checks_requested_stock_and_explains_customer_agreement(self):
        self.ms.positions.append(position(str(uuid.uuid4()),'BROWN'))
        next(p for p in self.ms.products if p['id']==VARIANTS['RED']).update(stock=0,reserve=0)
        row=self.request()
        result=await self.service.process(row['id'])
        self.assertEqual(result['state'],'needs_info')
        self.assertIn('Напишите клиенту',result['result'])
        self.assertIn('Сейчас в заказе: DEMO SCARF BROWN',result['result'])
        self.assertEqual(self.mutations(),[])
        next(p for p in self.ms.products if p['id']==VARIANTS['RED']).update(stock=2,reserve=0)
        self.store.retry(row['id'])
        result=await self.service.process(row['id'])
        self.assertEqual(result['state'],'awaiting_confirmation')
        self.assertIn('Добавить ещё один?',result['result'])
        self.assertEqual(self.mutations(),[])

    async def test_no_stock_does_not_promise_an_available_alternative(self):
        for item in self.ms.products: item.update(stock=0,reserve=0)
        result=await self.service.process(self.request()['id'])
        self.assertIn('Других свободных цветов тоже нет',result['result'])
        self.assertNotIn('Доступны:',result['result'])
        self.assertEqual(self.mutations(),[])

    async def test_existing_brown_needs_yes_then_adds_requested_color_without_replacing(self):
        brown=position(str(uuid.uuid4()),'BROWN')
        self.ms.positions.append(brown)
        before=copy.deepcopy(self.ms.positions)
        row=self.request('1010101007\nШарф серый')
        pending=await self.service.process(row['id'])
        self.assertEqual(pending['state'],'awaiting_confirmation')
        self.assertEqual(self.mutations(),[])
        token=pending['confirmation']['token']
        self.assertTrue(self.store.decide(row['id'],token,'yes',22))
        self.assertFalse(self.store.decide(row['id'],token,'yes',23))
        self.assertFalse(self.store.decide(row['id'],token,'no',23))
        result=await GiftService(self.ms,self.db,GiftStore(self.store.path)).process(row['id'])
        self.assertEqual(result['state'],'done')
        self.assertEqual(result['slot'],2)
        self.assertEqual(self.ms.positions[:2],before)
        self.assertEqual(self.ms.positions[-1]['assortment']['id'],VARIANTS['GREY'])
        self.assertEqual(self.ms.positions[-1]['reserve'],1)
        self.assertEqual(self.store.get(row['id'])['confirmation']['actor'],22)
        await self.service.process(row['id'])
        self.assertEqual(len(self.mutations()),1)
        self.assertEqual(self.mutations()[0][0],'POST')

    async def test_no_is_durable_and_removes_decision_buttons_for_everyone(self):
        self.ms.positions.append(position(str(uuid.uuid4()),'BROWN'))
        ui=GiftUI(self.ms,self.db,self.root/'ui.sqlite3')
        row=ui.store.ingest(self.message(),parse_gift(self.message().text),self.message().text)
        pending=await ui.service.process(row['id'])
        bot=SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=100)),edit_message_text=AsyncMock())
        await ui.notify(pending,bot)
        keyboard=bot.send_message.call_args.kwargs['reply_markup'].inline_keyboard
        self.assertEqual([b.text for b in keyboard[0]],['Да, добавить ещё один','Нет'])
        tasks=[]
        ctx=SimpleNamespace(bot=bot,application=SimpleNamespace(create_task=lambda c,**k:tasks.append(c)))
        query=SimpleNamespace(data=keyboard[0][1].callback_data,message=self.message(number=100),from_user=SimpleNamespace(id=22),answer=AsyncMock())
        with self.assertRaises(ApplicationHandlerStop): await ui.callback(SimpleNamespace(callback_query=query),ctx)
        await tasks.pop()
        self.assertEqual(ui.store.get(row['id'])['state'],'declined')
        self.assertIsNone(bot.edit_message_text.call_args.kwargs['reply_markup'])
        self.assertIn('Существующий шарф оставлен',bot.edit_message_text.call_args.kwargs['text'])
        query.data=keyboard[0][0].callback_data
        with self.assertRaises(ApplicationHandlerStop): await ui.callback(SimpleNamespace(callback_query=query),ctx)
        self.assertEqual(tasks,[])
        ui.store.retry(row['id'])
        await GiftService(self.ms,self.db,GiftStore(ui.store.path)).process(row['id'])
        self.assertEqual(self.mutations(),[])

    async def test_yes_callback_updates_shared_message_and_repeated_click_cannot_add_again(self):
        self.ms.positions.append(position(str(uuid.uuid4()),'BROWN'))
        ui=GiftUI(self.ms,self.db,self.root/'ui.sqlite3')
        msg=self.message()
        row=ui.store.ingest(msg,parse_gift(msg.text),msg.text)
        bot=SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=100)),edit_message_text=AsyncMock())
        await ui.run(row['id'],bot)
        data=bot.send_message.call_args.kwargs['reply_markup'].inline_keyboard[0][0].callback_data
        tasks=[]
        ctx=SimpleNamespace(bot=bot,application=SimpleNamespace(create_task=lambda c,**k:tasks.append(c)))
        query=SimpleNamespace(data=data,message=self.message(number=100),from_user=SimpleNamespace(id=22),answer=AsyncMock())
        for _ in range(2):
            with self.assertRaises(ApplicationHandlerStop): await ui.callback(SimpleNamespace(callback_query=query),ctx)
        self.assertEqual(len(tasks),1)
        self.assertEqual(self.mutations(),[])
        await tasks.pop()
        self.assertEqual(bot.send_message.await_count,1)
        self.assertIn('дополнительный',bot.edit_message_text.call_args.kwargs['text'])
        self.assertEqual([b.text for b in bot.edit_message_text.call_args.kwargs['reply_markup'].inline_keyboard[0]],['🔄 Проверить'])
        self.assertEqual(len(self.mutations()),1)
        self.assertEqual(len(self.ms.positions),3)

    async def test_source_edit_invalidates_old_confirmation_and_new_snapshot_requires_new_yes(self):
        self.ms.positions.append(position(str(uuid.uuid4()),'BROWN'))
        row=self.request()
        pending=await self.service.process(row['id'])
        old=pending['confirmation']['token']
        self.request('1010101007\nШарф серый')
        self.assertFalse(self.store.decide(row['id'],old,'yes',22))
        pending=await self.service.process(row['id'])
        self.assertNotEqual(pending['confirmation']['token'],old)
        self.store.decide(row['id'],pending['confirmation']['token'],'yes',22)
        self.ms.positions[-1]['quantity']=2
        result=await self.service.process(row['id'])
        self.assertEqual(result['state'],'needs_info')
        self.assertEqual(self.mutations(),[])

    async def test_two_pending_requests_cannot_add_a_third_free_scarf(self):
        self.ms.positions.append(position(str(uuid.uuid4()),'BROWN'))
        first=self.request(number=1);second=self.request(number=2)
        for row in [first,second]:
            pending=await self.service.process(row['id'])
            self.store.decide(row['id'],pending['confirmation']['token'],'yes',22)
        self.assertEqual((await self.service.process(first['id']))['state'],'done')
        self.assertEqual((await self.service.process(second['id']))['state'],'needs_info')
        self.assertEqual(len(self.mutations()),1)
        self.assertEqual(len(self.ms.positions),3)

    async def test_timeout_after_confirmed_extra_never_adds_a_third_scarf_on_refresh(self):
        self.ms.positions.append(position(str(uuid.uuid4()),'BROWN'))
        row=self.request()
        pending=await self.service.process(row['id'])
        self.store.decide(row['id'],pending['confirmation']['token'],'yes',22)
        self.ms.timeout_after_append=True
        result=await self.service.process(row['id'])
        self.assertEqual(result['state'],'done')
        await GiftService(self.ms,self.db,GiftStore(self.store.path)).process(row['id'])
        self.assertEqual(len(self.mutations()),1)
        self.assertEqual(len(self.ms.positions),3)


if __name__ == "__main__": unittest.main()
