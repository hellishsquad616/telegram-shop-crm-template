"""Read-only shipping information in a dedicated support forum topic."""
import asyncio
import logging
import re
from collections import Counter, deque
from urllib.parse import urlsplit

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationHandlerStop, CallbackQueryHandler, MessageHandler, filters

from customer_refunds import MS_API, RefundError
from refund_settings import SETTINGS


LOG = logging.getLogger(__name__)
CHAT_SETTING = "support_order_info_chat_id"
TOPIC_SETTING = "support_order_info_topic_id"
MARK_SETTING = "support_order_info_mark_id"
OTHER_STATUS = "Указан другой статус, проверьте сроки отправки заказа или уточните у ответственного менеджера"
SHIPMENT_TEXT = SETTINGS.get("support_shipment_text", {})

UUID = r"[a-f0-9]{8}-(?:[a-f0-9]{4}-){3}[a-f0-9]{12}"
NAME_WORD = r"[^\W\d_]+(?:[-'’][^\W\d_]+)*"


class OrderInfoError(RefundError):
    """A lookup clarification safe to show to support staff."""


def normalized(value):
    return " ".join(str(value or "").replace("ё", "е").replace("Ё", "Е").upper().split())


def name_words(value):
    return re.findall(NAME_WORD, normalized(value))


def parse_query(text):
    text = str(text or "").strip()
    number = re.fullmatch(r"(?:(?:заказ\s*)?(?:№|#)\s*|заказ\s+)?(\d{5,20})", text, re.I)
    if number:
        return "number", number[1]
    if len(text) <= 150 and re.fullmatch(NAME_WORD + r"(?:\s+" + NAME_WORD + r"){1,3}", text):
        return "name", text
    return None


def number_matches(order, number):
    # Never match a part of a longer order number. Renamed return/exchange
    # documents remain separate choices rather than silently replacing originals.
    return bool(re.search(r"(?<!\d)" + re.escape(number) + r"(?!\d)", str(order.get("name") or ""))
                or str(order.get("code") or "").strip() == number)


