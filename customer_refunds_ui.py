"""Private owner inbox fed only by the configured support topic."""
import asyncio
import logging
import re
import time
from datetime import datetime

from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup as Markup, InputMediaPhoto
from telegram.error import BadRequest, TelegramError, RetryAfter
from telegram.ext import ApplicationHandlerStop, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from customer_refunds import OWNER, SUPPORT_CHAT, SUPPORT_TOPIC, MOSCOW, RefundError, RefundService, EDITABLE, ACTIVE, rubles, approval_hash, hashed, completed_chargeback_available, waiting_funds, payment_method_from_label, now
from refund_receipt_image import render_refund
from yandex_refunds import is_yandex, yandex_label

MENU_CUSTOMER_REFUNDS = "💳 Возвраты средств"
LOG = logging.getLogger("shopcrm.customer_refunds")
STATE = {"new":"Новая заявка","needs_info":"Нужно уточнение","ready":"Готово к вашему подтверждению","submitting":"Запрос возврата отправляется","uncertain":"Проверяем результат без повторной отправки","pending":"Платёжный сервис обрабатывает возврат","succeeded":"Платёжный сервис подтвердил возврат","failed":"Возврат не подтверждён","rejected":"Отклонено"}
GUIDE = """Заявка поддержки одним сообщением:

Заказ: НОМЕР
ФИО: Фамилия Имя Отчество
email: почта клиента
phone: телефон клиента
ЮKassa, за вещь

Варианты: за вещь (все товары), за доставку, за весь заказ, за часть товаров.
Если возвращается одна вещь из нескольких, напишите:
ЮKassa, за вещь
DEMO PUFFER BLACK
Размер: XS
Название можно указать и в той же строке после «за вещь» либо как «Товар: ...». Вернётся одна единица выбранной вещи без доставки, по фактически оплаченной цене. При нескольких совпадениях нужно уточнить название, цвет и размер. Без названия «за вещь» сохраняет прежнее значение: все оставшиеся товары без доставки.
Для Яндекса укажите «Сплит», «Яндекс Пэй» или «Яндекс СБП» вместо «ЮKassa».

Компенсация за ателье / разницу в цене:
Создайте отдельный документ МойСклад на точную сумму компенсации, например «Ателье — 3 150 ₽». В заявке укажите:
ЮKassa, только за вещь
Товар: BASE PUFFER BURGUNDY, Размер=L
Компенсация клиенту — 3 150 ₽.
Вещь остаётся у клиента.
Можно указать Сплит вместо ЮKassa. Вернётся только названная сумма на исходный платёж, после подтверждения владельца; вещь не считается физически возвращённой.

В МойСклад подготовьте отдельный документ возврата/отмены с выбранными товарами, размерами и количеством. При нескольких документах владелец выберет нужный. Сумма берётся из платёжного сервиса с учётом скидок и предыдущих возвратов; расхождение цен с МойСклад не блокирует проведение. При возврате всего заказа оплаченная доставка включается автоматически.
При выборе вещи по названию документ может содержать другие позиции: они не попадут в денежный возврат. В описании документа сохранится пояснение о выбранной вещи и сумме.
Если шарф из документа не найден в оплаченных позициях либо имеет нулевую цену в чеке, владелец увидит вопрос «Это подарок?» и кнопки «Да, подарок» / «Нет, куплен». Подарок исключается только после ответа владельца. Купленный шарф должен быть подтверждён оплатой; цену из МойСклад бот не подставляет. Ответ пересчитывает карточку, проведение денег требует отдельного подтверждения суммы.
Расхождения ФИО, email и телефона с Tilda, МойСклад или оплатой показываются как замечания и не блокируют возврат. Для чека используется контакт исходной оплаты, при его отсутствии — корректный контакт документа. Номер заказа, магазин, статус документа, товары, размеры, количество и вариант возврата должны совпасть.

При необходимости добавьте payment_id: ID платежа ЮKassa или yandex_order_id: полный ID заказа Яндекс Пэй.
Деньги возвращаются только после личного подтверждения владельцем точной суммы. Для ЮKassa статус и дата МойСклад обновляются после успеха сервиса. Для Сплита после принятия операции в обработку ставится внутренняя отметка оформления с пояснением; фактический результат остаётся в боте до ответа Яндекса.

ЮKassa: картинка по реквизитам зарегистрированного чека. Сплит: подтверждение операции Яндекс Пэй с подписью «Не является фискальным чеком».
Возвраты через Яндекс Пэй разрешены для SPLIT/SPLIT_SBP, SBP и CARD. Способ оплаты определяется по API Яндекса.
Для повторного получения документа напишите в этой теме: «Нужен чек возврата — номер заказа». Бот проверит возврат, ответит готовым документом или передаст запрос владельцу. Если документ готовится, запрос сохраняется до его получения.
Если возврат отклонён и клиент обратился в банк, владелец может нажать «Чарджбек» в карточке: уведомление поступит в исходную тему поддержки. Это ручная отметка владельца; код отказа сам по себе её не устанавливает.
"""

def buttons(rows): return Markup([[Button(t,callback_data=d) for t,d in row] for row in rows])


def confirmation_token(record):
    # Bind the owner's Yes to the verified financial data, not a UI revision.
    return approval_hash({"snapshot_hash":approval_hash(record["snapshot"]),
        "amount_cents":record["amount_cents"],"payment_id":record["payment_id"],
        "ms_order_id":record["ms_order_id"]})[:24]


