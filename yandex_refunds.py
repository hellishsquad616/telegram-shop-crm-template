"""Yandex Pay refunds: exact cart matching, one POST, durable GET reconciliation."""
import asyncio
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import httpx

from customer_refunds import (
    ACTIVE, OWNER, RefundError, UncertainRefund, cents, document_items, dump,
    hashed, norm, order_matches, phone, quantity, delivery, valid_contact, customer_warnings, approval_hash, selected_document_items, gift_checked_items, GiftClarification,
)

API = "https://pay.yandex.ru/api/merchant"
from refund_settings import MERCHANT


def is_yandex(record):
    return (record["request"].get("method") == "yandex_split"
            or (record.get("snapshot") or {}).get("provider") == "yandex_split"
            or str(record.get("payment_id") or "").startswith("yandex:"))


def yandex_label(record):
    method = (record.get("snapshot") or {}).get("payment_method")
    return {"SBP":"Яндекс Пэй · СБП","CARD":"Яндекс Пэй · Карта"}.get(method,"Яндекс Сплит")


def decimal_amount(value):
    return f"{value // 100}.{value % 100:02d}"


class YandexAPIError(RefundError):
    def __init__(self, status):
        super().__init__(f"Яндекс Пэй: HTTP {status}. Результат операции не подтверждён.")
        self.status_code = status


class YandexPay:
    def __init__(self, base):
        self.base = Path(base)

    def credentials(self):
        try:
            data = json.loads((self.base / ".secrets/yandex_pay.json").read_text())
            valid = (isinstance(data, dict) and bool(MERCHANT) and data.get("merchant_id") == MERCHANT and data.get("environment") == "production"
                     and re.fullmatch(r"\d+:\d{5,20}", data.get("sample_order_id", ""))
                     and isinstance(data.get("api_key"), str) and 16 <= len(data["api_key"]) <= 4096
                     and all(33 <= ord(c) <= 126 for c in data["api_key"]))
        except (OSError, ValueError, TypeError):
            valid = False
        if not valid:
            raise RefundError("Ключ Яндекс Пэй ещё не установлен или магазин не подтверждён.")
        return data

    def enabled(self):
        data = self.credentials()
        from refund_setup import require_gate
        require_gate(self.base, "yandex_pay")
        if data.get("fiscalization_verified") is not True:
            raise RefundError("Сначала подтвердите подключение кассы Яндекс Пэй.")

    def order_id(self, request):
        prefix = self.credentials()["sample_order_id"].split(":")[0]
        expected = prefix + ":" + request["order_number"]
        if request.get("yandex_order_id") and request["yandex_order_id"] != expected:
            raise RefundError("ID заказа Яндекс Пэй не соответствует номеру заявки или магазину Tilda.")
        if request.get("payment_id"):
            raise RefundError("Для Яндекс Пэй используйте yandex_order_id, а не payment_id ЮKassa.")
        return expected

    async def call(self, method, path, *, body=None, key=None):
        if method == "POST": self.enabled()
        data = self.credentials()
        async with httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False) as client:
            try:
                response = await client.request(method, API + path, json=body, headers={
                    "Authorization": "Api-Key " + data["api_key"],
                    "X-Request-Id": key or str(uuid.uuid4()), "X-Request-Timeout": "10000",
                })
            except httpx.TransportError:
                if method == "POST":
                    raise UncertainRefund("Яндекс Пэй не подтвердил результат. Проверяем операцию без повторной отправки.") from None
                raise RefundError("Яндекс Пэй временно недоступен; повторите проверку.") from None
        if response.status_code != 200:
            try: duplicate = response.json().get("reasonCode") == "DUPLICATE_EXTERNAL_OPERATION_ID"
            except (ValueError, AttributeError): duplicate = False
            if method == "POST" and (duplicate or response.status_code >= 500 or response.status_code in {408, 409, 429}):
                raise UncertainRefund("Яндекс Пэй: результат требует GET-сверки. Повторный возврат не отправляем.")
            raise YandexAPIError(response.status_code)
        try:
            result = response.json()
            if result.get("code") != 200 or result.get("status") != "success" or not isinstance(result.get("data"), dict):
                raise ValueError
            return result["data"]
        except (ValueError, AttributeError):
            if method == "POST":
                raise UncertainRefund("Не удалось прочитать результат Яндекс Пэй. Выполняем только GET-сверку.") from None
            raise RefundError("Не удалось прочитать ответ Яндекс Пэй.") from None

    async def order(self, order_id):
        return await self.call("GET", "/v1/orders/" + quote(order_id, safe=""))

    async def operation(self, external_id):
        return await self.call("GET", "/v1/operations/" + quote(external_id, safe=""))

    async def refund(self, order_id, body, external_id):
        return await self.call("POST", "/v2/orders/" + quote(order_id, safe="") + "/refund",
                               body={**body, "externalOperationId": external_id}, key=external_id)