class OrderInformation:
    def __init__(self, service, database):
        self.service, self.database = service, database
        self.semaphore = asyncio.Semaphore(3)
        self.pending = set()
        self.seen = deque(maxlen=256)

    def location_matches(self, message):
        if not message:
            return False
        try:
            chat = int(self.database.get_setting(CHAT_SETTING) or 0)
            topic = int(self.database.get_setting(TOPIC_SETTING) or 0)
        except ValueError:
            return False
        return bool(chat and topic and message.chat.id == chat and message.message_thread_id == topic)

    async def rows(self, path, params):
        rows, offset = [], 0
        for _ in range(5):
            page = await self.service.ms("GET", path, params=dict(params, limit=100, offset=offset))
            chunk = page.get("rows", [])
            rows.extend(chunk)
            total = (page.get("meta") or {}).get("size")
            if (total is not None and len(rows) >= int(total)) or (total is None and len(chunk) < 100):
                return rows
            if not chunk:
                break
            offset += len(chunk)
        raise OrderInfoError("Слишком много совпадений в МойСклад. Укажите номер заказа или полное ФИО клиента.")

    def scoped_orders(self, rows):
        result = {}
        for row in rows:
            try:
                self.service.check_brand(row)
            except RefundError:
                continue
            if re.fullmatch(UUID, str(row.get("id") or "")):
                result[row["id"]] = row
        return list(result.values())

    def order_params(self):
        organization = str(SETTINGS.get("ms_organization_id") or "")
        if not re.fullmatch(UUID, organization):
            raise RefundError("Не настроено подключение МойСклад SHOPCRM.")
        return {"expand": "state,agent", "filter": "organization=" + MS_API + "/entity/organization/" + organization}

    async def search(self, kind, query):
        params = self.order_params()
        if kind == "number":
            rows = await self.rows("/entity/customerorder", dict(params, search=query))
            return [row for row in self.scoped_orders(rows) if number_matches(row, query)]
        wanted = Counter(name_words(query))
        # Search by a word and then compare every supplied name part locally.
        # This also accepts changed word order and ё/е without selecting namesakes.
        needle = max(wanted, key=len)
        agents = await self.rows("/entity/counterparty", {"search": needle})
        agents = [a for a in agents if not (wanted - Counter(name_words(a.get("name"))))]
        if len(agents) > 10:
            raise OrderInfoError("Найдено несколько клиентов с таким ФИО. Укажите номер заказа.")
        orders = []
        for agent in agents:
            if not re.fullmatch(UUID, str(agent.get("id") or "")):
                continue
            agent_filter = ";agent=" + MS_API + "/entity/counterparty/" + agent["id"]
            orders.extend(await self.rows("/entity/customerorder", dict(params, filter=params["filter"] + agent_filter)))
        return self.scoped_orders(orders)

    async def mark(self, order):
        mark_id = self.database.get_setting(MARK_SETTING)
        attributes = [a for a in order.get("attributes", [])
                      if (a.get("id") == mark_id if mark_id else normalized(a.get("name")) == "ПОМЕТКА")]
        if not attributes:
            return ""
        if len(attributes) != 1:
            raise RefundError("В документе несколько полей «Пометка». Проверьте заказ в МойСклад.")
        value = attributes[0].get("value")
        if isinstance(value, str):
            return value
        if not isinstance(value, dict):
            return ""
        if "name" in value:
            return str(value["name"] or "")
        href = str((value.get("meta") or {}).get("href") or "")
        parts = urlsplit(href)
        prefix = urlsplit(MS_API)
        if (parts.scheme != prefix.scheme or parts.netloc != prefix.netloc or parts.query or parts.fragment
                or not re.fullmatch(re.escape(prefix.path) + r"/entity/customentity/" + UUID + "/" + UUID, parts.path)):
            raise RefundError("Не удалось прочитать пометку МойСклад. Повторите поиск позже.")
        result = await self.service.ms("GET", parts.path[len(prefix.path):])
        return str(result.get("name") or "")

    async def card(self, order):
        self.service.check_brand(order)
        mark = await self.mark(order)
        answer = SHIPMENT_TEXT.get(normalized(mark), OTHER_STATUS)
        return "Заказ: " + str(order.get("name") or order["id"])[:150] + "\n\n" + answer

    def choices(self, orders):
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(
                (str(order.get("name") or "Заказ") + " · " + str((order.get("state") or {}).get("name") or "без статуса"))[:110],
                callback_data="soi:" + order["id"])]
            for order in sorted(orders, key=lambda o: (str(o.get("moment") or ""), o["id"]), reverse=True)
        ])

    async def result(self, parsed):
        orders = await self.search(*parsed)
        if not orders:
            return "Заказ не найден в МойСклад. Проверьте номер или ФИО клиента.", None
        if len(orders) > 10:
            return "Найдено больше 10 документов. Укажите номер заказа вместо ФИО.", None
        if len(orders) > 1:
            return "Найдено несколько документов. Выберите нужный заказ:", self.choices(orders)
        return await self.card(orders[0]), None

    async def external(self, update, context):
        message = update.message  # Edited text never starts a second search.
        if (not self.location_matches(message) or not message.text or message.text.startswith("/")
                or (message.from_user and message.from_user.is_bot)):
            return
        key = (message.chat.id, message.message_id)
        if key in self.pending or key in self.seen:
            raise ApplicationHandlerStop
        if len(self.pending) >= 8:
            await message.reply_text("Проверяется несколько заказов. Повторите запрос через несколько секунд.", parse_mode=None)
            raise ApplicationHandlerStop
        self.pending.add(key)
        self.seen.append(key)
        context.application.create_task(self.process(message, parse_query(message.text), key), update=update)
        raise ApplicationHandlerStop

    async def process(self, message, parsed, key):
        try:
            async with self.semaphore:
                if not parsed:
                    text, keyboard = "Напишите номер заказа или ФИО клиента. Например: 1234567890 или Иванова Анна Сергеевна.", None
                else:
                    try:
                        text, keyboard = await asyncio.wait_for(self.result(parsed), timeout=25)
                    except OrderInfoError as error:
                        text, keyboard = str(error), None
                    except (RefundError, asyncio.TimeoutError):
                        text, keyboard = "Не удалось проверить заказ в МойСклад. Повторите запрос позже или уточните у ответственного менеджера.", None
                    except Exception as error:
                        LOG.warning("Support order lookup failed: %s", type(error).__name__)
                        text, keyboard = "Не удалось проверить заказ. Повторите запрос позже.", None
                await message.reply_text(text, reply_markup=keyboard, parse_mode=None, do_quote=True,
                                         message_thread_id=message.message_thread_id)
        finally:
            self.pending.discard(key)

    async def callback(self, update, context):
        query = update.callback_query
        if not query or not self.location_matches(query.message):
            if query:
                await query.answer("Откройте заказ в теме «Информация по заказам клиентов».", show_alert=True)
            raise ApplicationHandlerStop
        if len(self.pending) >= 8:
            await query.answer("Проверяется несколько заказов. Повторите через несколько секунд.")
            raise ApplicationHandlerStop
        key = ("callback", query.message.chat.id, query.message.message_id, query.data)
        if key in self.pending:
            await query.answer("Этот заказ уже проверяется.")
            raise ApplicationHandlerStop
        self.pending.add(key)
        try:
            await query.answer("Проверяю пометку…")
        except Exception:
            self.pending.discard(key)
            raise
        context.application.create_task(self.open_order(query, key), update=update)
        raise ApplicationHandlerStop

    async def open_order(self, query, key):
        try:
            async with self.semaphore:
                async def fresh_card():
                    order = await self.service.ms("GET", "/entity/customerorder/" + query.data.split(":", 1)[1], params={"expand": "state"})
                    return await self.card(order)
                try:
                    text = await asyncio.wait_for(fresh_card(), timeout=25)
                except (RefundError, asyncio.TimeoutError):
                    text = "Не удалось проверить выбранный заказ. Повторите поиск по номеру или уточните у ответственного менеджера."
                except Exception as error:
                    LOG.warning("Support order choice failed: %s", type(error).__name__)
                    text = "Не удалось проверить выбранный заказ. Повторите поиск позже."
                await query.message.reply_text(text, parse_mode=None, do_quote=True,
                                               message_thread_id=query.message.message_thread_id)
        finally:
            self.pending.discard(key)


def register_order_information(application, service, database):
    ui = OrderInformation(service, database)
    application.bot_data["support_order_information"] = ui
    # Run before conversations and refund parsing, consuming only the configured topic.
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, ui.external), group=-7)
    application.add_handler(CallbackQueryHandler(ui.callback, pattern=r"^soi:" + UUID + "$"), group=-7)
    return ui
