import copy
import tempfile
import unittest
import warnings
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram.ext import ApplicationHandlerStop, ConversationHandler
from telegram import Chat, Message, MessageEntity, Update, User

import bot
from customer_refunds import RefundError
from database import Database
from order_check import ACTIVE, MENU_CHECK_ORDER, OrderCheck, chunks, register_order_check
from refund_settings import SETTINGS


ORG, DOC, STORE, ITEM, ITEM2 = [f"00000000-0000-4000-8000-{i:012d}" for i in range(1, 6)]


def document(**changes):
    value = {"id": DOC, "name": "order-1234567890", "organization": {"id": ORG},
             "state": {"name": "Отменен"}, "store": {"id": STORE, "name": "Основной склад"},
             "attributes": [{"name": "Пометка", "value": {"name": "ДЕМО ПЕРВАЯ ПАРТИЯ"}}]}
    value.update(changes)
    return value


def position(ident=ITEM, quantity=1, reserve=0, kind="variant", name="DEMO PUFFER RED (M)"):
    return {"id": ident, "quantity": quantity, "reserve": reserve,
            "assortment": {"id": ident, "name": name, "meta": {"type": kind}}}


class OrderCheckTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        mapping = patch.dict("support_order_info.SHIPMENT_TEXT", {
            "ДЕМО ПЕРВАЯ ПАРТИЯ": "Заказ относится к первой партии, отправка в течение недели",
        }, clear=True)
        mapping.start(); self.addCleanup(mapping.stop)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.database = Database(Path(temp.name) / "crm.sqlite3")
        self.database.initialize()
        self.database.ensure_admins([1])
        self.database.set_staff_role(2, "blocked", 1)
        setting = patch.dict(SETTINGS, {"ms_organization_id": ORG}, clear=True)
        setting.start()
        self.addCleanup(setting.stop)
        self.doc = document()
        self.positions = [position()]
        self.stock = {ITEM: {"id": ITEM, "stock": 3, "reserve": 1, "quantity": 102, "inTransit": 100}}
        self.fail_stock = False
        self.calls = []
        self.service = SimpleNamespace(ms=AsyncMock(side_effect=self.ms), check_brand=self.brand)
        self.ui = OrderCheck(self.service, self.database, lambda text: text in {MENU_CHECK_ORDER, bot.MENU_HOME})
        self.tasks = []
        self.context = SimpleNamespace(user_data={}, application=SimpleNamespace(
            bot_data={"database": self.database, "order_check": self.ui},
            create_task=lambda coro, **kwargs: self.tasks.append(coro)))

    def brand(self, order):
        if order.get("organization", {}).get("id") != ORG:
            raise RefundError("Wrong brand")

    async def ms(self, method, path, params=None):
        self.assertEqual(method, "GET")
        self.calls.append((path, params))
        if path == "/entity/customerorder":
            return {"rows": [copy.deepcopy(self.doc)]}
        if path == "/entity/customerorder/" + DOC:
            return copy.deepcopy(self.doc)
        if path.endswith("/positions"):
            return {"rows": copy.deepcopy(self.positions)}
        if path == "/entity/assortment":
            if self.fail_stock:
                raise RefundError("HTTP 503")
            self.assertIn("stockStore=", params["filter"])
            ident = params["filter"].split(";", 1)[0].split("=", 1)[1]
            return {"rows": [copy.deepcopy(self.stock[ident])] if ident in self.stock else []}
        raise AssertionError(path)

    def update(self, text="1234567890", user=1, chat_type="private", callback=None):
        message = SimpleNamespace(text=text, message_id=1, chat=SimpleNamespace(id=user, type=chat_type), reply_text=AsyncMock())
        query = SimpleNamespace(id="q1", data=callback, message=message, answer=AsyncMock()) if callback else None
        return SimpleNamespace(message=message if not callback else None, effective_message=message,
                               effective_user=SimpleNamespace(id=user), callback_query=query)

    async def test_card_shows_live_status_shipping_and_exact_free_stock_not_inbound(self):
        text, keyboard = await self.ui.result(("number", "1234567890"))
        self.assertIn("Статус МС: Отменен", text)
        self.assertIn("отправка в течение недели", text)
        self.assertIn("DEMO PUFFER RED (M)", text)
        self.assertIn("Есть свободный остаток: 2 шт.", text)
        self.assertNotIn("102 шт.", text)
        self.assertEqual(keyboard.inline_keyboard[0][0].callback_data, "ock:" + DOC)
        self.assertEqual(next(params["filter"] for path, params in self.calls if path == "/entity/assortment"),
                         "id=" + ITEM + ";stockStore=https://api.moysklad.ru/api/remap/1.2/entity/store/" + STORE)

    async def test_reserved_stock_and_repeated_positions_are_not_double_counted(self):
        self.positions = [position(quantity=1, reserve=1), position(quantity=2, reserve=1),
                          position(ITEM2, kind="service", name="Доставка СДЭК")]
        self.stock[ITEM].update(stock=2, reserve=3)
        text = await self.ui.card(self.doc)
        self.assertIn("нужно 3 шт.", text)
        self.assertIn("Недостаточно свободного остатка: 0 шт.", text)
        self.assertIn("Резерв этого заказа: 2 шт.", text)
        self.assertNotIn("Доставка СДЭК", text)
        self.assertEqual(sum(path == "/entity/assortment" for path, _ in self.calls), 1)

    async def test_unknown_stock_is_not_reported_as_zero_and_status_survives_api_failure(self):
        for stock in [{}, {"stock": None, "reserve": 0}, {"stock": 1}, {"stock": "NaN", "reserve": 0}]:
            self.stock[ITEM] = {"id": ITEM, **stock}
            text = await self.ui.card(self.doc)
            self.assertIn("Остаток: не удалось проверить", text)
            self.assertNotIn("остатка: 0", text)
        self.fail_stock = True
        text = await self.ui.card(self.doc)
        self.assertIn("Статус МС: Отменен", text)
        self.assertIn("не удалось проверить", text)

    async def test_missing_warehouse_and_mismatched_variant_never_look_available(self):
        text = await self.ui.card(document(store={}))
        self.assertIn("не выбран склад", text)
        self.assertFalse(any(path == "/entity/assortment" for path, _ in self.calls))
        self.stock[ITEM] = {"id": ITEM2, "stock": 100, "reserve": 0}
        self.assertIn("точная модификация не найдена", await self.ui.card(self.doc))

    async def test_shipping_preserves_raw_labels_and_does_not_invent_dates(self):
        self.assertIn("Срок отправки: в МС не указан.", await self.ui.card(document(attributes=[])))
        raw = document(attributes=[{"name": "Пометка", "value": {"name": "Отправки СДЭК 01.11"}}])
        self.assertIn("пометка МС): Отправки СДЭК 01.11", await self.ui.card(raw))
        planned = await self.ui.card(document(attributes=[], deliveryPlannedMoment="2026-11-20 15:30:00.000"))
        self.assertIn("Плановая дата отгрузки в МС: 20.11.2026 15:30", planned)
        self.assertNotIn("Срок отправки: в МС не указан", planned)

    async def test_different_brand_refused_before_positions_or_stock(self):
        self.doc["organization"] = {"id": ITEM2}
        text, keyboard = await self.ui.result(("number", "1234567890"))
        self.assertIn("не найден", text)
        self.assertIsNone(keyboard)
        with self.assertRaises(RefundError):
            await self.ui.card(self.doc)
        self.assertEqual(len(self.calls), 1)

    async def test_multiple_documents_offer_choices_and_callback_reloads(self):
        self.ui.search = AsyncMock(return_value=[self.doc, document(id=ITEM2, name="Возврат-1234567890")])
        text, keyboard = await self.ui.result(("number", "1234567890"))
        self.assertIn("несколько документов", text)
        self.assertEqual(len(keyboard.inline_keyboard), 2)
        self.assertTrue(all(row[0].callback_data.startswith("ock:") for row in keyboard.inline_keyboard))
        update = self.update(callback="ock:" + DOC)
        with self.assertRaises(ApplicationHandlerStop):
            await self.ui.callback(update, self.context)
        self.doc["state"]["name"] = "Доставлен"
        await self.tasks.pop()
        self.assertIn("Статус МС: Доставлен", update.effective_message.reply_text.call_args.args[0])

    async def test_mode_requires_staff_private_chat_and_clears_refund_text_input(self):
        refunds = SimpleNamespace(pending={1}, note_requests={1: {"action": "reject"}})
        self.context.application.bot_data["customer_refunds_ui"] = refunds
        for update in [self.update(user=2), self.update(chat_type="group")]:
            await self.ui.begin(update, self.context)
            self.assertNotIn(ACTIVE, self.context.user_data)
        await self.ui.begin(self.update(), self.context)
        self.assertTrue(self.context.user_data[ACTIVE])
        self.assertFalse(refunds.pending)
        self.assertFalse(refunds.note_requests)

    async def test_lookup_is_background_number_only_and_leaving_restores_navigation(self):
        update = self.update()
        await self.ui.receive(update, self.context)
        self.assertEqual(self.tasks, [])
        await self.ui.begin(update, self.context)
        invalid = self.update(text="Иванова Анна")
        with self.assertRaises(ApplicationHandlerStop):
            await self.ui.receive(invalid, self.context)
        self.assertEqual(self.tasks, [])
        with self.assertRaises(ApplicationHandlerStop):
            await self.ui.receive(update, self.context)
        self.service.ms.assert_not_awaited()
        await self.tasks.pop()
        self.assertFalse(self.ui.pending)
        for text in [bot.MENU_HOME, "/cancel", MENU_CHECK_ORDER]:
            self.context.user_data[ACTIVE] = True
            await self.ui.receive(self.update(text=text), self.context)
            self.assertNotIn(ACTIVE, self.context.user_data)
        self.context.user_data[ACTIVE] = True
        await self.ui.leave(self.update(), self.context)
        self.assertNotIn(ACTIVE, self.context.user_data)

    async def test_revoked_permission_and_forged_group_callback_do_not_call_moysklad(self):
        self.context.user_data[ACTIVE] = True
        with self.assertRaises(ApplicationHandlerStop):
            await self.ui.receive(self.update(user=2), self.context)
        for update in [self.update(user=2, callback="ock:" + DOC),
                       self.update(chat_type="group", callback="ock:" + DOC)]:
            with self.assertRaises(ApplicationHandlerStop):
                await self.ui.callback(update, self.context)
        self.service.ms.assert_not_awaited()

    async def test_menu_opens_check_and_ends_old_conversation(self):
        update = self.update(text=MENU_CHECK_ORDER)
        result = await bot.persistent_menu_button(update, self.context)
        self.assertEqual(result, ConversationHandler.END)
        self.assertTrue(self.context.user_data[ACTIVE])
        self.assertIn("номер заказа", update.effective_message.reply_text.call_args.args[0])
        for allowed, expected in [(True, True), (False, False)]:
            staff = dict(self.database.get_staff(1), can_check_order=allowed)
            labels = [b.text for row in bot.main_reply_keyboard(staff).keyboard for b in row]
            self.assertEqual(MENU_CHECK_ORDER in labels, expected)

    def test_long_cards_are_split_without_losing_positions(self):
        text = "\n".join("Вещь " + str(i) + " " + "x" * 200 for i in range(80))
        parts = list(chunks(text))
        self.assertTrue(all(len(part) <= 3800 for part in parts))
        self.assertEqual("\n".join(parts), text)

    def test_registration_precedes_generic_callbacks_and_refund_input(self):
        from telegram.ext import Application, CallbackQueryHandler
        app = Application.builder().token("123456:test").build()
        register_order_check(app, self.service, self.database, self.ui.navigation)
        handlers = app.handlers[-2]
        callback = next(h for h in handlers if isinstance(h, CallbackQueryHandler))
        self.assertTrue(callback.pattern.fullmatch("ock:" + DOC))
        self.assertIsNone(callback.pattern.fullmatch("cr:home"))
        observer = app.handlers[-9][0]
        self.assertTrue(observer.pattern.fullmatch("cr:home"))
        self.assertIsNone(observer.pattern.fullmatch("ock:" + DOC))

    async def test_button_and_command_close_each_active_conversation(self):
        from telegram.ext import Application
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            app = Application.builder().token("123456:test").build()
            bot.register_handlers(app)
        app.bot_data.update(database=self.database, order_check=self.ui)
        context = SimpleNamespace(application=app, user_data={})
        context.update = lambda fields: vars(context).update(fields)
        for text in [MENU_CHECK_ORDER, "/check_order"]:
            message = Message(message_id=2, date=datetime.now(bot.MOSCOW_TZ), chat=Chat(1, "private"),
                              from_user=User(1, "Test", False), text=text,
                              entities=[MessageEntity("bot_command", 0, len(text))] if text.startswith("/") else None)
            message.set_bot(SimpleNamespace(username="test_bot"))
            update = Update(2, message=message)
            for flow in [h for h in app.handlers[0] if isinstance(h, ConversationHandler)]:
                flow._conversations[(1, 1)] = next(iter(flow.states))
                picked = next(h for h in app.handlers[0] if h.check_update(update))
                self.assertIs(picked, flow)
                with patch.object(Message, "reply_text", new_callable=AsyncMock):
                    await flow.handle_update(update, app, flow.check_update(update), context)
                self.assertNotIn((1, 1), flow._conversations)
                self.assertTrue(context.user_data[ACTIVE])


if __name__ == "__main__":
    unittest.main()