def same_item(document_name, title):
    # Tilda and MoySklad use different names for the same CDEK delivery.
    # The caller requires quantity and a unique match; prices come from payment.
    carrier = r"\b(?:cdek|сдэк)\b"
    if delivery(document_name) and delivery(title) and re.search(carrier, norm(document_name)) and re.search(carrier, norm(title)):
        return True
    def model(name):
        value = re.split(r",\s*(?:размер|size|цвет)\b|\(", name, maxsplit=1, flags=re.I)[0]
        return norm(re.sub(r"\s+-\d+(?:[.,]\d+)?%$", "", value).rstrip(", "))
    if model(document_name) != model(title): return False
    def size(name):
        match = re.search(r"(?:размер|size)\s*[:=]?\s*([^,;\s()]+)", name, re.I)
        if not match: match = re.search(r"\(\s*([^,()]+)[,)]", name)
        return norm(match[1]) if match else ""
    left,right = size(document_name),size(title)
    if left == right: return True
    return bool(re.search(r"\bscarf\b|шарф",model(document_name)) and
                left in {"","onesize","one size","one","os","o","единый","универсальный"} and
                right in {"","onesize","one size","one","os","o","единый","универсальный"})


def cart_items(order):
    cart = order.get("cart") or {}
    if not isinstance(cart, dict): raise RefundError("Не получена корзина Яндекс Пэй.")
    total = cart.get("total") or {}
    if not isinstance(total, dict): raise RefundError("Не получен итог корзины Яндекс Пэй.")
    if order.get("shippingMethod") or cents(total.get("externalAmount") or "0") or cents(total.get("pointsAmount") or "0"):
        raise RefundError("Этот заказ использует Checkout, баллы или внешнюю оплату. Нужна отдельная сверка возврата.")
    raw_items = cart.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise RefundError("В Яндекс Пэй нет состава оставшихся оплаченных товаров.")
    items, seen = [], set()
    for raw in raw_items:
        if not isinstance(raw, dict): raise RefundError("Не получены данные позиции Яндекс Пэй.")
        pid = raw.get("productId")
        if not isinstance(pid, str) or not pid or len(pid) > 2048 or pid in seen:
            raise RefundError("Идентификаторы позиций корзины Яндекс Пэй не подтверждены.")
        seen.add(pid)
        count = quantity((raw.get("quantity") or {}).get("count"))
        amount = cents(raw.get("total"))
        if amount % count or cents(raw.get("pointsAmount") or "0"):
            raise RefundError("Не удалось точно сверить цены товаров Яндекс Пэй после скидок.")
        unit = amount // count
        if raw.get("finalPrice") is not None and cents(raw["finalPrice"]) != unit:
            raise RefundError("Цена единицы товара не совпала с итогом позиции Яндекс Пэй.")
        title = str(raw.get("title") or "").strip()
        receipt = raw.get("receipt") or {}
        if not title or not isinstance(receipt,dict):
            raise RefundError("В Яндекс Пэй не подтверждены данные позиции корзины.")
        if amount==0 and not re.search(r"\bscarf\b|шарф",norm(title)):
            raise RefundError("Не подтверждена положительная цена позиции Яндекс Пэй.")
        # v2 refunds use original product IDs/quantities. Fiscal fields can be
        # absent for SPLIT and SBP; they are never fabricated in the request.
        if receipt.get("title") and receipt["title"] != title:
            raise RefundError("Название фискальной позиции отличается от товара в Яндекс Пэй.")
        items.append({"product_id": pid, "title": title, "quantity": count, "unit_cents": unit, "receipt": receipt})
    if sum(i["quantity"] * i["unit_cents"] for i in items) != cents(total.get("amount")) or cents(total.get("amount")) != cents(order.get("orderAmount")):
        raise RefundError("Итог корзины, сумма заказа и цены позиций Яндекс Пэй не совпали.")
    return items


