import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram.ext import ApplicationHandlerStop

from customer_refunds import RefundError, SETTINGS
from database import Database
from support_order_info import (CHAT_SETTING, TOPIC_SETTING, MARK_SETTING, OTHER_STATUS,
                               OrderInformation, OrderInfoError, parse_query, number_matches)

ORG = "00000000-0000-4000-8000-000000000001"
DOC = "00000000-0000-4000-8000-000000000002"
DOC2 = "00000000-0000-4000-8000-000000000003"
AGENT = "00000000-0000-4000-8000-000000000004"
MARK = "00000000-0000-4000-8000-000000000005"
LIST = "00000000-0000-4000-8000-000000000006"
ENTRY = "00000000-0000-4000-8000-000000000007"


def order(mark="ДЕМО ПЕРВАЯ ПАРТИЯ", **changes):
    result = {"id": DOC, "name": "order-1234567890", "organization": {"id": ORG},
              "state": {"name": "Оплачен"},
              "attributes": [{"id": MARK, "name": "Пометка", "value": {"name": mark}}]}
    result.update(changes)
    return result


class OrderInformationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        mapping = patch.dict("support_order_info.SHIPMENT_TEXT", {
            "ДЕМО ПЕРВАЯ ПАРТИЯ": "Заказ относится к первой рассылке, отправка в течение недели",
            "ДЕМО ВТОРАЯ ПАРТИЯ": "Заказ относится ко второй рассылке, отправка в течение месяца",
            "ДЕМО АЛЬТЕРНАТИВНАЯ ПАРТИЯ": "Заказ относится ко второй рассылке, отправка в течение месяца",
        }, clear=True)
        mapping.start(); self.addCleanup(mapping.stop)
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.db = Database(Path(self.temp.name) / "crm.sqlite3"); self.db.initialize()
        self.db.set_setting(CHAT_SETTING, "-1001"); self.db.set_setting(TOPIC_SETTING, "102")
        self.db.set_setting(MARK_SETTING, MARK)
        self.settings = patch.dict(SETTINGS, {"ms_organization_id": ORG}, clear=True)
        self.settings.start(); self.addCleanup(self.settings.stop)
        def brand(doc):
            if doc.get("organization", {}).get("id") != ORG:
                raise RefundError("Wrong brand")
        self.service = SimpleNamespace(ms=AsyncMock(), check_brand=brand)
        self.ui = OrderInformation(self.service, self.db)

    def test_queries_accept_number_and_full_name_but_not_chat_prose(self):
        for text in ["1234567890", "#1234567890", "Заказ №1234567890", "№ 1234567890", "заказ 1234567890"]:
            self.assertEqual(parse_query(text), ("number", "1234567890"))
        for text in ["Иванова Анна Сергеевна", "Коваль-Иванова Анна", "  Семёнова  Анна "]:
            self.assertEqual(parse_query(text)[0], "name")
        for text in ["/start", "123", "Иванова", "Привет!", "1234567890 9999999999", "name: Анна Иванова"]:
            self.assertIsNone(parse_query(text))
        self.assertTrue(number_matches(order(), "1234567890"))
        self.assertTrue(number_matches(order(name="Возврат-1234567890"), "1234567890"))
        self.assertFalse(number_matches(order(name="order-112345678901"), "1234567890"))

    async def test_mark_in_attribute_controls_reply_not_document_status(self):
        first = await self.ui.card(order(state={"name": "Совсем другой статус"}))
        self.assertIn("первой рассылке, отправка в течение недели", first)
        second = await self.ui.card(order(mark=" демо  вторая партия "))
        self.assertIn("ко второй рассылке, отправка в течение месяца", second)
        self.assertIn("в течение месяца", await self.ui.card(order(mark="ДЕМО АЛЬТЕРНАТИВНАЯ ПАРТИЯ")))
        for doc in [order(mark="Другая пометка"), order(attributes=[]),
                    order(mark="ДЕМО НЕИЗВЕСТНАЯ ПАРТИЯ"),
                    order(mark="", state={"name": "ДЕМО ПЕРВАЯ ПАРТИЯ"})]:
            self.assertIn(OTHER_STATUS, await self.ui.card(doc))
        self.service.ms.assert_not_awaited()

    async def test_customentity_reference_is_read_only_and_rejects_untrusted_urls(self):
        doc = order(); attr = doc["attributes"][0]
        path = "/entity/customentity/" + LIST + "/" + ENTRY
        attr["value"] = {"meta": {"href": "https://api.moysklad.ru/api/remap/1.2" + path}}
        self.service.ms.return_value = {"name": "ДЕМО ПЕРВАЯ ПАРТИЯ"}
        self.assertIn("первой рассылке", await self.ui.card(doc))
        self.service.ms.assert_awaited_once_with("GET", path)
        for href in ["https://example.test" + path, "https://api.moysklad.ru/api/remap/1.2/entity/organization/" + DOC]:
            attr["value"]["meta"]["href"] = href
            with self.assertRaises(RefundError): await self.ui.card(doc)
        with self.assertRaises(RefundError): await self.ui.card(order(organization={"id": DOC2}))

    async def test_number_search_filters_substrings_and_other_brands(self):
        self.service.ms.return_value = {"rows": [order(), order(id=DOC2, name="order-112345678901"),
                                                order(id=DOC2, organization={"id": DOC2})]}
        text, keyboard = await self.ui.result(("number", "1234567890"))
        self.assertIn("первой рассылке", text); self.assertIsNone(keyboard)
        call = self.service.ms.call_args
        self.assertEqual(call.args, ("GET", "/entity/customerorder"))
        self.assertEqual(call.kwargs["params"]["search"], "1234567890")
        self.assertIn("organization=", call.kwargs["params"]["filter"])

    async def test_name_search_matches_all_parts_and_lists_orders_for_that_agent(self):
        self.service.ms.side_effect = [
            {"rows": [{"id": AGENT, "name": "Семёнова Анна Сергеевна"},
                      {"id": DOC2, "name": "Семенова Анастасия Сергеевна"}]},
            {"rows": [order(), order(id=DOC2, name="order-9999999999")]},
        ]
        text, keyboard = await self.ui.result(("name", "Анна Семенова"))
        self.assertIn("несколько документов", text)
        self.assertEqual(len(keyboard.inline_keyboard), 2)
        self.assertEqual(self.service.ms.await_count, 2)
        self.assertIn(";agent=", self.service.ms.call_args.kwargs["params"]["filter"])
        self.assertTrue(all(len(row[0].callback_data.encode()) <= 64 for row in keyboard.inline_keyboard))

    async def test_duplicate_return_and_original_are_not_automatically_collapsed(self):
        self.service.ms.return_value = {"rows": [order(), order(id=DOC2, name="Возврат-1234567890")]}
        text, keyboard = await self.ui.result(("number", "1234567890"))
        self.assertIn("Выберите нужный заказ", text)
        self.assertEqual(len(keyboard.inline_keyboard), 2)

    async def test_incomplete_pagination_never_selects_partial_results(self):
        self.service.ms.side_effect = [{"meta": {"size": 2}, "rows": [order()]},
                                       {"meta": {"size": 2}, "rows": []}]
        with self.assertRaises(OrderInfoError): await self.ui.result(("number", "1234567890"))
        self.assertEqual(self.service.ms.call_args.kwargs["params"]["offset"], 1)

    async def test_not_found_is_distinct_from_api_failure(self):
        self.service.ms.return_value = {"rows": []}
        text, keyboard = await self.ui.result(("number", "1234567890"))
        self.assertIn("не найден", text); self.assertIsNone(keyboard)
        message = self.message()
        self.service.ms.side_effect = RefundError("HTTP 403")
        await self.ui.process(message, ("number", "1234567890"), (1, 2))
        self.assertIn("Не удалось проверить", message.reply_text.call_args.args[0])
        self.assertNotIn(OTHER_STATUS, message.reply_text.call_args.args[0])

    def message(self, **changes):
        fields = dict(chat=SimpleNamespace(id=-1001), message_thread_id=102,
                      message_id=10, text="1234567890", from_user=SimpleNamespace(is_bot=False), reply_text=AsyncMock())
        fields.update(changes)
        return SimpleNamespace(**fields)

    async def test_only_new_human_messages_in_configured_topic_start_background_lookup(self):
        tasks = []
        def create_task(coro, **kwargs): tasks.append(coro)
        context = SimpleNamespace(application=SimpleNamespace(create_task=create_task))
        message = self.message()
        for other in [self.message(message_thread_id=101), self.message(chat=SimpleNamespace(id=-1002)),
                      self.message(from_user=SimpleNamespace(is_bot=True)), self.message(text="/refunds"), None]:
            await self.ui.external(SimpleNamespace(message=other), context)
        self.assertEqual(tasks, [])
        self.service.ms.return_value = {"rows": [order()]}
        with self.assertRaises(ApplicationHandlerStop):
            await self.ui.external(SimpleNamespace(message=message), context)
        self.service.ms.assert_not_awaited()  # The polling handler remains responsive.
        with self.assertRaises(ApplicationHandlerStop):
            await self.ui.external(SimpleNamespace(message=message), context)
        self.assertEqual(len(tasks), 1)
        await tasks[0]
        self.assertEqual(len(self.ui.pending), 0)
        self.assertEqual(message.reply_text.await_count, 1)
        self.assertEqual(message.reply_text.call_args.kwargs["message_thread_id"], 102)

    async def test_choice_reloads_current_mark_and_refuses_other_topic(self):
        query = SimpleNamespace(id="callback", data="soi:" + DOC, message=self.message(), answer=AsyncMock())
        self.service.ms.return_value = order(mark="ДЕМО ВТОРАЯ ПАРТИЯ")
        await self.ui.open_order(query, (1, 2))
        self.assertIn("второй рассылке", query.message.reply_text.call_args.args[0])
        self.service.ms.assert_awaited_once_with("GET", "/entity/customerorder/" + DOC, params={"expand": "state"})
        query.message.message_thread_id = 101
        context = SimpleNamespace(application=SimpleNamespace(create_task=lambda *a, **k: self.fail("Wrong topic")))
        with self.assertRaises(ApplicationHandlerStop):
            await self.ui.callback(SimpleNamespace(callback_query=query), context)
        self.assertTrue(query.answer.call_args.kwargs["show_alert"])

    async def test_busy_queue_does_not_block_or_silently_drop_request(self):
        self.ui.pending.update(range(8))
        message = self.message()
        context = SimpleNamespace(application=SimpleNamespace(create_task=lambda *a, **k: self.fail("Queue full")))
        with self.assertRaises(ApplicationHandlerStop):
            await self.ui.external(SimpleNamespace(message=message), context)
        self.assertIn("Повторите", message.reply_text.call_args.args[0])

    def test_registration_precedes_refund_and_conversation_handlers(self):
        from telegram.ext import Application, CallbackQueryHandler, MessageHandler
        from support_order_info import register_order_information
        app = Application.builder().token("123456:test").build()
        ui = register_order_information(app, self.service, self.db)
        self.assertIs(app.bot_data["support_order_information"], ui)
        self.assertTrue(any(isinstance(h, MessageHandler) and h.callback == ui.external for h in app.handlers[-7]))
        callback = next(h for h in app.handlers[-7] if isinstance(h, CallbackQueryHandler))
        self.assertTrue(callback.pattern.fullmatch("soi:" + DOC))
        self.assertIsNone(callback.pattern.fullmatch("cr:view:281:2"))


if __name__ == "__main__":
    unittest.main()