def card(record):
    r = record["request"]
    customer = (record.get("snapshot") or {}).get("customer") or r
    split = is_yandex(record)
    status = STATE[record["state"]]
    existing=(record.get("snapshot") or {}).get("existing_refund")
    if existing and record["state"]=="ready":
        status=("⏳ Яндекс уже обрабатывает возврат." if existing["status"]=="pending" else "✅ Возврат уже проведён в "+("Яндексе." if split else "ЮKassa."))+" В МойСклад стоит «Возврат-проведен».\nОжидает вашего подтверждения отправки документа сотрудникам."
    if record.get("completed_review"):
        status="⚠️ В МойСклад возврат проведён, в ЮKassa возврата нет"
    if record.get("chargeback_at"):status="Чарджбек — отмечен владельцем"
    elif waiting_funds(record):status="Ожидает денежных средств · возврат не проведён"
    if record["state"] in ACTIVE:
        status = "⏳ Возврат оформлен, в процессе. " + status
    elif record["state"] == "succeeded":
        status = "✅ Возврат проведён" if record["ms_synced"] and record.get("receipt_json") else "⏳ Возврат подтверждён, завершаем оформление"
    outcome=record.get('receipt_outcome')
    related=record.get('related_refund')
    if r.get('kind')=='receipt_request':
        status={'not_refunded':'Чек возврата отсутствует — возврат ещё не проведён. Можно оформить возврат.',
            'receipt_pending':'Деньги возвращены. Чек ещё формируется; повторный возврат не требуется.',
            'processing':'Возврат в обработке. Дожидаемся ответа сервиса без повторной отправки денег.',
            'ready':'Возврат подтверждён, чек найден.'}.get(outcome,status)
        if related:status=('По этому заказу возврат уже проведён' if related['state']=='succeeded' else 'По этому заказу возврат уже обрабатывается')+f" в заявке #{related['id']}. Повторная денежная операция не нужна."+(' Деньги возвращены, чек ещё формируется.' if related['state']=='succeeded' and not related.get('receipt_json') else '')
    lines = [f'↩️ Возврат #{record["id"]} · заказ {r["order_number"]}', status,
             "Оплата: " + (yandex_label(record) if split else "ЮKassa" if r.get("method")=="yookassa" else "Не определена"),
             customer.get("name") or "ФИО не указано", customer.get("email") or customer.get("phone") or "Контакт не указан"]
    if r.get("method_source")=="tilda":lines.append("Способ оплаты определён по заказу Tilda.")
    if r.get("method_source")=="yandex_api":lines.append("Способ оплаты исправлен по подтверждённому заказу API Яндекс Пэй; в заявке была указана ЮKassa.")
    if r.get("kind")=="receipt_request":lines.append("Сотрудник запросил чек возврата."+(" Перед проведением уточните, за что вернуть деньги." if not r.get('scope') and outcome not in {'ready','processing','receipt_pending'} else ""))
    lookup = record.get("payment_lookup")
    if lookup:
        lines.append("Поиск платежа: " + {"found":"найден","not_found":"не найден","ambiguous":"несколько совпадений","needs_info":"нужно уточнение","error":"проверка не завершена"}.get(lookup["status"],"нужно уточнение"))
        if lookup["status"]=="found":
            lines += ["Сумма исходной оплаты: "+rubles(lookup["amount_cents"]),"Платёж ЮKassa: "+lookup["payment_id"]]
            if lookup.get("refunded_cents"): lines += ["Уже возвращено по платежу: "+rubles(lookup["refunded_cents"])]
        elif lookup.get("reason"): lines.append(lookup["reason"])
    if record["snapshot"]:
        s = record["snapshot"]
        if s.get('compensation'):
            lines += ['', 'Компенсация: '+rubles(s['amount_cents'])+'. Вещь остаётся у клиента.',
                      'Вещь: '+str(r.get('item_selector') or s['financial_items'][0]['name'])]
        elif r.get("item_selector"): lines += ["", "Возврат одной вещи: "+r["item_selector"]+" · 1 шт. Без доставки."]
        lines += ["", "Документ МойСклад: " + s["order"]["name"], "Статус документа при проверке: " + s["order"]["state"]]
        for item in s.get("financial_items",s["order"]["items"]):
            lines.append(f'{item["name"]} — {item["quantity"]} шт. · {rubles(item["unit_cents"]*item["quantity"])}')
        processing=existing and (record["state"] in ACTIVE or (record["state"]=="ready" and existing["status"]=="pending"))
        lines += ["", ("Сумма компенсации — по заявке и отдельному документу МойСклад; остаток оплаты проверен в платёжном сервисе." if s.get('compensation') else "Цены и сумма — по данным платёжного сервиса."), ("Возврат в обработке: " if processing else "Уже возвращено: " if existing else "К возврату: ") + rubles(s["amount_cents"]),
                  ("Заказ Яндекс Пэй: " + s["provider_order_id"] if split else "Платёж ЮKassa: " + s["payment_id"]), "Оплачено: " + rubles(s["payment_amount_cents"])]
        if s["available_cents"] != s["payment_amount_cents"]: lines += ["Остаток платежа: " + rubles(s["available_cents"])]
        if s.get("customer_warnings"):
            lines += ["", "Замечания по данным клиента:"] + s["customer_warnings"]
    if record["refund_id"]: lines += ["ID возврата: " + record["refund_id"].removeprefix("yandex:")]
    if split and record["state"] == "pending":
        lines += ["МойСклад: " + ("Возврат-проведен — отметка оформления; Яндекс ещё обрабатывает деньги." if record.get("split_booked_at") and record["ms_synced"] else "ожидает отметки оформления."),
                  "Документ для поддержки: " + ("приём операции подтверждён" if record.get("receipt_json") else "ожидаем подтверждение приёма Яндексом")]
    if record["state"] == "succeeded":
        if existing:
            lines += ["МойСклад: " + ("статус «Возврат-проведен» подтверждён; "+("дата в документе указана" if existing.get("ms_date") else "дата в документе не указана") if record["ms_synced"] else "проведённый документ требует сверки")]
        else:lines += ["МойСклад: " + ("статус и дата обновлены" if record["ms_synced"] else "требуется обновление статуса и даты")]
        if split:
            lines += ["Подтверждение Яндекс Пэй: " + ("получено" if record.get("receipt_json") else "ожидаем данные операции"),
                      "Фискальный чек: касса подключена; регистрацию через API Яндекс Пэй проверить нельзя."]
        else:
            lines += ["Чек ЮKassa: " + {"succeeded":"зарегистрирован","pending":"регистрируется","canceled":"ОШИБКА регистрации — требуется ручная проверка"}.get(record["receipt_status"],"статус требует проверки")]
            lines += ["Картинка чека: " + ("получена" if record.get("receipt_json") else "ожидаем реквизиты зарегистрированного чека")]
    if record.get("receipt_error"): lines += [record["receipt_error"]]
    if record.get("note"): lines += ["", "Комментарий: " + record["note"]]
    if record.get("last_error") and not (r.get('kind')=='receipt_request' and outcome in {'ready','processing','receipt_pending'}): lines += ["", record["last_error"]]
    review = record.get("gift_review") or {}
    if review:
        lines += ["", "Уточнение о шарфе:"]
        for index,item in enumerate(review["items"],1):
            choice={"gift":"подарок — без денежного возврата","paid":"куплен — нужна оплаченная позиция в чеке",None:"Это подарок?"}.get(item.get("decision"),"Нужно уточнение")
            lines.append(f'{index}. {item["name"]} · {choice}')
    if record.get("chargeback_at"):
        lines += ["", "Чарджбек — отметка владельца: клиент обратился в банк, с нашей стороны возврат провести нельзя."]
    if record.get('checked_at'):
        checked=datetime.fromisoformat(record['checked_at']).astimezone(MOSCOW)
        lines += ['', f'Последняя проверка: {checked:%d.%m.%Y %H:%M:%S} МСК']
    text = "\n".join(lines)
    if len(text) > 3900: raise RefundError("Состав заявки слишком большой для одной карточки. Разделите документы возврата.")
    return text