def validate_order(order, order_id):
    if (not isinstance(order, dict) or order.get("merchantId") != MERCHANT or order.get("orderId") != order_id
            or order.get("currencyCode") != "RUB"):
        raise RefundError("Заказ не подтверждён как рублёвая оплата вашего магазина Яндекс Пэй.")
    method_data = order.get("paymentMethod")
    method = method_data.get("methodType") if isinstance(method_data, dict) else None
    if not isinstance(method, str) or method not in {"SPLIT", "SPLIT_SBP", "SBP", "CARD"}:
        raise RefundError("Способ оплаты не подтверждён ответом Яндекс Пэй. Разрешены Сплит, СБП и карта (SPLIT/SPLIT_SBP/SBP/CARD). Проведение остановлено.")


def original_paid_operation(order, operations):
    """Select one paid stage, never add successive stages of the same payment.

    Split can expose PREPAYMENT -> SUBMIT without AUTHORIZE. Card payments can
    expose AUTHORIZE -> CAPTURE, including a capture below the authorized sum.
    A prepayment alone does not prove a completed payment.
    """
    stages = {}
    for kind in ('AUTHORIZE', 'CAPTURE', 'PREPAYMENT', 'SUBMIT'):
        rows = [op for op in operations if op.get('operationType')==kind and op.get('status')=='SUCCESS']
        if len(rows)>1:
            raise RefundError('В истории Яндекса несколько успешных операций одного этапа оплаты. Нужна сверка без повторного возврата.')
        if rows:
            op=rows[0]
            if op.get('orderId')!=order.get('orderId') or cents(op.get('amount'))<=0:
                raise RefundError('Исходная операция оплаты Яндекса не подтверждена.')
            stages[kind]=op
    if 'SUBMIT' in stages:
        if 'PREPAYMENT' not in stages or cents(stages['PREPAYMENT']['amount'])!=cents(stages['SUBMIT']['amount']):
            raise RefundError('Предоплата и подтверждение заказа Сплита не сошлись по сумме. Нужна сверка истории Яндекса.')
        if 'CAPTURE' in stages and cents(stages['CAPTURE']['amount'])!=cents(stages['SUBMIT']['amount']):
            raise RefundError('Этапы списания Яндекса содержат разные суммы. Нужна сверка.')
    paid=next((stages[k] for k in ('CAPTURE','SUBMIT','AUTHORIZE') if k in stages),None)
    if paid is None or ('PREPAYMENT' in stages and 'SUBMIT' not in stages and 'CAPTURE' not in stages):
        raise RefundError('В истории Яндекса нет однозначного подтверждённого завершения оплаты. Одной предоплаты недостаточно.')
    if 'AUTHORIZE' in stages and cents(stages['AUTHORIZE']['amount'])<cents(paid['amount']):
        raise RefundError('Списание Яндекса превышает исходную авторизацию. Нужна сверка.')
    return paid


def matched_cart_items(order, items, scope="part", excluded_gifts=()):
    remaining = cart_items(order)
    selected = []
    for item in items:
        matches = [i for i in remaining if i["quantity"] >= item["quantity"]
                   and same_item(item["name"], i["title"])]
        if len(matches) != 1:
            raise RefundError("Товар, размер или количество документа МойСклад не совпали однозначно с оплатой Яндекс Пэй: " + item["name"][:140])
        match = matches[0]
        match["quantity"] -= item["quantity"]
        selected.append({**match,"quantity":item["quantity"],"name":match["title"],"delivery":delivery(match["title"])})
    def ignored_gift(item):
        return item["unit_cents"]==0 and any(same_item(g["name"],item["title"]) for g in excluded_gifts)
    if scope in {"all","goods"} and any(i["quantity"] and not delivery(i["title"]) and not ignored_gift(i) for i in remaining):
        raise RefundError("В документе не все оставшиеся оплаченные товары; укажите «за часть товаров».")
    if scope == "all":
        selected.extend({**i,"name":i["title"],"delivery":True} for i in remaining if i["quantity"] and not ignored_gift(i))
    return selected


