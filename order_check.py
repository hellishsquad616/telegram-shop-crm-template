"""Staff-only, read-only order checks against live MoySklad data."""
import asyncio
from collections import OrderedDict
from datetime import datetime
from decimal import Decimal, InvalidOperation
import logging
import re
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationHandlerStop, CallbackQueryHandler, MessageHandler, filters

from customer_refunds import MS_API, RefundError
from support_order_info import OrderInformation, OrderInfoError, SHIPMENT_TEXT, UUID, normalized, parse_query


LOG = logging.getLogger(__name__)
MENU_CHECK_ORDER = "🔎 Проверить заказ"
ACTIVE = "moysklad_order_check_active"
MOSCOW = ZoneInfo("Europe/Moscow")


def entity_id(entity):
    value = str(entity.get("id") or (entity.get("meta") or {}).get("href", "").rstrip("/").split("/")[-1])
    return value if re.fullmatch(UUID, value) else ""


def amount(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise RefundError("МойСклад не передал корректное количество.") from None
    if not result.is_finite():
        raise RefundError("МойСклад не передал корректное количество.")
    return result


def units(value):
    return format(value, "f").rstrip("0").rstrip(".") if value % 1 else str(int(value))


def chunks(text, limit=3800):
    current = ""
    for line in text.splitlines():
        for start in range(0, max(1, len(line)), limit):
            part = line[start:start + limit]
            if current and len(current) + len(part) + 1 > limit:
                yield current
                current = ""
            current = current + "\n" + part if current else part
    if current:
        yield current


class OrderCheck(OrderInformation):
    def __init__(self, service, database, navigation):
        super().__init__(service, database)
        self.navigation = navigation

    def allowed(self, update):
        message, user = update.effective_message, update.effective_user
        return bool(message and user and message.chat.type == "private"
                    and self.database.has_permission(user.id, "check_order"))

    async def begin(self, update, context):
        if not self.allowed(update):
            await update.effective_message.reply_text(
                "Проверка доступна в личном чате сотрудникам с правом «Проверить заказ в МС».", parse_mode=None)
            return
        context.user_data.clear()
        context.user_data[ACTIVE] = True
        refunds = context.application.bot_data.get("customer_refunds_ui")
        if refunds:
            refunds.pending.discard(update.effective_user.id)
            refunds.note_requests.pop(update.effective_user.id, None)
        await update.effective_message.reply_text(
            "Отправьте номер заказа, например #1010101001.\n"
            "Покажу статус и срок отправки из МоегоСклада, состав и остатки на складе заказа.\n"
            "Для выхода нажмите «Главное меню» или отправьте /cancel.", parse_mode=None)

    async def shipping_lines(self, order):
        try:
            mark = await self.mark(order)
        except RefundError:
            mark = ""
            lines = ["Пометка отправки: не удалось прочитать из МС."]
        else:
            mapped = SHIPMENT_TEXT.get(normalized(mark))
            if mapped:
                lines = ["Срок отправки: " + mapped, "Пометка МС: " + mark[:250]]
            elif mark and re.search(r"уедет|отправ|отгруз|рассыл", mark, re.I):
                lines = ["Срок / партия отправки (пометка МС): " + mark[:250]]
            else:
                lines = ["Срок отправки: в МС не указан."]
                if mark:
                    lines.append("Пометка МС: " + mark[:250])
        planned = order.get("deliveryPlannedMoment")
        if planned:
            try:
                label = datetime.fromisoformat(str(planned)).strftime("%d.%m.%Y %H:%M")
            except ValueError:
                label = str(planned)[:50]
            lines.append("Плановая дата отгрузки в МС: " + label)
            if lines[0] == "Срок отправки: в МС не указан.":
                lines.pop(0)
        return lines

    async def stock_lines(self, order):
        positions = await self.rows("/entity/customerorder/" + order["id"] + "/positions", {"expand": "assortment"})
        grouped = OrderedDict()
        for position in positions:
            item = position.get("assortment") or {}
            if (item.get("meta") or {}).get("type") == "service":
                continue
            quantity = amount(position.get("quantity"))
            if quantity <= 0:
                continue
            ident = entity_id(item)
            key = ident or (position.get("id"), item.get("name"))
            if key not in grouped:
                grouped[key] = {"id": ident, "name": str(item.get("name") or "Вещь без названия")[:200],
                                "quantity": Decimal(0), "reserved": Decimal(0)}
            grouped[key]["quantity"] += quantity
            grouped[key]["reserved"] += amount(position.get("reserve", 0))
        if not grouped:
            return ["В документе нет товарных позиций."]
        store = order.get("store") or {}
        store_id = entity_id(store)
        if not store_id:
            return ["Остатки: в заказе не выбран склад. Проверить наличие не удалось."] + [
                f"• {item['name']} — {units(item['quantity'])} шт." for item in grouped.values()]
        store_name = str(store.get("name") or "Склад заказа")[:150]
        lines = ["Склад: " + store_name, "Свободно = физический остаток минус все резервы."]
        stock_semaphore = asyncio.Semaphore(3)

        async def one(item):
            prefix = ["", f"• {item['name']} — нужно {units(item['quantity'])} шт."]
            if not item["id"]:
                return prefix + ["Остаток: точная модификация не определена."]
            try:
                async with stock_semaphore:
                    rows = await self.rows("/entity/assortment", {"filter":
                        "id=" + item["id"] + ";stockStore=" + MS_API + "/entity/store/" + store_id})
                matches = [row for row in rows if entity_id(row) == item["id"]]
                if len(matches) != 1:
                    return prefix + ["Остаток: точная модификация не найдена в ответе МС."]
                stock = amount(matches[0].get("stock"))
                reserved = amount(matches[0].get("reserve"))
                free = max(Decimal(0), stock - reserved)
                label = "Есть свободный остаток" if free >= item["quantity"] else "Недостаточно свободного остатка"
                return prefix + [f"{label}: {units(free)} шт.",
                                 f"Физически на складе: {units(stock)} шт. В резервах: {units(reserved)} шт.",
                                 f"Резерв этого заказа: {units(item['reserved'])} шт."]
            except RefundError:
                return prefix + ["Остаток: не удалось проверить в МС. Повторите проверку."]

        for item_lines in await asyncio.gather(*(one(item) for item in grouped.values())):
            lines.extend(item_lines)
        return lines

    async def card(self, order):
        self.service.check_brand(order)
        if not re.fullmatch(UUID, str(order.get("id") or "")):
            raise RefundError("Некорректный документ МоегоСклада.")
        state = order.get("state") or {}
        state_name = str(state.get("name") or ("не удалось прочитать" if state else "не указан"))[:200]
        lines = ["Заказ: " + str(order.get("name") or order["id"])[:150], "Статус МС: " + state_name, ""]
        lines.extend(await self.shipping_lines(order))
        lines.append("")
        try:
            lines.extend(await self.stock_lines(order))
        except RefundError:
            lines.append("Состав и остатки: не удалось проверить в МС. Повторите проверку.")
        lines.extend(["", "Проверено: " + datetime.now(MOSCOW).strftime("%d.%m.%Y %H:%M") + " МСК"])
        return "\n".join(lines)

    def choices(self, orders):
        keyboard = super().choices(orders)
        return InlineKeyboardMarkup([
            [InlineKeyboardButton(button.text, callback_data="ock:" + button.callback_data.split(":", 1)[1])]
            for row in keyboard.inline_keyboard for button in row])

    @staticmethod
    def refresh_keyboard(doc_id):
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("Обновить данные", callback_data="ock:" + doc_id)],
            [InlineKeyboardButton("Проверить другой заказ", callback_data="ock:start")]])

    async def result(self, parsed):
        orders = await self.search(*parsed)
        if not orders:
            return "Заказ не найден в МойСклад SHOPCRM. Проверьте номер.", None
        if len(orders) > 10:
            return "Найдено больше 10 документов. Уточните номер заказа.", None
        if len(orders) > 1:
            return "Найдено несколько документов. Выберите нужный заказ или возврат:", self.choices(orders)
        order = await self.service.ms("GET", "/entity/customerorder/" + orders[0]["id"],
                                      params={"expand": "state,store"})
        return await self.card(order), self.refresh_keyboard(order["id"])

    async def send_result(self, message, text, keyboard):
        parts = list(chunks(text))
        for i, part in enumerate(parts):
            await message.reply_text(part, parse_mode=None, reply_markup=keyboard if i == len(parts) - 1 else None)

    async def process_lookup(self, update, context, key, parsed=None, doc_id=None):
        try:
            async with self.semaphore:
                if not self.allowed(update):
                    return
                async def lookup():
                    if doc_id:
                        order = await self.service.ms("GET", "/entity/customerorder/" + doc_id,
                                                      params={"expand": "state,store"})
                        return await self.card(order), self.refresh_keyboard(doc_id)
                    return await self.result(parsed)
                try:
                    text, keyboard = await asyncio.wait_for(lookup(), timeout=60)
                except OrderInfoError as error:
                    text, keyboard = str(error), None
                except (RefundError, asyncio.TimeoutError):
                    text, keyboard = "Не удалось проверить заказ в МС. Повторите запрос позже.", None
                except Exception as error:
                    LOG.warning("Private order check failed: %s", type(error).__name__)
                    text, keyboard = "Не удалось проверить заказ. Повторите запрос позже.", None
                if self.allowed(update):
                    await self.send_result(update.effective_message, text, keyboard)
        finally:
            self.pending.discard(key)

    async def receive(self, update, context):
        message = update.message
        if not message or not context.user_data.get(ACTIVE):
            return
        text = str(message.text or "")
        if self.navigation(text) or text.startswith("/"):
            context.user_data.pop(ACTIVE, None)
            return
        if not self.allowed(update):
            context.user_data.pop(ACTIVE, None)
            await message.reply_text("Нет доступа к проверке заказов.", parse_mode=None)
            raise ApplicationHandlerStop
        parsed = parse_query(text)
        if not parsed or parsed[0] != "number":
            await message.reply_text("Отправьте один номер заказа, например #1010101001.", parse_mode=None)
            raise ApplicationHandlerStop
        key = (message.chat.id, message.message_id)
        if key in self.pending or key in self.seen:
            raise ApplicationHandlerStop
        if len(self.pending) >= 8:
            await message.reply_text("Проверяется несколько заказов. Повторите через несколько секунд.", parse_mode=None)
            raise ApplicationHandlerStop
        self.pending.add(key)
        self.seen.append(key)
        context.application.create_task(self.process_lookup(update, context, key, parsed=parsed), update=update)
        raise ApplicationHandlerStop

    async def callback(self, update, context):
        query = update.callback_query
        if not self.allowed(update):
            await query.answer("Нет доступа к проверке заказов в этом чате.", show_alert=True)
            raise ApplicationHandlerStop
        if query.data == "ock:start":
            await query.answer()
            await self.begin(update, context)
            raise ApplicationHandlerStop
        key = ("callback", query.id)
        if key in self.pending or key in self.seen:
            await query.answer("Этот запрос уже проверяется.")
            raise ApplicationHandlerStop
        if len(self.pending) >= 8:
            await query.answer("Проверяется несколько заказов. Повторите позже.")
            raise ApplicationHandlerStop
        await query.answer("Проверяю заказ…")
        self.pending.add(key)
        self.seen.append(key)
        context.application.create_task(
            self.process_lookup(update, context, key, doc_id=query.data.split(":", 1)[1]), update=update)
        raise ApplicationHandlerStop

    async def leave(self, update, context):
        context.user_data.pop(ACTIVE, None)


def register_order_check(application, service, database, navigation):
    ui = OrderCheck(service, database, navigation)
    application.bot_data["order_check"] = ui
    application.add_handler(CallbackQueryHandler(ui.callback, pattern=r"^ock:(?:start|" + UUID + r")$"), group=-2)
    # Observe navigation before refund/gift callbacks can stop dispatch.
    application.add_handler(CallbackQueryHandler(ui.leave, pattern=r"^(?!ock:).+"), group=-9)
    application.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT, ui.receive), group=-2)
    return ui