class RefundUI:
    def __init__(self,path,base,token,navigation,reset=None):
        self.service = RefundService(path,base,token)
        self.store = self.service.store
        self.navigation, self.pending, self.note_requests = navigation, set(), {}
        self.reset = reset
        self.locks = {}
        self.screen_lock = asyncio.Lock()
        self.decision_notice_lock = asyncio.Lock()
        self.wakeup = asyncio.Event()
        self.poll_interval = 5
        self.owner_retry_at = 0
        from refund_receipt_requests import ReceiptRequests
        self.receipt_requests=ReceiptRequests(self)

    def defer_owner(self,error):
        delay=error.retry_after
        seconds=delay.total_seconds() if hasattr(delay,"total_seconds") else float(delay)
        self.owner_retry_at=max(self.owner_retry_at,time.monotonic()+seconds+1)
        self.store.defer_owner_until(time.time()+seconds+1)
        LOG.info("Owner card updates deferred by Telegram for %s seconds",seconds)

    def card_hash(self,record):
        record=self.receipt_requests.annotate(record)
        return hashed({"text":card(record),"keyboard":self.keyboard(record).to_dict()})

    @staticmethod
    def owner(update):
        return bool(OWNER and update.effective_user and update.effective_user.id == OWNER and update.effective_chat and update.effective_chat.type == "private")

    def keyboard(self, record):
        record=self.receipt_requests.annotate(record)
        n, rev = record["id"], record["revision"]
        rows = []
        receipt_blocked=record['request'].get('kind')=='receipt_request' and record.get('receipt_outcome') in {'ready','processing','receipt_pending'}
        related=record.get('related_refund')
        if related:
            rows.append([('Открыть возврат по этому заказу',f"cr:view:{related['id']}:{related['revision']}")])
            if related['state']=='succeeded' and related.get('receipt_json'):rows.append([('🧾 Чек',f"cr:receipt:{related['id']}:{related['revision']}")])
        if record['request'].get('kind')=='receipt_request':rows.append([('🔄 Проверить готовность чека',f'cr:receiptcheck:{n}:{rev}')])
        if waiting_funds(record):rows.append([("Повторить проверку и подготовить возврат",f"cr:retryfunds:{n}:{rev}")])
        if record["state"] in EDITABLE and not record.get("chargeback_at") and not receipt_blocked:
            unknown=record['request'].get('method')=='unsupported' and not record.get('attempted_at') and not record.get('snapshot')
            if unknown:rows.append([('ЮKassa',f'cr:method:{n}:{rev}:yookassa'),('Яндекс Пэй / Сплит / СБП',f'cr:method:{n}:{rev}:yandex_split')])
            if not unknown and record['state'] in {'new','needs_info'} and not record['request'].get('scope') and not record.get('attempted_at'):
                for title,scope in (("Весь заказ с доставкой","all"),("Все вещи без доставки","goods"),("Только доставка","delivery"),("Только вещи из выбранного документа","part")):
                    rows.append([(title,f"cr:scope:{n}:{rev}:{scope}")])
            for index,item in enumerate((record.get("gift_review") or {}).get("items",[])):
                prefix = f'Шарф {index+1}: ' if len(record["gift_review"]["items"])>1 else ""
                rows.append([(prefix+"Да, подарок",f"cr:gift:{n}:{rev}:{index}:gift"),(prefix+"Нет, куплен",f"cr:gift:{n}:{rev}:{index}:paid")])
            if not unknown:rows.append([("Оформить возврат" if record.get('receipt_outcome')=='not_refunded' else "🔎 Проверить документ и платёж",f"cr:prepare:{n}:{rev}")])
            if record["state"] == "ready": rows.append([(("Подтвердить отправку документа · " if (record.get("snapshot") or {}).get("existing_refund") else "Провести возврат · ") + rubles(record["amount_cents"]),f"cr:confirm:{n}:{rev}")])
            rows.append([("Отклонить",f"cr:reject:{n}:{rev}"),("Уточнить / дать информацию",f"cr:note:{n}:{rev}")])
        if record["state"] in ACTIVE or record["state"] == "succeeded" or (record["state"]=="failed" and record.get("attempted_at")):
            rows.append([("🔄 Обновить",f"cr:check:{n}:{rev}")])
        if record["state"] == "succeeded" or (is_yandex(record) and record["state"] == "pending" and record.get("receipt_json")):
            rows.append([("📄 Документ" if is_yandex(record) else "🧾 Чек",f"cr:receipt:{n}:{rev}")])
        if completed_chargeback_available(record) or (record["state"] == "failed" and record["refund_status"] in {"canceled","FAIL"} and record["attempted_at"] and record["refund_id"] and not record.get("chargeback_at")):
            rows.append([("Чарджбек",f"cr:chargeback:{n}:{rev}")])
        rows.append([("← Все заявки", "cr:home")])
        rows.append([("🗑 Удалить сообщение", f"cr:delete:{n}:{rev}")])
        rows.append([("🏠 Главное меню", "menu:main")])
        return buttons(rows)

    async def send_card(self, record, context):
        async with self.screen_lock:
            record = self.store.get(record["id"])
            record=self.receipt_requests.annotate(record)
            if record["owner_message_id"] == 0: return
            text, markup = card(record), self.keyboard(record)
            fingerprint=self.card_hash(record)
            if record.get("owner_card_hash")==fingerprint:
                if record.get("owner_card_pending"):self.store.update(record["id"],owner_card_pending=0)
                return
            if time.monotonic()<self.owner_retry_at or time.time()<self.store.owner_retry_until():
                self.store.update(record["id"],owner_card_pending=1)
                return
            if record["owner_message_id"]:
                try:
                    await context.bot.edit_message_text(chat_id=OWNER,message_id=record["owner_message_id"],text=text,reply_markup=markup,parse_mode=None)
                    self.store.update(record["id"],owner_card_hash=fingerprint,owner_card_pending=0)
                    return
                except RetryAfter as error:
                    self.defer_owner(error);self.store.update(record["id"],owner_card_pending=1)
                    return
                except BadRequest as error:
                    if "message is not modified" in str(error).lower():
                        self.store.update(record["id"],owner_card_hash=fingerprint,owner_card_pending=0)
                        return
                    if "message to edit not found" not in str(error).lower(): raise
                    self.store.hide_owner_card(record["owner_message_id"])
                    return
            try:message = await context.bot.send_message(chat_id=OWNER,text=text,reply_markup=markup,parse_mode=None)
            except RetryAfter as error:
                self.defer_owner(error);self.store.update(record["id"],owner_card_pending=1)
                return
            self.store.show_owner_card(record["id"], message.message_id)
            self.store.update(record["id"],owner_card_hash=fingerprint,owner_card_pending=0)

    async def open_card(self, record, update):
        self.pending.discard(OWNER); self.note_requests.pop(OWNER,None)
        async with self.screen_lock:
            record = self.store.get(record["id"])
            record=self.receipt_requests.annotate(self.service.resolve_method(record))
            if record['request'].get('method')=='unsupported' and record['state'] in EDITABLE and not record.get('attempted_at') and not record.get('snapshot') and record.get('receipt_outcome') not in {'ready','processing','receipt_pending'}:
                self.pending.add(OWNER);self.note_requests[OWNER]={'id':record['id'],'revision':record['revision'],'action':'method'}
            try:
                await update.callback_query.edit_message_text(card(record),reply_markup=self.keyboard(record),parse_mode=None)
            except BadRequest as error:
                if "message is not modified" not in str(error).lower(): raise
            message_id = getattr(update.effective_message,"message_id",None)
            if isinstance(message_id,int): self.store.show_owner_card(record["id"],message_id)
            self.store.update(record["id"],owner_card_hash=self.card_hash(record),owner_card_pending=0)

    async def delete_card(self,record,update):
        self.pending.discard(OWNER);self.note_requests.pop(OWNER,None)
        async with self.screen_lock:
            message_id=getattr(update.effective_message,"message_id",None)
            if isinstance(message_id,int):self.store.hide_owner_card(message_id)
            try:await update.callback_query.delete_message()
            except BadRequest as error:
                if "message to delete not found" not in str(error).lower():raise

    async def send_decision_notices(self, context, number=None):
        async with self.decision_notice_lock:
            for notice in self.store.pending_decision_notices(number):
                if notice["source_chat"] != SUPPORT_CHAT: continue
                if notice["state"] == "insufficient_funds":
                    text=f'Заказ {notice["order_number"]} · возврат #{notice["request_id"]}\n\n{notice["note"]}'
                    if not notice.get("owner_message_id") and time.time()>=self.store.owner_retry_until():
                        try:
                            record=self.store.get(notice["request_id"])
                            message=await context.bot.send_message(chat_id=OWNER,text=text,parse_mode=None,
                                reply_markup=buttons([[("Открыть возврат",f'cr:view:{record["id"]}:{record["revision"]}')],[("Ожидает денежных средств","cr:funds")]]))
                            self.store.mark_funds_owner_notice(notice["id"],message.message_id)
                        except RetryAfter as error:self.defer_owner(error)
                        except TelegramError as error:LOG.warning("Refund funds owner delivery failed: %s",type(error).__name__)
                elif notice["state"] == "chargeback":
                    text = (f'⚠️ Чарджбек · заказ {notice["order_number"]}\nЗаявка #{notice["request_id"]}\n\n'
                            f'{notice["note"]}\n\nУведомление владельца.')
                else:
                    title = "❌ Возврат отклонён" if notice["state"] == "rejected" else "✏️ Нужно уточнение для возврата"
                    text = (f'{title} · заказ {notice["order_number"]}\n'
                        f'Заявка #{notice["request_id"]}\n\nПричина: {notice["note"]}\n\n'
                        'Исправьте указанную причину и отправьте новую заявку отдельным сообщением в эту же тему: '
                        'номер заказа, ФИО, email, телефон, способ оплаты (ЮKassa, Сплит или Яндекс СБП) и вариант возврата.\n'
                        'Выберите один вариант: «за вещь», «за доставку», «за весь заказ» или «за часть товаров». '
                        'Документ МойСклад должен содержать соответствующие товары, размеры и количество.\n'
                        'После новой проверки потребуется подтверждение владельца. По этой заявке деньги не отправлены.')
                if notice["message_id"] is not None:continue
                try:
                    try:
                        message = await context.bot.send_message(chat_id=SUPPORT_CHAT,message_thread_id=SUPPORT_TOPIC,
                            reply_to_message_id=notice["source_message"],text=text,parse_mode=None)
                    except BadRequest as error:
                        detail = str(error).lower()
                        if not any(word in detail for word in ("reply", "replied")) or "not found" not in detail: raise
                        message = await context.bot.send_message(chat_id=SUPPORT_CHAT,message_thread_id=SUPPORT_TOPIC,text=text,parse_mode=None)
                    self.store.mark_decision_notice(notice["id"],message.message_id)
                except TelegramError as error:
                    LOG.warning("Refund decision delivery failed: %s",type(error).__name__)

    async def topic_result(self, record, context):
        # One completion photo with all results; its durable ID is independent
        # of transient statuses/errors and survives worker retries/restarts.
        record = self.store.get(record["id"])
        if is_yandex(record) and record["state"] in {"pending","failed"}:
            return await self.split_progress(record,context)
        if record["state"] != "succeeded" or not record["ms_synced"] or not record.get("receipt_json") or record.get("completed_message_id"):
            return
        if (record.get("snapshot") or {}).get("existing_refund") and (record.get("approved_by")!=OWNER or not record.get("attempted_at")):
            return
        split = is_yandex(record)
        existing=(record.get("snapshot") or {}).get("existing_refund")
        stamp=existing.get("ms_date") if existing else record.get("split_booked_at") or record["succeeded_at"]
        date = datetime.fromisoformat(stamp).astimezone(MOSCOW).strftime("%d.%m.%Y %H:%M") if stamp else None
        caption = (f'✅ Возврат проведён · заказ {record["request"]["order_number"]}\n'
                   f'Сумма: {rubles(record["amount_cents"])}\n'
                   'МойСклад: Возврат-проведен.' + (f' Дата: {date} МСК.' if date else '') + '\n' +
                   ("Подтверждение операции "+yandex_label(record)+". Не является фискальным чеком." if split else "Чек возврата зарегистрирован в ЮKassa."))
        message_id = record.get("receipt_support_message_id")
        if message_id:
            try:
                if split and record.get("split_notice_status") == "PENDING":
                    picture = await asyncio.to_thread(render_refund,record)
                    try: await context.bot.edit_message_media(chat_id=SUPPORT_CHAT,message_id=message_id,media=InputMediaPhoto(media=picture,caption=caption,parse_mode=None))
                    finally: picture.close()
                else:
                    await context.bot.edit_message_caption(chat_id=SUPPORT_CHAT,message_id=message_id,caption=caption,parse_mode=None)
            except BadRequest as error:
                if "message is not modified" not in str(error).lower():
                    if "message to edit not found" not in str(error).lower(): raise
                    message_id = None
        if not message_id:
            picture = await asyncio.to_thread(render_refund,record)
            try:
                message = await self.support_photo(record,context,picture,caption)
                message_id = message.message_id
            finally: picture.close()
        fields = {"completed_message_id":message_id,"receipt_support_message_id":message_id}
        if split: fields["split_notice_status"] = "SUCCESS"
        self.store.update(record["id"],**fields)

    async def support_photo(self,record,context,picture,caption):
        try:
            return await context.bot.send_photo(chat_id=SUPPORT_CHAT,message_thread_id=SUPPORT_TOPIC,
                reply_to_message_id=record["source_message"],photo=picture,caption=caption,parse_mode=None)
        except BadRequest as error:
            detail=str(error).lower()
            if not any(word in detail for word in ("reply","replied")) or "not found" not in detail:raise
            picture.seek(0)
            return await context.bot.send_photo(chat_id=SUPPORT_CHAT,message_thread_id=SUPPORT_TOPIC,
                photo=picture,caption=caption,parse_mode=None)

    async def split_progress(self, record, context):
        proof = record.get("receipt_json") or {}
        status = proof.get("status")
        if status not in {"PENDING","FAIL"} or record.get("split_notice_status") == status: return
        if status == "PENDING":
            self.service.split.pending_identity(record)
            existing=(record.get("snapshot") or {}).get("existing_refund")
            if not record["ms_synced"] or (not existing and not record.get("split_booked_at")): return
            stamp=existing.get("ms_date") if existing else record["split_booked_at"]
            date = datetime.fromisoformat(stamp).astimezone(MOSCOW).strftime("%d.%m.%Y %H:%M") if stamp else None
            caption = (f'⏳ Возврат в процессе · заказ {record["request"]["order_number"]}\nСумма: {rubles(record["amount_cents"])}\n'
                       f'Яндекс принял операцию, завершение возврата ещё не подтверждено.\n'
                       'МойСклад: Возврат-проведен — отметка оформления'+(f', {date} МСК.' if date else '.')+'\n'
                       'Подтверждение приёма операции. Не является фискальным чеком.')
        else:
            if not record.get("receipt_support_message_id"): return
            caption = (f'❌ Яндекс не выполнил возврат · заказ {record["request"]["order_number"]}\n'
                       f'Сумма: {rubles(record["amount_cents"])}\n'
                       'В МойСклад остаётся внутренняя отметка оформления. Проверьте отказ с владельцем; повторная операция не отправлена.\n'
                       'Не является фискальным чеком.')
        message_id = record.get("receipt_support_message_id")
        picture = await asyncio.to_thread(render_refund,record)
        try:
            if message_id:
                try: await context.bot.edit_message_media(chat_id=SUPPORT_CHAT,message_id=message_id,media=InputMediaPhoto(media=picture,caption=caption,parse_mode=None))
                except BadRequest as error:
                    if "message is not modified" not in str(error).lower(): raise
            else:
                message = await self.support_photo(record,context,picture,caption)
                message_id = message.message_id
        finally: picture.close()
        self.store.update(record["id"],receipt_support_message_id=message_id,split_notice_status=status)

    async def lookup_result(self, record, context):
        # Lookup details are shown in the private owner card only.
        return

    async def send_receipt(self, record, context, force_owner=False):
        if not force_owner or not record.get("receipt_json"): return
        split = is_yandex(record)
        pending = split and record["state"] == "pending"
        if record["state"] != "succeeded" and not pending: return
        if pending: self.service.split.pending_identity(record)
        caption = ("Подтверждение возврата Яндекс Пэй" if split else "Чек возврата") + " · заказ " + record["request"]["order_number"] + " · " + rubles(record["amount_cents"])
        if pending: caption = "Возврат "+yandex_label(record)+" в процессе · заказ " + record["request"]["order_number"] + " · " + rubles(record["amount_cents"])
        picture = await asyncio.to_thread(render_refund,record)
        try: message = await context.bot.send_photo(chat_id=OWNER,photo=picture,caption=caption + ("\nНе является фискальным чеком." if split else "\nПо данным зарегистрированного чека ЮKassa."),parse_mode=None)
        finally: picture.close()
        self.store.update(record["id"],receipt_owner_message_id=message.message_id)

    async def refresh_outcome(self, record, context, force_owner_receipt=False, notify_support=False):
        await self.lookup_result(record,context)
        if notify_support or waiting_funds(record):await self.send_decision_notices(context,record["id"])
        if record["state"] == "succeeded" or (is_yandex(record) and record["state"] == "pending"):
            try: record = await self.service.receipt(record)
            except RefundError as error: record = self.store.update(record["id"],receipt_error=str(error))
            except Exception as error:
                LOG.warning("Refund receipt fetch failed: %s",type(error).__name__)
                record = self.store.update(record["id"],receipt_error="Не удалось получить реквизиты чека. Повторим получение позже; деньги повторно не возвращаются.")
        # Only the background reconciler publishes completion to support. Send
        # it before editing the private card so a private-chat error cannot
        # prevent delivery of the completed refund and receipt.
        if notify_support:
            await self.topic_result(self.store.get(record["id"]),context)
        await self.send_card(record,context)
        await self.send_receipt(self.store.get(record["id"]),context,force_owner_receipt)

    async def external(self, update, context):
        message = update.effective_message
        if not SUPPORT_CHAT or not SUPPORT_TOPIC or not message or message.chat.id != SUPPORT_CHAT or message.message_thread_id != SUPPORT_TOPIC: return
        if not message.from_user or getattr(message.from_user,"is_bot",False) or message.from_user.id == context.bot.id: raise ApplicationHandlerStop
        from refund_receipt_requests import parse_receipt_request
        text=message.text or message.caption or ""
        if parse_receipt_request(text):
            try:
                job,created=self.receipt_requests.store.add(message.chat.id,message.message_id,text,message.from_user.id)
                if created or job['state']!='sent':context.application.create_task(self.receipt_requests.process(job['id'],context),update=update)
            except RefundError as error:await message.reply_text(str(error),parse_mode=None)
            raise ApplicationHandlerStop
        record, changed = self.store.ingest(message.chat.id,message.message_id,message.text or message.caption or "",message.from_user.id if message.from_user else None)
        if record and changed:
            await self.send_card(record,context)
            context.application.create_task(self.prepare_incoming(record["id"],context),update=update)
        raise ApplicationHandlerStop

    async def prepare_incoming(self, number, context):
        async with self.locks.setdefault(number,asyncio.Lock()):
            record = self.store.get(number)
            try:
                record=self.service.resolve_method(record)
                order_id = (record.get("gift_review") or {}).get("document_id")
                if not order_id:
                    docs = await self.service.candidates(record)
                    if len(docs) != 1:
                        raise RefundError("Подходящих документов возврата/отмены: " + str(len(docs)) + ". Откройте проверку и выберите документ либо уточните его статус в МойСклад.")
                    order_id=docs[0]["id"]
                snapshot = await self.service.preview(record,order_id)
                self.store.prepare(number,record["revision"],snapshot)
            except RefundError as error:
                self.store.preparation_error(number,record["revision"],error)
            except Exception as error:
                LOG.warning("Refund preparation failed: %s",type(error).__name__)
                self.store.preparation_error(number,record["revision"],"Не удалось проверить заявку. Повторите проверку.")
            await self.send_card(self.store.get(number),context)
            await self.lookup_result(self.store.get(number),context)
            await self.topic_result(self.store.get(number),context)

    async def home(self, update, context, page=0, funds=False):
        if not self.owner(update):
            await update.effective_message.reply_text("Возвраты средств доступны только владельцу в личном чате."); return
        if self.reset: self.reset(update,context)
        self.pending.discard(OWNER); self.note_requests.pop(OWNER,None)
        waiting=self.store.waiting_for_funds()
        records = waiting if funds else self.store.list()
        text = ("Ожидает денежных средств\n\nЮKassa подтвердила отмену этих операций из-за недостатка средств. После пополнения выберите заявку для новой проверки и подтверждения суммы." if funds else "💳 Возвраты средств\n\nЗаявки поступают из темы поддержки. Выберите заявку для проверки.")
        if not records: text += "\n\nНовых заявок пока нет."
        page = max(0,min(page,max(0,(len(records)-1)//10)))
        rows = [[(f'#{r["id"]} · {r["request"]["order_number"]} · '+("Ожидает денежных средств" if waiting_funds(r) else STATE[r["state"]]),f'cr:view:{r["id"]}:{r["revision"]}')] for r in records[page*10:page*10+10]]
        action="funds" if funds else "home"
        if page: rows.append([("←",f"cr:{action}:{page-1}")])
        if (page+1)*10 < len(records): rows.append([("→",f"cr:{action}:{page+1}")])
        if funds:rows.append([("← Все заявки","cr:home")])
        else:rows.insert(0,[(f"Ожидает денежных средств · {len(waiting)}","cr:funds")])
        rows.append([("Инструкция поддержки", "cr:help")])
        rows.append([("🏠 Главное меню", "menu:main")])
        if update.callback_query:
            async with self.screen_lock:
                message_id = getattr(update.effective_message,"message_id",None)
                if isinstance(message_id,int): self.store.hide_owner_card(message_id)
                try: await update.callback_query.edit_message_text(text,reply_markup=buttons(rows),parse_mode=None)
                except BadRequest as error:
                    if "message is not modified" not in str(error).lower(): raise
        else: await update.effective_message.reply_text(text,reply_markup=buttons(rows),parse_mode=None)

    async def callback(self, update, context):
        if not self.owner(update):
            await update.callback_query.answer("Подтверждает только владелец в личном чате.",show_alert=True);raise ApplicationHandlerStop
        retry_until=self.store.owner_retry_until()
        paused=retry_until>time.time()
        try:
            if paused:
                until=datetime.fromtimestamp(retry_until,MOSCOW).strftime("%d.%m %H:%M")
                await update.callback_query.answer("Telegram ограничил обновление карточек до "+until+" МСК. Обновление сохранено в очереди; повторного возврата денег не будет.",show_alert=True)
            else:await update.callback_query.answer('Проверяю статус возврата…' if update.callback_query.data.startswith(('cr:check:','cr:receiptcheck:')) else None)
        except RetryAfter as error:self.defer_owner(error)
        bits=update.callback_query.data.split(":")
        if paused and len(bits)>2 and bits[1] in {"view","confirm","execute","publish"} and bits[2].isdigit():
            record=self.store.get(int(bits[2]))
            if record["owner_message_id"]!=0:self.store.update(record["id"],owner_card_pending=1)
            raise ApplicationHandlerStop
        context.application.create_task(self.dispatch_safe(update,context),update=update)
        raise ApplicationHandlerStop

    async def dispatch_safe(self, update, context):
        bits = update.callback_query.data.split(":")
        try:
            if bits[1] in {"home","funds"}: return await self.home(update,context,int(bits[2]) if len(bits)>2 else 0,funds=bits[1]=="funds")
            if bits[1] == "help":
                return await update.effective_message.reply_text(GUIDE,parse_mode=None)
            number = int(bits[2])
            if bits[1]=='check':
                # Bind the clicked old card before any slow provider lookup.
                # A running check already owns the lock; do not queue duplicates.
                await self.open_card(self.store.get(number),update)
                lock=self.locks.setdefault(number,asyncio.Lock())
                if lock.locked():
                    self.store.update(number,owner_card_pending=1)
                    self.wakeup.set()
                    return
                async with lock:
                    return await self.dispatch(update,context,bits)
            if bits[1]=='receiptcheck':
                await self.receipt_requests.refresh_for_refund(number,context)
                return await self.open_card(self.store.get(number),update)
            if bits[1]=='method':
                record=self.store.get(number)
                if record['revision']!=int(bits[3]) or record['request'].get('method')!='unsupported':return await self.open_card(record,update)
                return await self.choose_provider(record,bits[4],update,context)
            if bits[1]=="delete":return await self.delete_card(self.store.get(number),update)
            # Reading a card and opening confirmation never wait for the
            # reconciler's network requests. Execution still uses its lock,
            # a fresh financial snapshot and the atomic journal claim.
            if bits[1] in {"view","confirm"}:return await self.dispatch(update,context,bits)
            async with self.locks.setdefault(number,asyncio.Lock()):
                await self.dispatch(update,context,bits)
        except RetryAfter as error:
            self.defer_owner(error)
            if len(bits)>2 and bits[2].isdigit():self.store.update(int(bits[2]),owner_card_pending=1)
        except RefundError as error:
            try:await update.effective_message.reply_text(str(error),parse_mode=None)
            except RetryAfter as delayed:
                self.defer_owner(delayed)
                if len(bits)>2 and bits[2].isdigit():self.store.update(int(bits[2]),last_error=str(error),owner_card_pending=1)
        except Exception as error:
            LOG.warning("Refund callback failed: %s",type(error).__name__)
            try:await update.effective_message.reply_text("Не удалось завершить действие. Откройте заявку и проверьте её статус; повторный денежный запрос автоматически не отправляется.")
            except RetryAfter as delayed:self.defer_owner(delayed)

    async def confirm_card(self, record, update, changed=False):
        if record["state"] != "ready" or not record["snapshot"]:
            return await self.open_card(record,update)
        number,rev=record["id"],record["revision"]
        message_id=getattr(update.effective_message,"message_id",None)
        if isinstance(message_id,int):self.store.show_owner_card(number,message_id)
        prefix="Данные возврата изменились. Подтвердите актуальную сумму и состав.\n\n" if changed else ""
        existing=record["snapshot"].get("existing_refund")
        action="publish" if existing else "execute"
        label="Да, отправить чек сотрудникам" if existing else "Да, вернуть " + rubles(record["amount_cents"])
        prompt=("Подтвердите отправку сотрудникам документа об операции Яндекса. Возврат ещё в обработке; его результат обновится автоматически." if existing and existing["status"]=="pending" else "Подтвердите отправку сотрудникам документа уже проведённого возврата. Повторная денежная операция не создаётся." if existing else "Подтвердите возврат указанной суммы на исходный платёж клиента.")
        try:
            return await update.callback_query.edit_message_text(prefix+card(record)+"\n\n"+prompt,parse_mode=None,
                reply_markup=buttons([[(label,f"cr:{action}:{number}:{rev}:{confirmation_token(record)}")],[("Отмена",f"cr:view:{number}:{rev}")]]))
        except BadRequest as error:
            if "message is not modified" not in str(error).lower():raise

    async def dispatch(self, update, context, bits):
        action, number, rev = bits[1],int(bits[2]),int(bits[3])
        record = self.store.get(number)
        if action == "view": return await self.open_card(record,update)
        if action == "delete": return await self.delete_card(record,update)
        if action == "confirm": return await self.confirm_card(record,update)
        if action in {'prepare','scope','doc'} and self.receipt_requests.annotate(record).get('receipt_outcome') in {'ready','processing','receipt_pending'} and record['request'].get('kind')=='receipt_request':return await self.open_card(record,update)
        if action in {"execute","publish"}:
            if record["attempted_at"]:
                return await self.refresh_outcome(await self.service.check(record),context)
            if record["state"] != "ready": return await self.open_card(record,update)
            if (action=="publish")!=bool((record.get("snapshot") or {}).get("existing_refund")):
                return await self.confirm_card(record,update,changed=True)
            token=bits[4] if len(bits)>4 else None
            if (token is not None and (not record["snapshot"] or token!=confirmation_token(record))) or (token is None and record["revision"]!=rev):
                return await self.confirm_card(record,update,changed=True)
            rev=record["revision"]
        if action in {"check","receipt"}:rev=record["revision"]
        if record["revision"] != rev: raise RefundError("Карточка устарела после правки заявки. Откройте новую версию.")
        if action == "scope":
            record=self.store.clarify_scope(record,bits[4],OWNER)
            rev=record['revision'];action='prepare'
        if action == "retryfunds":
            await self.service.retry_after_funds(number,rev,OWNER)
            self.wakeup.set()
        elif action == "gift":
            record=self.store.choose_gift(number,rev,OWNER,int(bits[4]),bits[5])
            message_id=getattr(update.effective_message,"message_id",None)
            if isinstance(message_id,int):self.store.show_owner_card(number,message_id)
            self.wakeup.set()
            try:
                snapshot=await self.service.preview(record,record["gift_review"]["document_id"])
                self.store.prepare(number,record["revision"],snapshot)
            except RefundError as error:
                self.store.preparation_error(number,record["revision"],error)
        elif action in {"prepare","doc"}:
            if record["attempted_at"]: raise RefundError("Возврат уже начинался. Используйте проверку результата.")
            record=self.service.resolve_method(record)
            if action == "prepare":
                docs = await self.service.candidates(record)
                if not docs: raise RefundError("Нет документа со статусом возврата/отмены. Обычный оплаченный заказ не изменяется.")
                if len(docs) != 1:
                    if len(docs) > 25: raise RefundError("Найдено больше 25 документов возврата. Уточните статусы лишних копий в МойСклад и повторите проверку.")
                    context.user_data.setdefault("refund_candidates",{})[f"{number}:{rev}"] = [d["id"] for d in docs]
                    rows = [[(f'{d["name"]} · {d["state"]["name"]} · {rubles(int(d["sum"]))}',f'cr:doc:{number}:{rev}:{i}')] for i,d in enumerate(docs[:25])]
                    return await update.effective_message.reply_text("Есть несколько документов с этим номером. Выберите подготовленный для возврата:",reply_markup=buttons(rows),parse_mode=None)
                order_id = docs[0]["id"]
            else:
                ids = context.user_data.get("refund_candidates",{}).get(f"{number}:{rev}",[])
                if not 0 <= int(bits[4]) < len(ids): raise RefundError("Список документов устарел. Выполните проверку заново.")
                order_id = ids[int(bits[4])]
            try:
                snapshot = await self.service.preview(record,order_id)
                self.store.prepare(number,rev,snapshot)
            except RefundError as error:
                self.store.preparation_error(number,rev,error)
                await self.send_card(self.store.get(number),context);raise
            finally: await self.lookup_result(self.store.get(number),context)
        elif action == "publish":
            from completed_refunds import approve_completed
            try:
                await approve_completed(self.service,number,rev,OWNER)
            except RefundError as error:
                self.store.update(number,last_error=str(error))
            finally:
                self.wakeup.set()
                await self.refresh_outcome(self.store.get(number),context)
            return
        elif action == "execute":
            if record["state"] != "ready" or record["attempted_at"]:
                return await self.refresh_outcome(await self.service.check(record),context)
            message_id = getattr(update.effective_message,"message_id",None)
            if isinstance(message_id,int): self.store.update(number,owner_message_id=message_id)
            await update.callback_query.edit_message_text("⏳ Оформляем возврат по заказу " + record["request"]["order_number"] + ". Проверяем данные и ответ платёжного сервиса.",parse_mode=None)
            try:
                await self.service.execute(number,rev,OWNER)
            except RefundError as error:
                self.store.update(number,last_error=str(error))
            finally:
                self.wakeup.set()
                current = self.store.get(number)
                await self.refresh_outcome(current,context)
            return
        elif action == "check":
            await self.service.check(record,force=True)
            self.store.update(number,checked_at=now())
            return await self.refresh_outcome(self.store.get(number),context)
        elif action == "chargeback":
            if record.get("completed_review"):
                from completed_refunds import mark_completed_chargeback
                await mark_completed_chargeback(self.service,record,OWNER)
            else:self.store.chargeback(number,rev,OWNER)
            self.wakeup.set()
            await self.send_decision_notices(context,number)
        elif action == "receipt":
            if record["state"] != "succeeded" and not (is_yandex(record) and record["state"] == "pending"):
                raise RefundError("Документ доступен после подтверждённого приёма операции или успешного возврата.")
            await self.refresh_outcome(record,context,force_owner_receipt=True)
            if not self.store.get(number).get("receipt_json"):
                await update.effective_message.reply_text("Документ ещё не готов. Нажмите «Обновить», затем откройте его кнопкой в карточке.")
            return
        elif action in {"reject","note"}:
            if record["attempted_at"]: raise RefundError("Денежный возврат уже начинался. Сначала проверьте результат.")
            self.pending.add(OWNER); self.note_requests[OWNER] = {"id":number,"revision":rev,"action":action}
            return await update.effective_message.reply_text("Напишите причину отклонения — отправлю её сотрудникам в тему поддержки." if action == "reject" else "Напишите, что нужно исправить — отправлю сотрудникам в тему поддержки. Для отмены откройте «💳 Возвраты средств».")
        else: raise RefundError("Неизвестная кнопка возврата.")
        await self.send_card(self.store.get(number),context)

    async def choose_provider(self,record,method,update,context):
        shown=self.receipt_requests.annotate(record)
        if shown.get('receipt_outcome') in {'ready','processing','receipt_pending'}:
            if getattr(update,'callback_query',None):return await self.open_card(shown,update)
            return await self.send_card(shown,context)
        record=self.store.choose_method(record,method,OWNER)
        self.pending.discard(OWNER);self.note_requests.pop(OWNER,None)
        if record['request'].get('kind')=='receipt_request':
            record=await self.receipt_requests.refresh_for_refund(record['id'],context)
            if record.get('receipt_outcome') in {'ready','processing','receipt_pending'} or not record['request'].get('scope'):
                if getattr(update,'callback_query',None):return await self.open_card(record,update)
                return await self.send_card(record,context)
        await self.prepare_incoming(record['id'],context)

    async def input(self, update, context):
        if not self.owner(update): return
        text = update.effective_message.text or ""
        if self.navigation(text) or text.startswith("/"):
            self.pending.discard(OWNER);self.note_requests.pop(OWNER,None);return
        pending = self.note_requests.get(OWNER)
        if not pending: self.pending.discard(OWNER);return
        try:
            if not text.strip() or len(text) > 1000: raise RefundError("Комментарий — от 1 до 1 000 символов.")
            if pending['action']=='method':
                method=payment_method_from_label(text.strip())
                if not method:raise RefundError('Выберите ЮKassa или Яндекс Пэй кнопкой в карточке либо напишите «ЮKassa» / «Сплит».')
                record=self.store.get(pending['id'])
                if record['request'].get('method')!='unsupported':
                    self.pending.discard(OWNER);self.note_requests.pop(OWNER,None)
                    await self.send_card(record,context)
                    raise ApplicationHandlerStop
                if record['revision']!=pending['revision']:raise RefundError('Заявка изменилась. Откройте её текущую карточку.')
                await self.choose_provider(record,method,update,context)
                raise ApplicationHandlerStop
            record = self.store.decision(pending["id"],pending["revision"],OWNER,"rejected" if pending["action"] == "reject" else "needs_info",text.strip())
            self.pending.discard(OWNER);self.note_requests.pop(OWNER,None)
            self.wakeup.set()
            await self.send_decision_notices(context,record["id"])
            await self.send_card(self.store.get(record["id"]),context)
        except RefundError as error:
            await update.effective_message.reply_text(str(error),parse_mode=None)
        raise ApplicationHandlerStop

    async def reconcile_pending(self, application):
        await self.send_decision_notices(application)
        semaphore=asyncio.Semaphore(3)
        async def reconcile_one(record):
            async with semaphore:
                if record["state"] == "new":
                    try: await self.prepare_incoming(record["id"], application)
                    except Exception as error: LOG.warning("Refund preparation recovery failed: %s",type(error).__name__)
                    return
                async with self.locks.setdefault(record["id"],asyncio.Lock()):
                    try:
                        current = self.store.get(record["id"])
                        if is_yandex(current) and current["state"] == "pending" and not (current.get("snapshot") or {}).get("existing_refund"):
                            await self.refresh_outcome(current,application,notify_support=True)
                            current = self.store.get(record["id"])
                        current = await self.service.check(current)
                        await self.refresh_outcome(current,application,notify_support=True)
                    except RefundError as error:
                        self.store.update(record["id"],last_error=str(error))
                    except Exception as error:
                        LOG.warning("Refund result check failed: %s",type(error).__name__)
        await asyncio.gather(*(reconcile_one(record) for record in self.store.list(pending=True)))
        await self.receipt_requests.reconcile(application)

    async def worker(self, application):
        while True:
            self.wakeup.clear()
            try:
                await self.reconcile_pending(application)
            except Exception as error:
                LOG.warning("Refund background scan failed: %s",type(error).__name__)
            try:
                await asyncio.wait_for(self.wakeup.wait(),timeout=self.poll_interval)
            except asyncio.TimeoutError:
                pass


class PendingRefundInput(filters.MessageFilter):
    def __init__(self,pending): super().__init__(name="PendingRefundInput");self.pending=pending
    def filter(self,message): return bool(message.from_user and message.from_user.id in self.pending and message.text)


def register_refunds(application,path,base,token,navigation,reset=None):
    ui = RefundUI(path,base,token,navigation,reset)
    application.bot_data["customer_refunds_ui"] = ui
    async def leave_note(update, context):
        ui.pending.discard(update.effective_user.id if update.effective_user else 0)
        if update.effective_user: ui.note_requests.pop(update.effective_user.id, None)
    application.add_handler(CallbackQueryHandler(leave_note,pattern=r"^(?!cr:).+"),group=-6)
    if SUPPORT_CHAT and SUPPORT_TOPIC:
        application.add_handler(MessageHandler(filters.Chat(SUPPORT_CHAT),ui.external),group=-5)
    application.add_handler(MessageHandler(filters.ChatType.PRIVATE & PendingRefundInput(ui.pending),ui.input),group=-3)
    application.add_handler(CommandHandler("refunds",ui.home))
    application.add_handler(MessageHandler(filters.Regex("^"+re.escape(MENU_CUSTOMER_REFUNDS)+"$"),ui.home))
    application.add_handler(CallbackQueryHandler(ui.callback,pattern=r"^cr:"),group=-4)
    return ui