def refund_body(order, items, scope="part", excluded_gifts=()):
    financial_items = matched_cart_items(order,items,scope,excluded_gifts)
    selected = {}
    for item in financial_items:
        selected[item["product_id"]] = selected.get(item["product_id"],0)+item["quantity"]
    amount = sum(i["quantity"] * i["unit_cents"] for i in financial_items)
    available = cents(order["orderAmount"])
    if amount < 100 or amount > available or 0 < available - amount < 100:
        raise RefundError("Сумма возврата недоступна с учётом остатка платежа и ограничений Яндекс Пэй.")
    body = {"refundAmount": decimal_amount(amount)}
    if amount != available:
        body["refundCart"] = {"items": [{"productId": pid, "quantityCount": str(count)} for pid, count in sorted(selected.items())]}
    return body


class YandexRefunds:
    def __init__(self, service):
        self.service = service
        self.store = service.store
        self.api = YandexPay(service.base)

    async def preview(self, record, order_id=None):
        r = record["request"]
        if not r["scope"]: raise RefundError("Укажите: за вещь, за доставку, за всё или за часть вещей.")
        provider_order_id = self.api.order_id(r)
        if order_id is None:
            docs = await self.service.candidates(record)
            if len(docs) != 1: raise RefundError("Выберите подготовленный документ возврата/отмены в МойСклад.")
            order_id = docs[0]["id"]
        doc, positions = await self.service.document(order_id)
        if not order_matches(doc.get("name"), r["order_number"]): raise RefundError("Номер документа не соответствует заявке.")
        from customer_refunds import completed_return_state
        if completed_return_state((doc.get("state") or {}).get("name")):
            from completed_refunds import preview_completed
            return await preview_completed(self.service,record,doc,positions)
        customer_hash = await self.service.customer(record, doc)
        document_snapshot,items = selected_document_items(doc,positions,r)
        if len(dump(items)) > 2600: raise RefundError("Слишком большой состав возврата; разделите документы.")
        data = await self.api.order(provider_order_id)
        order = data.get("order")
        validate_order(order, provider_order_id)
        if order.get("paymentStatus") not in {"CAPTURED", "PARTIALLY_REFUNDED"}:
            raise RefundError("Заказ Яндекс Пэй не оплачен либо уже полностью возвращён.")
        if data.get("delivery"):
            raise RefundError("Возврат доставки Checkout требует отдельной сверки. Операция остановлена.")
        contact = str(order.get("fiscalContact") or "").strip()
        details = await self.service.customer_details(record,doc)
        fiscal_contact = valid_contact({"email" if "@" in contact else "phone":contact},details.get("receipt_contact"))
        if not fiscal_contact: raise RefundError("Не найден корректный контакт для документа возврата Яндекс Пэй.")
        warnings = list(dict.fromkeys(details["warnings"]+customer_warnings(r,fiscal_contact)))
        operations = data.get("operations")
        if not isinstance(operations, list) or not operations:
            raise RefundError("Не получена история операций Яндекс Пэй для сверки предыдущих возвратов.")
        ids, refunds = set(), []
        for op in operations:
            if not isinstance(op, dict) or op.get("orderId") != provider_order_id or not op.get("operationId") or op["operationId"] in ids or op.get("status") not in {"PENDING", "SUCCESS", "FAIL"}:
                raise RefundError("История операций Яндекс Пэй не подтверждена.")
            ids.add(op["operationId"])
            if op["status"] == "PENDING": raise RefundError("В Яндекс Пэй уже обрабатывается операция по этому заказу.")
            if op.get("operationType") == "REFUND" and op["status"] == "SUCCESS": refunds.append(op)
        refunded = sum(cents(op.get("amount")) for op in refunds)
        available = cents(order.get("orderAmount"))
        original = available + refunded
        paid_operation = original_paid_operation(order, operations)
        if cents(paid_operation['amount']) != original:
            raise RefundError("Исходная оплата и предыдущие возвраты Яндекс Пэй не сошлись с остатком заказа.")
        original_items = cart_items(order)
        paid_items=[{"name":i["title"],"unit_cents":i["unit_cents"],"quantity":i["quantity"]} for i in original_items]
        if r['scope']=='compensation':
            from refund_compensation import compensation_yandex
            financial_items,body=compensation_yandex(order,r,document_snapshot)
            excluded_gifts,gift_review=[],None
        else:
            items,excluded_gifts,gift_review = gift_checked_items(record,doc,document_snapshot,items,paid_items,"yandex:"+provider_order_id,sorted(op["operationId"] for op in refunds))
            financial_items = matched_cart_items(order,items,r["scope"],excluded_gifts)
            body = refund_body(order,items,r["scope"],excluded_gifts)
        selected_goods = sum(i["quantity"] * i["unit_cents"] for i in financial_items if not i["delivery"])
        available_goods = sum(i["quantity"] * i["unit_cents"] for i in original_items if not delivery(i["title"]))
        if r["scope"] == "goods" and selected_goods != available_goods:
            raise RefundError("В документе не все оставшиеся товары; укажите «за часть товаров».")
        if r["scope"] == "all" and cents(body["refundAmount"]) != cents(order["orderAmount"]):
            raise RefundError("Документ всего заказа должен включать все товары и доставку.")
        amount = cents(body["refundAmount"])
        body["motive"] = "Возврат заказа " + r["order_number"]
        snapshot = {"provider": "yandex_split", "provider_order_id": provider_order_id,
                    "payment_method":order["paymentMethod"]["methodType"],
                    "order": {"id": doc["id"], "name": doc["name"], "updated": doc.get("updated"), "state": doc.get("state", {}).get("name"), "items": document_snapshot},
                    "financial_items":[{k:i[k] for k in ("name","quantity","unit_cents","delivery")} for i in financial_items],
                    "payment_id": "yandex:" + provider_order_id, "amount_cents": amount, "available_cents": available,
                    "payment_amount_cents": original, "body": body, "customer_hash": customer_hash,
                    "customer":{**details["customer"],**fiscal_contact},"customer_warnings":warnings,
                    "settings_hash": hashed(__import__("refund_settings").SETTINGS), "source_hash": record["source_hash"],
                    "receipt_contact": fiscal_contact,
                    "provider_cart_hash": hashed(order["cart"]), "previous_refunds": sorted(op["operationId"] for op in refunds)}
        if r.get("item_selector") or items != document_snapshot: snapshot["selected_items"] = items
        if r['scope']=='compensation':snapshot['compensation']={'amount_cents':amount,'item_stays_with_customer':True,'item_selector':r.get('item_selector')}
        if gift_review: snapshot.update(gift_review=gift_review,excluded_gifts=excluded_gifts)
        snapshot["hash"] = approval_hash(snapshot)
        return snapshot

    def identity(self, record):
        snapshot = record.get("snapshot") or {}
        if (record["request"].get("method") != "yandex_split" or snapshot.get("provider") != "yandex_split"
                or record.get("payment_id") != "yandex:" + snapshot.get("provider_order_id", "")
                or self.api.order_id(record["request"]) != snapshot.get("provider_order_id")):
            raise RefundError("Платёжный сервис или заказ изменились. Отправка возврата остановлена.")
        return snapshot["provider_order_id"]

    async def execute(self, number, revision, actor):
        if not OWNER or actor != OWNER: raise RefundError("Проведение доступно только владельцу.")
        record = self.store.get(number)
        if record["revision"] != revision or record["state"] != "ready" or not record.get("snapshot"):
            raise RefundError("Сначала подготовьте актуальную карточку возврата.")
        order_id = self.identity(record)
        async with self.service.payment_locks.setdefault(record["payment_id"], asyncio.Lock()):
            try: fresh = await self.preview(record, record["ms_order_id"])
            except GiftClarification as error:
                self.store.preparation_error(number,revision,error)
                raise
            except RefundError as error:
                self.store.preparation_error(number,revision,error)
                raise
            if fresh["hash"] != record["snapshot"]["hash"]:
                if fresh.get("existing_refund"):
                    self.store.prepare(number,revision,fresh)
                    raise RefundError("Яндекс уже обрабатывает или завершил возврат. Подтвердите отправку документа сотрудникам.")
                self.store.update(number, state="needs_info", last_error="Документ, корзина или предыдущие возвраты изменились. Подготовьте карточку заново.")
                raise RefundError("Данные изменились; возврат Яндекс Пэй не отправлен.")
            self.api.enabled()
            claimed = self.store.claim(number, revision, actor)
            try:
                data = await self.api.refund(order_id, claimed["snapshot"]["body"], claimed["idempotence_key"])
            except UncertainRefund as error:
                self.store.update(number, state="uncertain", last_error=str(error)); raise
            except RefundError as error:
                self.store.update(number, state="failed", last_error=str(error)); raise
            except Exception:
                self.store.update(number, state="uncertain", last_error="Результат Яндекс Пэй требует GET-сверки; новая операция не создаётся."); raise
            try:
                return await self.accept(claimed, data.get("operation"))
            except Exception:
                current = self.store.get(number)
                if current["state"] == "submitting":
                    self.store.update(number, state="uncertain", last_error="Ответ Яндекс Пэй требует сверки по сохранённому ID операции.")
                raise

    async def accept(self, record, op):
        order_id = self.identity(record)
        try:
            valid = (isinstance(op, dict) and str(uuid.UUID(op["operationId"])) == op["operationId"]
                     and op.get("externalOperationId") == record["idempotence_key"] and op.get("orderId") == order_id
                     and op.get("operationType") == "REFUND" and cents(op.get("amount")) == record["amount_cents"]
                     and op.get("status") in {"PENDING", "SUCCESS", "FAIL"}
                     and (not record.get("refund_id") or record["refund_id"] == "yandex:" + op["operationId"]))
        except (ValueError, KeyError, TypeError, RefundError): valid = False
        if not valid:
            self.store.update(record["id"], state="uncertain", last_error="Операция Яндекс Пэй не совпала с подтверждённым возвратом.")
            raise RefundError("Результат Яндекс Пэй не подтверждён. Выполняем только GET-сверку.")
        current = self.store.get(record["id"])
        if current["state"] == "succeeded" and op["status"] == "PENDING": return current
        fields = {"refund_id": "yandex:" + op["operationId"], "refund_status": op["status"], "last_error": None}
        state = {"PENDING": "pending", "SUCCESS": "succeeded", "FAIL": "failed"}[op["status"]]
        if state == "failed": fields["last_error"] = "Яндекс Пэй не выполнил возврат. Проверьте операцию в кабинете."
        if state == "pending":
            created = None
            if self.service.crm_path:
                from database import Database
                source = Database(self.service.crm_path).get_order_by_number(record["request"]["order_number"])
                if source and source.get("created_at"):
                    created = datetime.fromtimestamp(int(source["created_at"]),timezone.utc).isoformat()
            fields.update(receipt_status="external_pending",receipt_json={
                "provider":"yandex_split","status":"PENDING","amount_cents":record["amount_cents"],
                "order_id":order_id,"operation_id":op["operationId"],"external_operation_id":record["idempotence_key"],
                "requested_at":record["attempted_at"],"order_created_at":created,
                "contact":next(iter(record["snapshot"]["receipt_contact"].values()))})
        if state == "failed" and (record.get("receipt_json") or {}).get("provider") == "yandex_split":
            fields.update(receipt_status="external_failed",receipt_json={**record["receipt_json"],"status":"FAIL",
                "failed_at":op.get("updated") or op.get("created")})
        if state == "succeeded":
            data = await self.api.order(order_id)
            validate_order(data.get("order"), order_id)
            matches = [i for i in data.get("operations", []) if isinstance(i, dict) and i.get("operationId") == op["operationId"]]
            if len(matches) != 1 or matches[0].get("status") != "SUCCESS" or matches[0].get("externalOperationId") != record["idempotence_key"] or cents(matches[0].get("amount")) != record["amount_cents"] or matches[0].get("operationType") != "REFUND" or matches[0].get("orderId") != order_id:
                fields["last_error"] = "Ожидаем подтверждение возврата в истории заказа Яндекс Пэй."
                return self.store.update(record["id"], **fields, state="uncertain")
            try:
                stamp = datetime.fromisoformat(op.get("updated") or op["created"])
                if stamp.tzinfo is None: raise ValueError
            except (ValueError, KeyError, TypeError):
                raise RefundError("Яндекс Пэй ещё не передал дату успешной операции; ожидаем GET-сверку.") from None
            try: paid_at=original_paid_operation(data['order'],data.get('operations',[])).get('created')
            except RefundError: paid_at=None
            fields.update(succeeded_at=record.get("succeeded_at") or stamp.isoformat(), receipt_status="external",
                          receipt_json={"provider": "yandex_split", "amount_cents": record["amount_cents"],
                                        "order_id": order_id, "operation_id": op["operationId"],
                                        "external_operation_id": record["idempotence_key"], "status": "SUCCESS",
                                        "completed_at": stamp.isoformat(),
                                        "order_created_at": data["order"].get("created"),
                                        "paid_at": paid_at,
                                        "contact": next(iter(record["snapshot"]["receipt_contact"].values()))})
        saved = self.store.update(record["id"], **fields, state=state)
        if state == "pending": return await self.confirmation(saved)
        if state == "succeeded" and not saved["ms_synced"]:
            try: await self.service.sync_ms(saved)
            except RefundError as error: self.store.update(saved["id"], last_error=str(error))
        return self.store.get(record["id"])

    async def check(self, record, force=False):
        if (record.get("snapshot") or {}).get("existing_refund"):
            from completed_refunds import check_completed
            return await check_completed(self.service,record)
        if force and record['state'] in {'succeeded','failed'} and record.get('attempted_at') and record.get('idempotence_key'):
            self.identity(record)
            data=await self.api.operation(record['idempotence_key'])
            return await self.accept(record,data.get('operation'))
        if record["state"] == "succeeded":
            if not record["ms_synced"]:
                try: await self.service.sync_ms(record)
                except RefundError as error: self.store.update(record["id"], last_error=str(error))
            return self.store.get(record["id"])
        if record["state"] not in ACTIVE: return record
        order_id = self.identity(record)
        try: data = await self.api.operation(record["idempotence_key"])
        except YandexAPIError as error:
            if error.status_code != 404: raise
            data = await self.api.order(order_id)
            validate_order(data.get("order"), order_id)
            matches = [op for op in data.get("operations", []) if op.get("externalOperationId") == record["idempotence_key"]]
            if len(matches) != 1:
                return self.store.update(record["id"], state="uncertain", last_error="Операция пока не найдена в Яндекс Пэй. Повторный денежный запрос не создаётся.")
            data = {"operation": matches[0]}
        return await self.accept(record, data.get("operation"))

    async def confirmation(self, record):
        # The public Merchant API exposes operations, not fiscal receipt requisites.
        if (record.get("snapshot") or {}).get("existing_refund"):return record
        if record["state"] == "pending":
            self.pending_identity(record)
            if not record.get("split_booked_at") or not record["ms_synced"]:
                try: await self.service.book_pending_split(record)
                except RefundError as error: self.store.update(record["id"],receipt_error=str(error))
            return self.store.get(record["id"])
        if record["state"] != "succeeded" or record.get("receipt_json"): return record
        data = await self.api.operation(record["idempotence_key"])
        return await self.accept(record, data.get("operation"))

    def pending_identity(self, record):
        if (record.get("snapshot") or {}).get("existing_refund"):
            from completed_refunds import pending_completed_identity
            return pending_completed_identity(self.service,record)
        order_id = self.identity(record)
        proof = record.get("receipt_json") or {}
        try:
            valid = (record["state"] == "pending" and record["refund_status"] == "PENDING"
                and OWNER and record["approved_by"] == OWNER and record["attempted_at"]
                and datetime.fromisoformat(record["attempted_at"]).tzinfo is not None
                and str(uuid.UUID(record["idempotence_key"])) == record["idempotence_key"]
                and proof.get("provider") == "yandex_split" and proof.get("status") == "PENDING"
                and proof.get("order_id") == order_id
                and str(uuid.UUID(proof["operation_id"])) == proof["operation_id"]
                and record["refund_id"] == "yandex:"+proof["operation_id"]
                and proof.get("external_operation_id") == record["idempotence_key"]
                and proof.get("amount_cents") == record["amount_cents"] == record["snapshot"]["amount_cents"]
                and cents(record["snapshot"]["body"]["refundAmount"]) == record["amount_cents"])
        except (KeyError,TypeError,ValueError,RefundError): valid = False
        if not valid: raise RefundError("Яндекс ещё не подтвердил приём именно этой операции. Раннее оформление остановлено.")
        return order_id
