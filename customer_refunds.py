"""Owner-approved customer refunds. Source: a prepared return/cancellation order."""
import asyncio
import hashlib
import json
import os
import re
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import httpx
MS_API = "https://api.moysklad.ru/api/remap/1.2"
from refund_storage import connect_database

from refund_settings import OWNER, SUPPORT_CHAT, SUPPORT_TOPIC, SHOP_ID, SETTINGS
YOO_API = "https://api.yookassa.ru/v3"
MOSCOW = ZoneInfo("Europe/Moscow")
SAMPLE_PAYMENT = {}
ACTIVE = {"submitting", "uncertain", "pending"}
FUNDS_NOTICE = "Возврат средств не проведен, так как на Юкассе недостаточно средств для проведения возврата. Он будет проведен позднее"
EDITABLE = {"new", "needs_info", "ready", "rejected"}
DEFAULT_RETURN_STATES = {"возврат", "возврат срочный", "возврат принят", "возврат принят ср", "возврат заявление", "отменен", "отмена", "возврат средств", "отмена заказа"}


class RefundError(RuntimeError):
    pass


class GiftClarification(RefundError):
    def __init__(self, review):
        self.review = review
        super().__init__("Шарф не сопоставлен с оплаченной позицией. Уточните, был ли он подарком, кнопками ниже.")


class CompletedMismatch(RefundError):
    def __init__(self, review):
        self.review=review
        super().__init__("В МойСклад уже стоит «Возврат-проведен», но в ЮKassa возврата нет: возвращено 0 ₽. Чек возврата не найден. Если клиент обратился в банк, нажмите «Чарджбек», чтобы сообщить сотрудникам. Повторная отправка денег не выполняется.")


def completed_chargeback_available(record):
    review=record.get("completed_review") or {}
    return bool(record["state"] in EDITABLE and not record.get("attempted_at") and review.get("source_hash")==record.get("source_hash")
        and review.get("status")=="ms_completed_yoo_not_refunded" and not record.get("chargeback_at"))


class UncertainRefund(RefundError):
    pass


def waiting_funds(record):
    return bool(record["state"]=="failed" and record.get("failure_reason")=="insufficient_funds"
        and record.get("refund_status")=="canceled" and record.get("refund_id") and record.get("attempted_at")
        and record["request"].get("method")=="yookassa" and not record.get("chargeback_at"))


class YooKassaHTTPError(RefundError):
    def __init__(self,status,payload=None,secret=""):
        payload=payload if isinstance(payload,dict) else {}
        self.status=status
        fields=[]
        for key,label,pattern in (("code","Код",r"[a-z0-9_]{1,80}"),("id","ID ошибки",r"[a-f0-9-]{36}"),("parameter","Параметр",r"[a-zA-Z0-9_.\[\]-]{1,100}")):
            value=str(payload.get(key) or "")
            if re.fullmatch(pattern,value):fields.append(label+": "+value)
        description=" ".join(str(payload.get("description") or "").split())
        if secret:description=description.replace(secret,"[ключ скрыт]")
        if description:fields.append(description[:350])
        super().__init__(f"ЮKassa: HTTP {status}. "+(" · ".join(fields) if fields else "Не удалось подтвердить результат запроса."))


class PaymentLookupError(RefundError):
    def __init__(self, message, status="needs_info"):
        super().__init__(message)
        self.status = status


def now():
    return datetime.now(timezone.utc).isoformat()


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def hashed(value):
    return hashlib.sha256(dump(value).encode()).hexdigest()


def approval_hash(snapshot):
    return hashed({k:v for k,v in snapshot.items() if k not in {"hash","customer","customer_warnings"}})


def norm(value):
    return " ".join(re.sub(r"[-–—_]", " ", str(value or "").lower().replace("ё", "е")).split())


def payment_method_from_label(value):
    return {"яндекс сплит":"yandex_split","сплит":"yandex_split","split":"yandex_split","yandex split":"yandex_split",
        "яндекс пэй":"yandex_split","яндекс пей":"yandex_split","yandex pay":"yandex_split","яндекс сбп":"yandex_split",
        "юкасса":"yookassa","юkassa":"yookassa","yookassa":"yookassa","полная":"yookassa","полная оплата":"yookassa"}.get(norm(value))


def cents(value):
    try:
        amount = Decimal(re.sub(r"\s+","",str(value)).replace(",", "."))
        if not amount.is_finite() or amount < 0 or amount * 100 != (amount * 100).to_integral_value():
            raise ValueError
        return int(amount * 100)
    except (InvalidOperation, ValueError):
        raise RefundError("Некорректная сумма; требуется точность до копейки.")


def rubles(value):
    return f"{value // 100:,}".replace(",", " ") + f",{value % 100:02d} ₽"


def money(value):
    return {"value": f"{value // 100}.{value % 100:02d}", "currency": "RUB"}


def quantity(value):
    try:
        n = Decimal(str(value))
        if not n.is_finite() or n <= 0 or n != n.to_integral_value() or n > 100000:
            raise ValueError
        return int(n)
    except (InvalidOperation, ValueError):
        raise RefundError("Для изделий требуется положительное целое количество.")


def parse_request(text):
    from refund_receipt_requests import parse_receipt_request
    receipt_number=parse_receipt_request(text)
    if receipt_number:
        request=parse_request(receipt_number)
        request["kind"]="receipt_request"
        return request
    lines = [line.strip().strip("*") for line in text.strip().splitlines() if line.strip()]
    match = re.fullmatch(r"(?:заказ\s*:?\s*)?[№#]?\s*(\d{5,20})", lines[0] if lines else "", re.I)
    if not match:
        return None
    request = {"order_number": match[1], "scope": "", "name": "", "email": "", "phone": "", "payment_id": "", "yandex_order_id":"", "paid_at":"", "paid_amount_cents":None}
    for line in lines[1:]:
        for field, aliases in (("name", "name|фио|имя"), ("email", "email|e-mail|почта"), ("phone", "phone|телефон"), ("payment_id", "payment_id|id платежа"), ("refund_id", "refund_id|id возврата"), ("yandex_order_id", "yandex_order_id|id заказа яндекс(?: пэй| пей)?")):
            value = re.match(r"(?:" + aliases + r")\s*:\s*(.+)$", line, re.I)
            if value:
                request[field] = value[1].strip()
        date = re.fullmatch(r"(?:(?:дата(?: и время)?(?: оплаты)?|оплата|оплачено)\s*:?\s*)?(\d{1,2}\.\d{1,2}(?:\.\d{4})?)\s+(?:в\s*)?(\d{1,2}:\d{2}(?::\d{2})?)",line,re.I)
        if date: request["paid_at"] = date[1] + " " + date[2]
        amount = re.fullmatch(r"(?:сумма(?:\s+(?:оплаты|платежа|заказа))?|оплачено|оплата)\s*:\s*([\d \u00a0\u202f]+(?:[.,]\d{1,2})?)\s*(?:руб\.?|р\.?|₽)?",line,re.I)
        if amount: request["paid_amount_cents"] = cents(amount[1])
        if not request["name"] and re.fullmatch(r"[А-Яа-яЁё-]+(?:\s+[А-Яа-яЁё-]+){2,3}",line) and "лояльност" not in line.lower() and " за " not in line.lower():
            request["name"] = line
    scope_lines=[]
    for line in lines[1:]:
        label=re.fullmatch(r"(?:вариант возврата|тип возврата|возврат|вернуть)\s*:\s*(.+)",line,re.I)
        if label: scope_lines.append("за "+label[1])
        elif re.fullmatch(r"товары|товар|вещи|доставка|весь заказ|часть товаров|часть вещей",line,re.I): scope_lines.append("за "+line)
        else: scope_lines.append(line)
    lower = norm("\n".join(scope_lines))
    method_text = norm("\n".join(line for line in lines[1:] if not re.match(r"(?:name|фио|имя|email|e-mail|почта|phone|телефон|payment_id|id платежа|yandex_order_id|id заказа яндекс(?: пэй| пей)?)\s*:", line, re.I)))
    request["method"] = "yandex_split" if re.search(r"\bсплит\b|\bsplit\b|яндекс\s+(?:пэй|пей|сбп)|yandex\s*(?:pay|sbp)",method_text) else "yookassa" if re.search(r"\bполная\b|юкасс|юkass|yookassa|яндекс\.касса", method_text) else "unsupported"
    if re.search(r"за\s+(?:часть\s+(?:вещ|товар)|некоторые)", lower): request["scope"] = "part"
    elif re.search(r"за\s+(?:достав|доставка)", lower): request["scope"] = "delivery"
    elif re.search(r"за\s+(?:все|всё|весь заказ)", lower): request["scope"] = "all"
    elif re.search(r"за\s+(?:вещ|товар|издел)", lower): request["scope"] = "goods"
    from refund_compensation import parse_compensation
    parse_compensation(lines[1:], request)
    selectors = []
    def item_line(value):
        return bool(value and not re.match(r"(?:name|фио|имя|email|e-mail|почта|phone|телефон|payment_id|refund_id|id возврата|yandex_order_id|дата|сумма|оплата|оплачено|размер|size|количество|вариант возврата|тип возврата|товар|вещь|изделие|название(?:\s+(?:вещи|товара|изделия))?)\s*:",value,re.I)
                    and not re.search(r"@|^\+?\d|юкасс|юkass|yookassa|\bполная\b|\bсплит\b|яндекс|yandex",value,re.I))
    for index,line in enumerate(lines[1:],1):
        label = re.fullmatch(r"(?:товар|вещь|изделие|название(?:\s+(?:вещи|товара|изделия))?)\s*:\s*(.+)",line,re.I)
        if label: selectors.append(label[1].strip());continue
        inline = re.search(r"\bза\s+(?:вещь|товар|изделие)\b\s*[:—–,\-]?\s*(.*)$",line,re.I)
        if inline:
            value = inline[1].strip()
            if item_line(value): selectors.append(value)
            elif not value and index+1 < len(lines) and item_line(lines[index+1]): selectors.append(lines[index+1])
    selectors = list({norm(v):v for v in selectors}.values())
    if selectors:
        if len(selectors) != 1 or request["scope"] not in {"goods","part","compensation"}:
            request["item_selector_error"] = "Укажите одну вещь для частичного возврата или выберите возврат всего заказа без названия вещи."
        else:
            sizes = list({norm(m[1]):m[1] for line in lines[1:] if (m:=re.fullmatch(r"(?:размер|size)\s*:\s*(.+)",line,re.I))}.values())
            if len(sizes)>1: request["item_selector_error"] = "Укажите один размер возвращаемой вещи."
            request["item_selector"] = selectors[0]+(" Размер="+sizes[0] if len(sizes)==1 else "")
            if request['scope']!='compensation':request["scope"] = "part"
    return request


def order_matches(name, number):
    # Copies are eligible too, but only in a return/cancellation state.
    return bool(re.search(r"(?<!\d)" + re.escape(number) + r"(?!\d)", str(name)))


def return_state(name):
    return norm(name) in DEFAULT_RETURN_STATES


def completed_return_state(name):
    return norm(name) == "возврат проведен"


def delivery(name):
    return bool(re.search(r"достав|\bсдэк\b|\bcdek\b|shipping|delivery", norm(name)))


def phone(value):
    digits = re.sub(r"\D", "", str(value or ""))
    return "7" + digits[1:] if len(digits) == 11 and digits.startswith("8") else digits


def valid_contact(*sources):
    for source in sources:
        source = source or {}
        email = str(source.get("email") or "").strip()
        number = phone(source.get("phone"))
        result = {}
        if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+",email): result["email"] = email
        if re.fullmatch(r"\d{10,15}",number): result["phone"] = number
        if result: return result
    return {}


def customer_warnings(request, reference):
    result = []
    for key,label,normalize in (("name","ФИО",norm),("email","Email",lambda v:str(v or "").strip().casefold()),("phone","Телефон",phone)):
        if reference.get(key) and normalize(request.get(key)) != normalize(reference[key]):
            result.append(label+" заявки отличается от данных заказа; не блокирует возврат.")
    return result


def names_match(document_name, receipt_name):
    from yandex_refunds import same_item
    return same_item(document_name, receipt_name)


def document_items(order, positions, scope):
    if not return_state((order.get("state") or {}).get("name")):
        raise RefundError("Нужен документ со статусом возврата или отмены, а не обычный оплаченный заказ.")
    items = []
    for position in positions:
        assortment = position.get("assortment") or {}
        name = str(assortment.get("name") or "").strip()
        count = quantity(position.get("quantity", 0))
        price = (Decimal(str(position.get("price", 0))) * (1 - Decimal(str(position.get("discount", 0))) / 100)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        if name and price == 0 and norm(name) in {"самовывоз", "pickup", "self pickup"}:
            continue
        if not name or price < 0:
            raise RefundError("В документе есть позиция без названия или с отрицательной ценой.")
        shipping = delivery(name)
        if scope == "delivery" and not shipping or scope in {"goods", "part"} and shipping:
            raise RefundError("Состав документа МойСклад не соответствует заявке. Подготовьте отдельный документ только с возвращаемыми позициями.")
        items.append({"id": position["id"], "name": name, "quantity": count, "unit_cents": int(price), "delivery": shipping})
    if not items:
        raise RefundError("В документе МойСклад нет выбранных позиций возврата.")
    return items


def selected_document_items(order, positions, request):
    if request.get("item_selector_error"): raise RefundError(request["item_selector_error"])
    if request.get('scope') == 'compensation':
        from refund_compensation import compensation_document
        return compensation_document(order, positions, request)
    selector = str(request.get("item_selector") or "").strip()
    if not selector:
        items = document_items(order,positions,'all')
        scope = request['scope']
        selected = [i for i in items if (i['delivery'] if scope=='delivery' else not i['delivery'] if scope in {'goods','part'} else True)]
        if not selected: raise RefundError('В документе МойСклад нет выбранных позиций возврата.')
        return items,selected
    if request["scope"] != "part" or len(selector)>240:
        raise RefundError("Уточните название одной вещи для частичного возврата.")
    items = document_items(order,positions,"all")
    def tokens(value):
        value = re.sub(r"\s+-\d+(?:[.,]\d+)?%$","",value)
        aliases = {"пуховик":"puffer","пуховики":"puffer","пуффер":"puffer","puffers":"puffer",
                   "ветровка":"windbreaker","ветровки":"windbreaker","windbreakers":"windbreaker",
                   "зип":"zip","худи":"hoodie","демозип":"demozip","демо":"demo",
                   "черный":"black","красный":"red","коричневый":"brown","серый":"grey","gray":"grey",
                   "белый":"white","синий":"blue","розовый":"pink"}
        return {aliases.get(v,v) for v in re.findall(r"\w+",norm(value))}-{"размер","size","цвет","color"}
    wanted = tokens(selector)
    def explicit_size(value):
        match = re.search(r"(?:размер|size)\s*[:=]?\s*([^,;\s()]+)|\(\s*([^,()]+)[,)]",value,re.I)
        return norm(match[1] or match[2]) if match else ""
    size = explicit_size(selector)
    if not size:
        sizes = wanted & {"xxs","xs","s","m","l","xl","xxl","xxxl","2xl","3xl","4xl","onesize"}
        if len(sizes)>1: raise RefundError("Укажите один размер возвращаемой вещи.")
        size = next(iter(sizes),"")
    matches = [i for i in items if not i["delivery"] and wanted and wanted<=tokens(i["name"])
               and (not size or size==explicit_size(i["name"]))]
    if len(matches) != 1:
        raise RefundError("Название вещи не найдено однозначно в документе МойСклад. Укажите полное название, цвет и размер возвращаемой вещи.")
    # A named singular item means one unit, even when the order has two.
    return items,[{**matches[0],"quantity":1}]


def receipt_item_key(item):
    return hashed({k: v for k, v in item.items() if k != "quantity"})


def gift_checked_items(record, document, document_items_snapshot, items, paid_items, payment_id, previous):
    old = record.get("gift_review") or {}
    prior_ids = {i["id"] for i in old.get("items",[])}
    def is_scarf(item): return bool(re.search(r"\bscarf\b|шарф",norm(item["name"])))
    def paid_matches(item):
        return [p for p in paid_items if p["unit_cents"]>0 and names_match(item["name"],p["name"])]
    candidates = [i for i in items if is_scarf(i) and (i["id"] in prior_ids or not paid_matches(i))]
    if not candidates: return items,[],None
    basis = hashed({"document_id":document["id"],"items":sorted(document_items_snapshot,key=dump),"payment_id":payment_id,
                    "paid_items":sorted(paid_items,key=dump),"previous":previous,"source_hash":record["source_hash"]})
    decisions = {i["id"]:i.get("decision") for i in old.get("items",[])} if old.get("basis")==basis else {}
    review = {"document_id":document["id"],"basis":basis,
              "items":[{"id":i["id"],"name":i["name"],"quantity":i["quantity"],"decision":decisions.get(i["id"])} for i in candidates]}
    if any(i["decision"] is None for i in review["items"]): raise GiftClarification(review)
    excluded = []
    for candidate in review["items"]:
        if candidate["decision"] == "gift":
            if paid_matches(candidate):
                raise RefundError("В чеке у шарфа есть оплаченная стоимость. Уточните решение: подарок не должен исключать оплаченную позицию без сверки.")
            excluded.append({k:candidate[k] for k in ("id","name","quantity")})
        elif candidate["decision"] != "paid": raise RefundError("Не подтверждено решение о шарфе.")
        elif not paid_matches(candidate):
            raise RefundError("Шарф отмечен как купленный, но его оплаченная позиция не найдена в платёжном сервисе. Нужна сверка чека; цену из МойСклад не используем.")
    excluded_ids = {i["id"] for i in excluded}
    selected = [i for i in items if i["id"] not in excluded_ids]
    if not selected and record["request"]["scope"]!="all":
        raise RefundError("После исключения подарка нет оплаченных вещей для возврата. Уточните вариант возврата.")
    return selected,excluded,review


RECEIPT_ITEM_FIELDS = {"description", "quantity", "amount", "vat_code", "payment_subject", "payment_mode", "measure", "mark_code_info", "mark_mode", "mark_quantity", "product_code", "planned_status", "payment_subject_industry_details", "country_of_origin_code", "customs_declaration_number", "excise", "supplier", "agent_type"}


def make_receipt(payment, receipts, refunded_receipts, doc_items, customer, scope="part", excluded_gifts=()):
    purchases = [r for r in receipts if r.get("type") == "payment" and r.get("status") == "succeeded"]
    if len(purchases) != 1:
        raise RefundError("Нужен один однозначный зарегистрированный чек исходной оплаты. Проверьте чеки платежа.")
    original = purchases[0]
    if not original.get("tax_system_code"):
        raise RefundError("В исходном чеке отсутствует система налогообложения. Нужна настройка чеков.")
    remaining = {}
    for raw in original.get("items", []):
        item = {k: v for k, v in raw.items() if k in RECEIPT_ITEM_FIELDS}
        if not all(k in item for k in ("description", "quantity", "amount", "vat_code")) or item["amount"].get("currency") != "RUB":
            raise RefundError("Не хватает данных позиций исходного чека.")
        key = receipt_item_key(item)
        if key not in remaining: remaining[key] = {"item": item, "quantity": 0}
        remaining[key]["quantity"] += quantity(item["quantity"])
    original_total = sum(cents(r["item"]["amount"]["value"]) * r["quantity"] for r in remaining.values())
    if original_total != cents(payment["amount"]["value"]):
        raise RefundError("Итог исходного чека отличается от суммы платежа. Требуется уточнение.")
    for receipt in sorted(refunded_receipts, key=lambda r: str(r.get('registered_at') or r.get('created_at') or '')):
        if receipt.get("type") != "refund" or receipt.get("status") != "succeeded":
            raise RefundError("Чек предыдущего возврата ещё не зарегистрирован; повторный возврат пока остановлен.")
        for raw in receipt.get("items", []):
            item = {k: v for k, v in raw.items() if k in RECEIPT_ITEM_FIELDS}
            if (item.get('amount') or {}).get('currency')!='RUB':
                raise RefundError('Валюта предыдущего чека возврата не совпала с оплатой.')
            key = receipt_item_key(item)
            if key not in remaining:
                # A price compensation does not return the physical unit.
                # Preserve its remaining paid price for any later return.
                signature = lambda value: {k:v for k,v in value.items() if k not in {'quantity','amount'}}
                price, count = cents(item['amount']['value']), quantity(item['quantity'])
                matches = [r for r in remaining.values() if r['quantity']>=count
                           and signature(r['item'])==signature(item) and 0<price<cents(r['item']['amount']['value'])]
                if len(matches)!=1:
                    raise RefundError("Не удалось сверить позиции предыдущего возврата с исходным чеком.")
                row=matches[0];row['quantity']-=count
                residual={**row['item'], 'amount':money(cents(row['item']['amount']['value'])-price)}
                residual_key=receipt_item_key(residual)
                remaining.setdefault(residual_key, {'item':residual,'quantity':0})['quantity']+=count
                continue
            remaining[key]["quantity"] -= quantity(item["quantity"])
            if remaining[key]["quantity"] < 0: raise RefundError("В чеках возвращено больше товара, чем оплачено.")
    selected = []
    for item in doc_items:
        matches = [r for r in remaining.values() if r["quantity"] >= item["quantity"]
                   and names_match(item["name"],r["item"]["description"])]
        if len(matches) != 1:
            raise RefundError("Не удалось однозначно сопоставить позицию с оплаченным чеком: " + item["name"][:180])
        row = matches[0]; row["quantity"] -= item["quantity"]
        selected.append({**row["item"], "quantity": str(item["quantity"])})
    def ignored_gift(row):
        return cents(row["item"]["amount"]["value"])==0 and any(names_match(g["name"],row["item"]["description"]) for g in excluded_gifts)
    if scope == "all":
        if any(r["quantity"] and not delivery(r["item"]["description"]) and not ignored_gift(r) for r in remaining.values()):
            raise RefundError("В документе перечислены не все оставшиеся оплаченные товары для полного возврата.")
        # Delivery belongs to a full refund even when it was omitted in MS.
        selected.extend({**r["item"],"quantity":str(r["quantity"])} for r in remaining.values() if r["quantity"] and not ignored_gift(r))
    if scope == "goods" and any(r["quantity"] and not delivery(r["item"]["description"]) and not ignored_gift(r) for r in remaining.values()):
        raise RefundError("В документе перечислены не все оставшиеся товары. Укажите «за часть товаров».")
    contact = valid_contact(original.get("customer"),payment.get("metadata"),customer)
    if not contact: raise RefundError("Не найден корректный контакт для регистрации чека в исходной оплате или документе возврата.")
    return {"customer": contact, "items": selected, "tax_system_code": original["tax_system_code"],
            **{k: original[k] for k in ("timezone", "internet", "receipt_industry_details", "receipt_operational_details") if k in original}}


class YooKassa:
    def __init__(self, base): self.base = Path(base)

    def credentials(self):
        file = self.base / ".secrets/yookassa.json"
        if not file.exists(): raise RefundError("Ключ ЮKassa ещё не установлен. Заявка сохранена; проведение недоступно.")
        data = json.loads(file.read_text())
        if not SHOP_ID or data.get("shop_id") != SHOP_ID or data.get("test") is not False:
            raise RefundError("Не подтверждён боевой магазин ЮKassa.")
        return data

    def enabled(self):
        from refund_setup import require_gate
        require_gate(self.base, "yookassa")

    async def call(self, method, path, *, params=None, body=None, key=None):
        if method == "POST": self.enabled()
        data = self.credentials()
        async with httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False) as client:
            try:
                response = await client.request(method, YOO_API + path, params=params, json=body,
                    auth=httpx.BasicAuth(data["shop_id"], data["secret_key"]),
                    headers={"Idempotence-Key": key} if key else {})
            except httpx.TransportError:
                if method == "POST": raise UncertainRefund("ЮKassa не подтвердила результат. Повторное списание не отправляем; проверяем статус.")
                raise RefundError("ЮKassa временно недоступна; повторите проверку.")
        if response.status_code != 200:
            if method == "POST" and (response.status_code >= 500 or response.status_code in {408, 409, 429}):
                raise UncertainRefund("Результат запроса ЮKassa требует проверки. Повторная отправка остановлена.")
            try:payload=response.json()
            except ValueError:payload=None
            raise YooKassaHTTPError(response.status_code,payload,data.get("secret_key",""))
        try: return response.json()
        except ValueError:
            if method == "POST": raise UncertainRefund("ЮKassa вернула неполный ответ. Проверяем результат без повторной отправки.")
            raise RefundError("Не удалось прочитать ответ ЮKassa.")

    async def get(self, path, params=None): return await self.call("GET", path, params=params)

    async def pages(self, path, params=None, max_pages=30):
        result, cursor, seen = [], None, set()
        for _ in range(max_pages):
            page = await self.get(path, {**(params or {}), "limit": 100, **({"cursor": cursor} if cursor else {})})
            result.extend(page.get("items", [])); cursor = page.get("next_cursor")
            if not cursor: return result
            if cursor in seen: break
            seen.add(cursor)
        raise RefundError("Слишком много операций для автоматической сверки. Уточните ID платежа.")


def registered_yoo_receipt(receipt, record):
    for item in receipt.get("items",[]):
        if (item.get("amount") or {}).get("currency") != "RUB": raise RefundError("Валюта чека отличается от возврата.")
    total = sum(cents(item["amount"]["value"])*quantity(item["quantity"]) for item in receipt.get("items",[]))
    if total != record["amount_cents"]: raise RefundError("Сумма зарегистрированного чека не совпала с суммой возврата.")
    if (record.get("snapshot") or {}).get("existing_refund"):
        from completed_refunds import financial_items, same_financial_items
        if not same_financial_items(record["snapshot"]["financial_items"],financial_items(receipt)):
            raise RefundError("Состав зарегистрированного чека не совпал с подтверждённым существующим возвратом.")
    required = ("id","fiscal_provider_id","fiscal_document_number","fiscal_storage_number","fiscal_attribute")
    values = {k:str(receipt.get(k) or "").strip() for k in required}
    if any(not v or len(v)>128 or any(ord(c)<32 for c in v) for v in values.values()):
        raise RefundError("ЮKassa ещё не передала все фискальные реквизиты чека. Повторим получение позже.")
    if any(not values[k].isdigit() for k in required[2:]):
        raise RefundError("Некорректные фискальные реквизиты; картинка не отправлена.")
    contact = receipt.get("customer") or record["snapshot"].get("receipt_contact") or record["request"]
    values.update(amount_cents=total,contact=str(contact.get("email") or contact.get("phone") or "Не указан"),registered_at=receipt.get("registered_at"))
    return values


class RefundStore:
    def __init__(self, path):
        self.path = Path(path)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        with closing(connect_database(self.path)) as c, c:
            c.executescript("""
              CREATE TABLE IF NOT EXISTS customer_refund_requests(
                id INTEGER PRIMARY KEY, source_chat INTEGER NOT NULL, source_message INTEGER NOT NULL,
                source_hash TEXT NOT NULL, source_text TEXT NOT NULL, request TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 1, state TEXT NOT NULL DEFAULT 'new',
                snapshot TEXT, payment_id TEXT, ms_order_id TEXT, amount_cents INTEGER,
                idempotence_key TEXT UNIQUE, approved_by INTEGER, attempted_at TEXT,
                refund_id TEXT UNIQUE, refund_status TEXT, receipt_status TEXT, succeeded_at TEXT, ms_synced INTEGER NOT NULL DEFAULT 0,
                owner_message_id INTEGER, notified_state TEXT, last_error TEXT, note TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(source_chat,source_message));
              CREATE TABLE IF NOT EXISTS customer_refund_audit(
                id INTEGER PRIMARY KEY, request_id INTEGER NOT NULL, revision INTEGER NOT NULL,
                actor INTEGER, action TEXT NOT NULL, created_at TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS customer_refund_ui_limits(
                name TEXT PRIMARY KEY, retry_at REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS customer_refund_attempt_history(
                id INTEGER PRIMARY KEY, request_id INTEGER NOT NULL, operation_key TEXT NOT NULL UNIQUE,
                refund_id TEXT NOT NULL UNIQUE, record_json TEXT NOT NULL, actor INTEGER NOT NULL,
                archived_at TEXT NOT NULL, FOREIGN KEY(request_id) REFERENCES customer_refund_requests(id));
              CREATE TABLE IF NOT EXISTS customer_refund_decision_notices(
                id INTEGER PRIMARY KEY, request_id INTEGER NOT NULL, revision INTEGER NOT NULL,
                state TEXT NOT NULL, note TEXT NOT NULL, order_number TEXT NOT NULL,
                source_chat INTEGER NOT NULL, source_message INTEGER NOT NULL,
                notice_key TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL, message_id INTEGER,
                FOREIGN KEY(request_id) REFERENCES customer_refund_requests(id));
            """)
            columns = {r[1] for r in c.execute("PRAGMA table_info(customer_refund_requests)")}
            for name, kind in {"checked_at":"TEXT", "failure_reason":"TEXT", "receipt_json":"TEXT", "receipt_error":"TEXT", "receipt_owner_message_id":"INTEGER", "receipt_support_message_id":"INTEGER", "completed_message_id":"INTEGER", "split_booked_at":"TEXT", "split_notice_status":"TEXT", "chargeback_at":"TEXT", "gift_review":"TEXT", "payment_lookup":"TEXT", "lookup_notified":"TEXT", "completed_review":"TEXT", "owner_card_hash":"TEXT", "owner_card_pending":"INTEGER NOT NULL DEFAULT 0"}.items():
                if name not in columns: c.execute("ALTER TABLE customer_refund_requests ADD COLUMN " + name + " " + kind)
            notice_columns={r[1] for r in c.execute("PRAGMA table_info(customer_refund_decision_notices)")}
            if "owner_message_id" not in notice_columns:c.execute("ALTER TABLE customer_refund_decision_notices ADD COLUMN owner_message_id INTEGER")

    def get(self, number):
        with closing(connect_database(self.path)) as c:
            row = c.execute("SELECT * FROM customer_refund_requests WHERE id=?", (number,)).fetchone()
        if row is None: raise RefundError("Заявка не найдена.")
        result = dict(row)
        for key in ("request", "snapshot", "receipt_json", "payment_lookup", "gift_review", "completed_review"):
            if result[key]: result[key] = json.loads(result[key])
        return result

    def audit(self, c, number, revision, actor, action):
        c.execute("INSERT INTO customer_refund_audit(request_id,revision,actor,action,created_at) VALUES(?,?,?,?,?)", (number, revision, actor, action, now()))

    def owner_retry_until(self):
        with closing(connect_database(self.path)) as c:
            row=c.execute("SELECT retry_at FROM customer_refund_ui_limits WHERE name='owner_card'").fetchone()
        return float(row[0]) if row else 0

    def defer_owner_until(self,timestamp):
        with closing(connect_database(self.path)) as c,c:
            c.execute("INSERT INTO customer_refund_ui_limits(name,retry_at) VALUES('owner_card',?) ON CONFLICT(name) DO UPDATE SET retry_at=MAX(retry_at,excluded.retry_at)",(float(timestamp),))

    def show_owner_card(self, number, message_id):
        if not isinstance(message_id, int) or message_id <= 0:
            raise RefundError("Не найдено сообщение карточки.")
        with closing(connect_database(self.path)) as c, c:
            c.execute("BEGIN IMMEDIATE")
            # Zero means intentionally hidden, unlike NULL (never delivered).
            c.execute("UPDATE customer_refund_requests SET owner_message_id=0,owner_card_pending=0,owner_card_hash=NULL WHERE owner_message_id=? AND id<>?", (message_id, number))
            c.execute("UPDATE customer_refund_requests SET owner_card_hash=CASE WHEN owner_message_id=? THEN owner_card_hash ELSE NULL END,owner_card_pending=0,owner_message_id=? WHERE id=?", (message_id,message_id, number))

    def hide_owner_card(self, message_id):
        with closing(connect_database(self.path)) as c, c:
            c.execute("UPDATE customer_refund_requests SET owner_message_id=0,owner_card_pending=0,owner_card_hash=NULL WHERE owner_message_id=?", (message_id,))

    def _queue_decision_notice(self, c, number):
        row = c.execute("SELECT * FROM customer_refund_requests WHERE id=?", (number,)).fetchone()
        if not row or row["state"] not in {"rejected", "needs_info"} or not row["note"] or row["attempted_at"]:
            return
        key = hashed([number, row["revision"], row["state"], row["note"]])
        c.execute("""INSERT OR IGNORE INTO customer_refund_decision_notices
            (request_id,revision,state,note,order_number,source_chat,source_message,notice_key,created_at)
            VALUES(?,?,?,?,?,?,?,?,?)""", (number,row["revision"],row["state"],row["note"],
                json.loads(row["request"])["order_number"],row["source_chat"],row["source_message"],key,row["updated_at"]))

    def queue_existing_decision(self, number, actor):
        if not OWNER or actor != OWNER: raise RefundError("Нет доступа к решению по заявке.")
        with closing(connect_database(self.path)) as c, c:
            self._queue_decision_notice(c, number)

    def chargeback(self, number, revision, actor):
        if not OWNER or actor != OWNER: raise RefundError("Отметить чарджбек может только владелец.")
        with closing(connect_database(self.path)) as c, c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM customer_refund_requests WHERE id=?",(number,)).fetchone()
            if not row or row["revision"] != revision:
                raise RefundError("Карточка устарела. Откройте текущую заявку.")
            mismatch=dict(row)
            mismatch["completed_review"]=json.loads(row["completed_review"]) if row["completed_review"] else None
            mismatch["chargeback_at"]=None # Repeated delivery is idempotent below.
            allowed_mismatch=completed_chargeback_available(mismatch)
            if not allowed_mismatch and (row["state"] != "failed" or row["refund_status"] not in {"canceled","FAIL"} or not row["attempted_at"] or not row["refund_id"]):
                raise RefundError("Кнопка доступна после отказа сервиса либо сверки: возврат проведён в МойСклад, но отсутствует в ЮKassa.")
            if not row["chargeback_at"]:
                stamp = now()
                c.execute("UPDATE customer_refund_requests SET chargeback_at=?,updated_at=? WHERE id=?",(stamp,stamp,number))
                c.execute("""INSERT INTO customer_refund_decision_notices
                    (request_id,revision,state,note,order_number,source_chat,source_message,notice_key,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?)""",(number,revision,"chargeback",
                        ("В МойСклад стоит «Возврат-проведен», в ЮKassa возврата нет.\n" if allowed_mismatch else "")+"Клиент обратился в банк, поэтому с нашей стороны возврат провести нельзя.",
                        json.loads(row["request"])["order_number"],row["source_chat"],row["source_message"],
                        hashed([number,"chargeback"]),stamp))
                self.audit(c,number,revision,actor,"chargeback_notice")
        return self.get(number)

    def pending_decision_notices(self, number=None):
        with closing(connect_database(self.path)) as c:
            where = "(message_id IS NULL OR (state='insufficient_funds' AND owner_message_id IS NULL))" + (" AND request_id=?" if number is not None else "")
            return [dict(row) for row in c.execute("SELECT * FROM customer_refund_decision_notices WHERE " + where + " ORDER BY id", (() if number is None else (number,)))]

    def mark_decision_notice(self, notice_id, message_id):
        with closing(connect_database(self.path)) as c, c:
            c.execute("UPDATE customer_refund_decision_notices SET message_id=? WHERE id=? AND message_id IS NULL", (message_id, notice_id))

    def mark_funds_owner_notice(self, notice_id, message_id):
        with closing(connect_database(self.path)) as c, c:
            c.execute("UPDATE customer_refund_decision_notices SET owner_message_id=? WHERE id=? AND state='insufficient_funds' AND owner_message_id IS NULL",(message_id,notice_id))

    def record_insufficient_funds(self, record, fields):
        with closing(connect_database(self.path)) as c, c:
            c.execute("BEGIN IMMEDIATE")
            row=c.execute("SELECT * FROM customer_refund_requests WHERE id=?",(record["id"],)).fetchone()
            if not row or row["revision"]!=record["revision"] or row["idempotence_key"]!=record["idempotence_key"]:
                raise RefundError("Операция изменилась; повторите проверку существующего возврата.")
            fields={**fields,"notified_state":None,"updated_at":now()}
            c.execute("UPDATE customer_refund_requests SET "+",".join(k+"=?" for k in fields)+" WHERE id=?",(*fields.values(),record["id"]))
            c.execute("""INSERT OR IGNORE INTO customer_refund_decision_notices
                (request_id,revision,state,note,order_number,source_chat,source_message,notice_key,created_at)
                VALUES(?,?,?,?,?,?,?,?,?)""",(record["id"],record["revision"],"insufficient_funds",FUNDS_NOTICE,
                    record["request"]["order_number"],record["source_chat"],record["source_message"],
                    hashed([record["id"],"insufficient_funds",fields["refund_id"]]),now()))
        return self.get(record["id"])

    def restart_after_funds(self, record, actor):
        if not OWNER or actor!=OWNER:raise RefundError("Повторную проверку запускает только владелец.")
        with closing(connect_database(self.path)) as c, c:
            c.execute("BEGIN IMMEDIATE")
            row=c.execute("SELECT * FROM customer_refund_requests WHERE id=?",(record["id"],)).fetchone()
            if not row or row["revision"]!=record["revision"] or row["refund_id"]!=record["refund_id"] or row["idempotence_key"]!=record["idempotence_key"]:
                raise RefundError("Заявка уже изменилась; откройте текущую карточку.")
            saved=dict(row);saved["request"]=json.loads(saved["request"])
            if not waiting_funds(saved) or not row["idempotence_key"]:raise RefundError("Предыдущая операция не подтверждена как отменённая из-за недостатка средств.")
            c.execute("INSERT INTO customer_refund_attempt_history(request_id,operation_key,refund_id,record_json,actor,archived_at) VALUES(?,?,?,?,?,?)",
                (record["id"],row["idempotence_key"],row["refund_id"],dump(dict(row)),actor,now()))
            c.execute("""UPDATE customer_refund_requests SET revision=revision+1,state='new',snapshot=NULL,
                idempotence_key=NULL,approved_by=NULL,attempted_at=NULL,refund_id=NULL,refund_status=NULL,
                failure_reason=NULL,receipt_status=NULL,succeeded_at=NULL,ms_synced=0,last_error=NULL,note=NULL,
                receipt_json=NULL,receipt_error=NULL,receipt_owner_message_id=NULL,receipt_support_message_id=NULL,
                completed_message_id=NULL,notified_state=NULL,owner_card_hash=NULL,updated_at=? WHERE id=?""",(now(),record["id"]))
            self.audit(c,record["id"],record["revision"]+1,actor,"retry_after_insufficient_funds:"+row["refund_id"])
        return self.get(record["id"])

    def waiting_for_funds(self):
        with closing(connect_database(self.path)) as c:
            ids=[r[0] for r in c.execute("SELECT id FROM customer_refund_requests WHERE state='failed' AND failure_reason='insufficient_funds' AND refund_status='canceled' AND chargeback_at IS NULL ORDER BY id DESC")]
        return [record for record in (self.get(n) for n in ids) if waiting_funds(record)]

    def ingest(self, chat, message, text, actor):
        request = parse_request(text)
        if request is None: return None, False
        digest = hashed(text)
        with closing(connect_database(self.path)) as c, c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM customer_refund_requests WHERE source_chat=? AND source_message=?", (chat, message)).fetchone()
            if row and row["source_hash"] == digest: return self.get(row["id"]), False
            if row and (row["attempted_at"] or row["chargeback_at"]):
                self.audit(c, row["id"], row["revision"], actor, "source_edited_after_attempt")
                return self.get(row["id"]), False
            if row:
                number, rev = row["id"], row["revision"] + 1
                c.execute("UPDATE customer_refund_requests SET source_hash=?,source_text=?,request=?,revision=?,state='new',snapshot=NULL,payment_id=NULL,ms_order_id=NULL,amount_cents=NULL,last_error=NULL,gift_review=NULL,completed_review=NULL,payment_lookup=NULL,lookup_notified=NULL,updated_at=? WHERE id=?", (digest,text,dump(request),rev,now(),number))
            else:
                cur = c.execute("INSERT INTO customer_refund_requests(source_chat,source_message,source_hash,source_text,request,created_at,updated_at) VALUES(?,?,?,?,?,?,?)", (chat,message,digest,text,dump(request),now(),now()))
                number, rev = cur.lastrowid, 1
            self.audit(c, number, rev, actor, "ingest")
        return self.get(number), True

    def update(self, number, **fields):
        allowed = {"checked_at", "state", "failure_reason", "snapshot", "payment_id", "ms_order_id", "amount_cents", "refund_id", "refund_status", "receipt_status", "succeeded_at", "ms_synced", "owner_message_id", "notified_state", "last_error", "note", "receipt_json", "receipt_error", "receipt_owner_message_id", "receipt_support_message_id", "completed_message_id", "split_booked_at", "split_notice_status", "lookup_notified", "owner_card_hash", "owner_card_pending"}
        assert fields and set(fields) <= allowed
        if isinstance(fields.get("snapshot"), dict): fields["snapshot"] = dump(fields["snapshot"])
        if isinstance(fields.get("receipt_json"), dict): fields["receipt_json"] = dump(fields["receipt_json"])
        previous = self.get(number)
        if any(k in fields and fields[k] != previous[k] for k in ("state","note","last_error","ms_synced","receipt_status")):
            fields["notified_state"] = None
        fields["updated_at"] = now()
        with closing(connect_database(self.path)) as c, c:
            c.execute("UPDATE customer_refund_requests SET " + ",".join(k+"=?" for k in fields) + " WHERE id=?", (*fields.values(),number))
        return self.get(number)

    def lookup(self, number, revision, value):
        encoded = dump(value)
        with closing(connect_database(self.path)) as c, c:
            result = c.execute("UPDATE customer_refund_requests SET lookup_notified=CASE WHEN payment_lookup=? THEN lookup_notified ELSE NULL END,payment_lookup=?,updated_at=? WHERE id=? AND revision=? AND attempted_at IS NULL",(encoded,encoded,now(),number,revision))
            if result.rowcount != 1: raise RefundError("Заявка изменилась; повторите поиск по новой версии.")
        return self.get(number)

    def resolve_method(self, record, method, label):
        if method not in {"yookassa","yandex_split"}:raise RefundError("Неизвестный способ оплаты.")
        with closing(connect_database(self.path)) as c, c:
            c.execute("BEGIN IMMEDIATE")
            row=c.execute("SELECT * FROM customer_refund_requests WHERE id=?",(record["id"],)).fetchone()
            if not row or row["revision"]!=record["revision"] or row["source_hash"]!=record["source_hash"]:
                raise RefundError("Заявка изменилась; повторите проверку.")
            request=json.loads(row["request"])
            if request.get("method")=="unsupported" and row["state"] in EDITABLE and not row["attempted_at"] and not row["chargeback_at"] and not row["snapshot"]:
                request.update(method=method,method_source="tilda",method_source_label=label)
                c.execute("UPDATE customer_refund_requests SET request=?,owner_card_hash=NULL,last_error=CASE WHEN last_error LIKE 'Укажите способ оплаты:%' THEN NULL ELSE last_error END,updated_at=? WHERE id=?",(dump(request),now(),record["id"]))
                self.audit(c,record["id"],record["revision"],0,"method_from_tilda:"+method)
        return self.get(record["id"])

    def confirm_yandex_provider(self, record, order_id):
        with closing(connect_database(self.path)) as c,c:
            c.execute("BEGIN IMMEDIATE")
            row=c.execute("SELECT * FROM customer_refund_requests WHERE id=?",(record["id"],)).fetchone()
            if not row or row["revision"]!=record["revision"] or row["source_hash"]!=record["source_hash"] or row["attempted_at"] or row["snapshot"] or row["chargeback_at"] or row["state"] not in EDITABLE:
                raise RefundError("Заявка изменилась; способ оплаты не изменён. Повторите проверку.")
            request=json.loads(row["request"])
            if request["method"]!="yookassa":raise RefundError("Способ оплаты уже изменился.")
            request.update(method="yandex_split",method_source="yandex_api",yandex_order_id=order_id)
            c.execute("UPDATE customer_refund_requests SET request=?,owner_card_hash=NULL,updated_at=? WHERE id=?",(dump(request),now(),record["id"]))
            self.audit(c,record["id"],record["revision"],0,"provider_corrected_from_yandex_api:"+order_id)
        return self.get(record["id"])

    def enrich_contacts(self,record,customer):
        with closing(connect_database(self.path)) as c,c:
            c.execute("BEGIN IMMEDIATE")
            row=c.execute("SELECT * FROM customer_refund_requests WHERE id=?",(record['id'],)).fetchone()
            if not row or row['revision']!=record['revision'] or row['source_hash']!=record['source_hash']:raise RefundError('Заявка изменилась; откройте текущую карточку.')
            if row['attempted_at'] or row['snapshot']:return self.get(record['id'])
            request=json.loads(row['request']);changed=False
            for field in ('name','email','phone'):
                if not request.get(field) and customer.get(field):request[field]=str(customer[field]);changed=True
            if changed:
                c.execute("UPDATE customer_refund_requests SET request=?,owner_card_hash=NULL,updated_at=? WHERE id=?",(dump(request),now(),record['id']))
                self.audit(c,record['id'],record['revision'],0,'contacts_from_tilda')
        return self.get(record['id'])

    def choose_method(self,record,method,actor):
        if not OWNER or actor!=OWNER or method not in {'yookassa','yandex_split'}:raise RefundError('Способ оплаты выбирает только владелец.')
        with closing(connect_database(self.path)) as c,c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT * FROM customer_refund_requests WHERE id=?',(record['id'],)).fetchone()
            if not row or row['revision']!=record['revision'] or row['source_hash']!=record['source_hash'] or row['attempted_at'] or row['snapshot'] or row['chargeback_at'] or row['state'] not in EDITABLE:raise RefundError('Заявка изменилась или возврат уже начат. Способ оплаты не изменён.')
            request=json.loads(row['request'])
            if request.get('method')!='unsupported':raise RefundError('Способ оплаты уже определён. Откройте текущую карточку.')
            request.update(method=method,method_source='owner')
            c.execute("UPDATE customer_refund_requests SET request=?,revision=revision+1,state='new',last_error=NULL,owner_card_hash=NULL,updated_at=? WHERE id=?",(dump(request),now(),record['id']))
            self.audit(c,record['id'],record['revision']+1,actor,'method_from_owner:'+method)
        return self.get(record['id'])

    def clarify_scope(self, record, scope, actor):
        if not OWNER or actor!=OWNER or scope not in {"all","goods","delivery","part"}:
            raise RefundError("Вариант возврата уточняет только владелец.")
        with closing(connect_database(self.path)) as c, c:
            c.execute("BEGIN IMMEDIATE")
            row=c.execute("SELECT * FROM customer_refund_requests WHERE id=?",(record["id"],)).fetchone()
            if not row or row["revision"]!=record["revision"] or row["source_hash"]!=record["source_hash"] or row["state"] not in {"new","needs_info"} or row["attempted_at"] or row["chargeback_at"] or row["snapshot"]:
                raise RefundError("Заявка изменилась или возврат уже начат.")
            request=json.loads(row["request"])
            if request.get("scope"):raise RefundError("Вариант возврата уже указан; требуется новая проверка заявки.")
            request.update(scope=scope,scope_source="owner")
            c.execute("UPDATE customer_refund_requests SET request=?,revision=revision+1,state='new',last_error=NULL,owner_card_hash=NULL,updated_at=? WHERE id=?",(dump(request),now(),record["id"]))
            self.audit(c,record["id"],record["revision"]+1,actor,"scope_from_owner:"+scope)
        return self.get(record["id"])

    def prepare(self, number, revision, snapshot):
        with closing(connect_database(self.path)) as c, c:
            cur = c.execute("UPDATE customer_refund_requests SET snapshot=?,payment_id=?,ms_order_id=?,amount_cents=?,gift_review=?,completed_review=NULL,revision=revision+1,state='ready',last_error=NULL,updated_at=? WHERE id=? AND revision=? AND attempted_at IS NULL AND chargeback_at IS NULL", (dump(snapshot),snapshot["payment_id"],snapshot["order"]["id"],snapshot["amount_cents"],dump(snapshot["gift_review"]) if snapshot.get("gift_review") else None,now(),number,revision))
            if cur.rowcount != 1: raise RefundError("Заявка изменилась или уже обрабатывается.")
        return self.get(number)

    def preparation_error(self, number, revision, error):
        with closing(connect_database(self.path)) as c, c:
            if isinstance(error,CompletedMismatch):
                c.execute("UPDATE customer_refund_requests SET state='needs_info',revision=revision+1,snapshot=NULL,amount_cents=NULL,gift_review=NULL,completed_review=?,ms_order_id=?,payment_id=?,notified_state=NULL,last_error=?,updated_at=? WHERE id=? AND revision=? AND attempted_at IS NULL AND chargeback_at IS NULL",
                    (dump(error.review),error.review["document_id"],error.review["payment_id"],str(error),now(),number,revision))
                return
            if isinstance(error,GiftClarification):
                c.execute("UPDATE customer_refund_requests SET state='needs_info',revision=revision+1,snapshot=NULL,amount_cents=NULL,completed_review=NULL,gift_review=?,ms_order_id=?,notified_state=NULL,last_error=?,updated_at=? WHERE id=? AND revision=? AND attempted_at IS NULL",
                          (dump(error.review),error.review["document_id"],str(error),now(),number,revision))
                return
            c.execute("UPDATE customer_refund_requests SET state='needs_info',completed_review=NULL,notified_state=NULL,last_error=?,updated_at=? WHERE id=? AND revision=? AND attempted_at IS NULL", (str(error),now(),number,revision))

    def choose_gift(self, number, revision, actor, index, decision):
        if not OWNER or actor!=OWNER or decision not in {"gift","paid"}: raise RefundError("Уточнить подарок может только владелец.")
        with closing(connect_database(self.path)) as c, c:
            c.execute("BEGIN IMMEDIATE")
            row=c.execute("SELECT * FROM customer_refund_requests WHERE id=?",(number,)).fetchone()
            if not row or row["revision"]!=revision or row["attempted_at"] or row["state"] not in EDITABLE:
                raise RefundError("Карточка устарела или возврат уже начат. Решение о подарке не изменено.")
            review=json.loads(row["gift_review"]) if row["gift_review"] else {}
            if not isinstance(index,int) or not 0<=index<len(review.get("items",[])):
                raise RefundError("Уточнение о шарфе устарело. Повторите проверку.")
            review["items"][index]["decision"]=decision
            c.execute("UPDATE customer_refund_requests SET gift_review=?,revision=revision+1,state='new',snapshot=NULL,payment_id=NULL,amount_cents=NULL,last_error=NULL,notified_state=NULL,updated_at=? WHERE id=?",(dump(review),now(),number))
            self.audit(c,number,revision,actor,"scarf_"+decision+":"+review["items"][index]["id"])
        return self.get(number)

    def decision(self, number, revision, actor, state, note=None):
        if not OWNER or actor != OWNER or state not in {"rejected","needs_info"}: raise RefundError("Нет доступа к решению по заявке.")
        with closing(connect_database(self.path)) as c, c:
            cur = c.execute("UPDATE customer_refund_requests SET state=?,note=?,notified_state=NULL,updated_at=? WHERE id=? AND revision=? AND attempted_at IS NULL", (state,note,now(),number,revision))
            if cur.rowcount != 1: raise RefundError("Заявка изменилась или возврат уже начат. Решение не изменено.")
            self.audit(c,number,revision,actor,state)
            self._queue_decision_notice(c, number)
        return self.get(number)

    def claim(self, number, revision, actor):
        if not OWNER or actor != OWNER: raise RefundError("Проведение доступно только владельцу.")
        with closing(connect_database(self.path)) as c, c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT * FROM customer_refund_requests WHERE id=?", (number,)).fetchone()
            if not row or row["revision"] != revision or row["state"] != "ready" or row["attempted_at"] or row["chargeback_at"]:
                raise RefundError("Эта версия уже обрабатывается или устарела. Повторный возврат не отправлен.")
            snapshot=json.loads(row["snapshot"])
            if snapshot.get("existing_refund"):
                raise RefundError("Возврат уже проведён. Можно только подтвердить отправку существующего чека.")
            if c.execute("SELECT id FROM customer_refund_requests WHERE payment_id=? AND chargeback_at IS NOT NULL LIMIT 1",(row["payment_id"],)).fetchone():
                raise RefundError("По этому платежу владелец отметил чарджбек. Новый возврат со стороны магазина не отправлен.")
            known = set(snapshot["previous_refunds"])
            completed = c.execute("SELECT refund_id FROM customer_refund_requests WHERE payment_id=? AND id<>? AND state='succeeded'", (row["payment_id"],number)).fetchall()
            if any(not x["refund_id"] or str(x["refund_id"]).removeprefix("yandex:") not in known for x in completed):
                raise RefundError("Другой возврат уже завершился после проверки карточки. Подготовьте карточку заново.")
            other = c.execute("SELECT id FROM customer_refund_requests WHERE payment_id=? AND id<>? AND state IN ('submitting','uncertain','pending')", (row["payment_id"],number)).fetchone()
            if other: raise RefundError("Другой возврат этого платежа ещё обрабатывается. Сначала дождитесь результата.")
            duplicate = c.execute("SELECT id FROM customer_refund_requests WHERE ms_order_id=? AND id<>? AND state IN ('submitting','uncertain','pending','succeeded')", (row["ms_order_id"],number)).fetchone()
            if duplicate: raise RefundError("По этому документу возврат уже проводился или обрабатывается. Проверьте предыдущую заявку.")
            c.execute("UPDATE customer_refund_requests SET state='submitting',idempotence_key=?,approved_by=?,attempted_at=?,updated_at=? WHERE id=?", (str(uuid.uuid4()),actor,now(),now(),number))
            self.audit(c, number, revision, actor, "owner_confirmed")
        return self.get(number)

    def list(self, pending=False):
        with closing(connect_database(self.path)) as c:
            sql = "SELECT id FROM customer_refund_requests"
            if pending: sql += " WHERE state IN ('new','submitting','uncertain','pending') OR owner_message_id IS NULL OR (owner_card_pending=1 AND owner_message_id<>0) OR (state='failed' AND split_notice_status='PENDING' AND receipt_support_message_id IS NOT NULL) OR (state='succeeded' AND (ms_synced=0 OR (receipt_json IS NULL AND COALESCE(receipt_status,'pending')<>'canceled') OR (receipt_json IS NOT NULL AND completed_message_id IS NULL)))"
            sql += " ORDER BY id DESC" + ("" if pending else " LIMIT 200")
            ids = [r[0] for r in c.execute(sql)]
        return [self.get(i) for i in ids]

    def for_order(self, number):
        with closing(connect_database(self.path)) as c:
            ids=[row["id"] for row in c.execute("SELECT id,request FROM customer_refund_requests ORDER BY id DESC") if json.loads(row["request"]).get("order_number")==number]
        return [self.get(i) for i in ids]


class RefundService:
    def __init__(self, path, base, ms_token):
        from yandex_refunds import YandexRefunds
        self.store, self.base, self.ms_token = RefundStore(path), Path(base), ms_token
        self.yoo = YooKassa(base)
        self.payment_locks = {}
        self.crm_path = None
        self.split = YandexRefunds(self)

    def resolve_method(self, record):
        if record.get("attempted_at") or record.get('snapshot') or not self.crm_path:
            return record
        from database import Database
        original=Database(self.crm_path).get_order_by_number(record["request"]["order_number"])
        original=original if isinstance(original,dict) else {}
        record=self.store.enrich_contacts(record,{'name':original.get('customer_name'),'email':original.get('email'),'phone':original.get('phone')})
        if record['request'].get('method')!='unsupported':return record
        label=str((original or {}).get("payment_method") or "").strip()
        method=payment_method_from_label(label)
        return self.store.resolve_method(record,method,label) if method else record

    def check_brand(self, order):
        organization = SETTINGS.get("ms_organization_id")
        if not organization:
            raise RefundError("Не настроено ограничение МойСклад для бренда SHOPCRM.")
        def entity_id(entity):
            return str(entity.get("id") or (entity.get("meta") or {}).get("href", "").rstrip("/").split("/")[-1])
        if entity_id(order.get("organization") or {}) != organization:
            raise RefundError("Документ принадлежит другой организации МойСклад.")
        scope = SETTINGS.get("ms_brand_scope") or {}
        if scope.get("field") in {"salesChannel", "project"} and scope.get("id"):
            if entity_id(order.get(scope["field"]) or {}) != scope["id"]:
                raise RefundError("Документ принадлежит другому бренду МойСклад.")
        elif scope.get("field") == "attribute" and scope.get("id") and scope.get("value"):
            values = [a.get("value") for a in order.get("attributes", []) if entity_id(a) == scope["id"]]
            if values != [scope["value"]]:
                raise RefundError("Метка бренда документа МойСклад не совпала.")
        elif SETTINGS.get("ms_organization_exclusive") is not True:
            raise RefundError("Для общей организации необходимо ограничение по бренду МойСклад.")

    async def customer(self, record, document):
        """Bind the document, keeping legacy snapshot hashes compatible."""
        r = record["request"]
        if not order_matches(document.get("name"),r["order_number"]):
            raise RefundError("Номер документа МойСклад не соответствует заявке.")
        agent_id = str((document.get("agent") or {}).get("id") or (document.get("agent", {}).get("meta") or {}).get("href", "").rstrip("/").split("/")[-1])
        return hashed({"name":norm(r.get("name")),"email":str(r.get("email") or "").strip().casefold(),
                       "phone":phone(r.get("phone")),"agent":agent_id})

    async def customer_details(self, record, document):
        r = record["request"]
        sources, warnings = [], []
        agent = document.get("agent") or {}
        agent_id = str(agent.get("id") or (agent.get("meta") or {}).get("href", "").rstrip("/").split("/")[-1])
        if re.fullmatch(r"[a-f0-9-]{36}",agent_id):
            try: agent = await self.ms("GET","/entity/counterparty/"+agent_id)
            except RefundError: warnings.append("Контакты покупателя МойСклад не удалось прочитать; не блокирует возврат.")
        sources.append({k:agent.get(k) for k in ("name","email","phone") if agent.get(k)})
        if self.crm_path and Path(self.crm_path).is_file():
            from database import Database
            original = Database(self.crm_path).get_order_by_number(r["order_number"])
            if original:
                sources.append({"name":original.get("customer_name"),"email":original.get("email"),"phone":original.get("phone")})
        for source in sources: warnings.extend(customer_warnings(r,source))
        receipt_contact = valid_contact(*sources)
        sources.append(r)
        customer = {k:next((str(s[k]).strip() for s in sources if s.get(k)),"") for k in ("name","email","phone")}
        return {"customer":customer,"receipt_contact":receipt_contact,"warnings":list(dict.fromkeys(warnings))}

    async def ms(self, method, path, *, params=None, body=None):
        if not self.ms_token:
            secret = self.base / ".secrets/moysklad.json"
            if secret.is_file(): self.ms_token = json.loads(secret.read_text()).get("token", "")
        if not self.ms_token: raise RefundError("МойСклад ещё не подключён. Заявка сохранена.")
        async with httpx.AsyncClient(headers={"Authorization": "Bearer " + self.ms_token, "Accept-Encoding": "gzip"}, timeout=30, follow_redirects=False, trust_env=False) as client:
            try: response = await client.request(method, MS_API + path, params=params, json=body)
            except httpx.TransportError: raise RefundError("МойСклад временно недоступен. Статус денежного возврата сохранён отдельно.")
        if response.status_code != 200: raise RefundError(f"МойСклад: HTTP {response.status_code}. Нужна сверка документа.")
        return response.json()

    async def candidates(self, record):
        number = record["request"]["order_number"]
        data = await self.ms("GET", "/entity/customerorder", params={"search": number, "expand": "state", "limit": 100})
        if int(data.get("meta",{}).get("size",len(data.get("rows",[])))) > len(data.get("rows",[])):
            raise RefundError("Слишком много документов с этим номером. Нужна ручная сверка копий.")
        matches = []
        for o in data.get("rows", []):
            if order_matches(o.get("name"), number) and (return_state((o.get("state") or {}).get("name")) or completed_return_state((o.get("state") or {}).get("name"))):
                try: self.check_brand(o)
                except RefundError: continue
                matches.append(o)
        return matches

    async def document(self, order_id):
        order = await self.ms("GET", "/entity/customerorder/"+order_id, params={"expand": "state"})
        self.check_brand(order)
        rows, offset = [], 0
        while True:
            page = await self.ms("GET", "/entity/customerorder/"+order_id+"/positions", params={"expand": "assortment", "limit": 100, "offset": offset})
            rows.extend(page.get("rows", []))
            if len(page.get("rows", [])) < 100: return order, rows
            offset += 100
            if offset > 1000: raise RefundError("В документе слишком много позиций для автоматического возврата.")

    async def payment(self, record, order):
        number = record["request"]["order_number"]
        hint = record["request"].get("payment_id") or SAMPLE_PAYMENT.get(number)
        if hint:
            if not re.fullmatch(r"[a-f0-9-]{36}", hint): raise RefundError("Некорректный ID платежа ЮKassa.")
            return await self.yoo.get("/payments/"+hint)
        snapshot=record.get('snapshot') or {}
        lookup=record.get('payment_lookup') or {}
        bound=snapshot.get('payment_id') if snapshot.get('source_hash')==record.get('source_hash') and snapshot.get('order',{}).get('id')==record.get('ms_order_id') else None
        cached=lookup.get('payment_id') if lookup.get('status')=='found' and lookup.get('order_number')==number and lookup.get('source_hash')==record.get('source_hash') else None
        if bound or cached:
            candidate=await self.yoo.get('/payments/'+str(bound or cached))
            if self.payment_matches(candidate,number):return candidate
            raise RefundError('Сохранённый платёж не подтвердил номер заказа. Нужна повторная сверка; деньги не отправлены.')
        # Return documents may be renamed or dated long after payment. Use the
        # original Tilda order's date; the window only selects candidates, and
        # payment metadata plus the full preview still verify their identity.
        original = None
        if self.crm_path:
            from database import Database
            original = Database(self.crm_path).get_order_by_number(number)
        moment=None
        if original and original.get("created_at"):
            moment = datetime.fromtimestamp(int(original["created_at"]), timezone.utc)
        else:
            originals = await self.ms("GET", "/entity/customerorder", params={"search":number,"limit":100})
            # A return copy's date is not evidence of when the payment occurred.
            rows=originals.get('rows',[])
            complete=int(originals.get('meta',{}).get('size',len(rows)))==len(rows)
            originals = [o for o in rows if re.fullmatch(re.escape(SETTINGS.get("order_name_prefix", ""))+re.escape(number),str(o.get("name","")),re.I)]
            if complete and len(originals)==1 and originals[0].get('moment'):
                moment = datetime.fromisoformat(originals[0]["moment"]).replace(tzinfo=MOSCOW)
        fallback=moment is None
        if moment is not None:
            start, end = moment-timedelta(days=2), moment+timedelta(days=3)
            rows = await self.yoo.pages("/payments", {"created_at.gte": start.astimezone(timezone.utc).isoformat(), "created_at.lte": end.astimezone(timezone.utc).isoformat(), "status": "succeeded"})
        else:
            rows=await self.yoo.pages('/payments',{'status':'succeeded'},max_pages=160)
        matches = [p for p in rows if self.payment_matches(p,number)]
        if not matches and not fallback:
            # Imports can carry the return date rather than the payment date.
            rows=await self.yoo.pages('/payments',{'status':'succeeded'},max_pages=160)
            matches=[p for p in rows if self.payment_matches(p,number)]
            fallback=True
        if not matches:
            alternate=str(order.get('code') or '').strip()
            if alternate!=number and re.fullmatch(r'\d{5,20}',alternate):
                linked=[p for p in rows if self.payment_matches(p,alternate) and p.get('paid') is True and p.get('test') is False
                        and (p.get('recipient') or {}).get('account_id')==SHOP_ID and (p.get('amount') or {}).get('currency')=='RUB']
                if len(linked)==1:
                    raise PaymentLookupError('Оплата с номером '+number+' в этом магазине ЮKassa не найдена. '
                        'В документе МойСклад указан другой исходный код: '+alternate+'. По нему есть оплата '
                        +rubles(cents(linked[0]['amount']['value']))+'. Уточните исходный номер заказа и исправьте заявку/документ. '
                        'Платёж другого номера автоматически не подставляется.','order_mismatch')
            raise PaymentLookupError('В истории этого магазина ЮKassa не найден оплаченный платёж с номером заказа '+number+'. Проверьте исходный номер и способ оплаты; при наличии укажите payment_id.',"not_found")
        if len(matches)>1:raise PaymentLookupError("Найдено несколько платежей ЮKassa с этим номером заказа. Укажите payment_id в заявке поддержки.","ambiguous")
        payment=matches[0]
        if (fallback and record.get('id') and record.get('revision') and not record.get('attempted_at')
                and payment.get('paid') is True and payment.get('test') is False
                and (payment.get('recipient') or {}).get('account_id')==SHOP_ID and (payment.get('amount') or {}).get('currency')=='RUB'):
            self.store.lookup(record['id'],record['revision'],{'status':'found','order_number':number,'source_hash':record['source_hash'],
                'payment_id':payment['id'],'amount_cents':cents(payment['amount']['value']),
                'refunded_cents':cents((payment.get('refunded_amount') or {'value':'0.00'})['value'])})
        return payment

    @staticmethod
    def payment_matches(payment, number):
        meta = payment.get("metadata") or {}
        for key in ("tilda_orderid", "tilda_order_id", "orderid", "order_id"):
            if meta.get(key) is not None:
                return str(meta[key]).strip() == number
        return bool(re.search(r"(?:заказ[ау]?|order)\s*(?:№|#|n|номер)?\s*"+re.escape(number)+r"(?!\d)",payment.get("description", ""),re.I))

    async def preview(self, record, order_id=None):
        try:return await self._preview(record,order_id)
        except PaymentLookupError as error:
            if error.status!="not_found" or record.get("attempted_at") or record.get("snapshot") or record["request"].get("method")!="yookassa" or record["request"].get("payment_id") or not self.crm_path:raise
            from database import Database
            original=Database(self.crm_path).get_order_by_number(record["request"]["order_number"]) or {}
            if norm(original.get("payment_method")) not in {"яндекс сплит","сплит","yandex split","яндекс пэй","яндекс пей","yandex pay"}:raise
            from yandex_refunds import validate_order
            oid=self.split.api.order_id(record["request"])
            data=await self.split.api.order(oid)
            validate_order(data.get("order"),oid)
            if data["order"].get("paymentStatus") not in {"CAPTURED","PARTIALLY_REFUNDED","REFUNDED"}:raise RefundError("Оплата Яндекс Пэй ещё не подтверждена. Способ оплаты заявки не изменён.")
            fresh=self.store.confirm_yandex_provider(record,oid)
            return await self._preview(fresh,order_id)

    async def _preview(self, record, order_id=None):
        if record.get("chargeback_at"):raise RefundError("Владелец отметил чарджбек: с нашей стороны возврат не проводится.")
        record=self.resolve_method(record)
        r = record["request"]
        if r["method"] == "yandex_split": return await self.split.preview(record,order_id)
        if r["method"] != "yookassa": raise RefundError("Укажите способ оплаты: ЮKassa или Сплит.")
        if not r["scope"]: raise RefundError("Уточните: за вещь, за доставку, за всё или за часть вещей.")
        if order_id is None:
            candidates = await self.candidates(record)
            if len(candidates) != 1: raise RefundError("Выберите документ возврата/отмены: подходящих документов " + str(len(candidates)) + ".")
            order_id = candidates[0]["id"]
        order, positions = await self.document(order_id)
        if not order_matches(order.get("name"), r["order_number"]): raise RefundError("Номер выбранного документа не соответствует заявке.")
        if completed_return_state((order.get("state") or {}).get("name")):
            from completed_refunds import preview_completed
            return await preview_completed(self,record,order,positions)
        customer_hash = await self.customer(record, order)
        document_snapshot,items = selected_document_items(order,positions,r)
        if len(dump(items)) > 2600: raise RefundError("Слишком большой состав для одной карточки. Разделите документы возврата.")
        lookup = None
        payment = await self.payment(record, order)
        pid = payment.get("id")
        if payment.get("status") != "succeeded" or payment.get("paid") is not True or payment.get("test") is not False or (payment.get("recipient") or {}).get("account_id") != SHOP_ID:
            raise RefundError("Платёж не подтверждён как оплаченный боевой платёж вашего магазина.")
        if not self.payment_matches(payment, r["order_number"]):
            raise RefundError("Нельзя подтвердить связь платежа с номером заказа. Требуется уточнение.")
        if payment["amount"].get("currency") != "RUB" or payment.get("transfers") or payment.get("deal"):
            raise RefundError("Этот тип или валюта платежа пока не поддерживаются.")
        metadata = payment.get("metadata") or {}
        previous,receipts = await asyncio.gather(
            self.yoo.pages("/refunds", {"payment_id": pid}),
            self.yoo.pages("/receipts", {"payment_id": pid}))
        if any(x.get("status") not in {"pending","succeeded","canceled"} or (x.get("amount") or {}).get("currency") != "RUB" or x.get("payment_id",pid) != pid for x in previous):
            raise RefundError("Не удалось подтвердить историю возвратов этого платежа.")
        if any(x.get("status") == "pending" for x in previous): raise RefundError("По этому платежу уже есть возврат в обработке.")
        completed = [x for x in previous if x.get("status") == "succeeded"]
        refunded = sum(cents(x["amount"]["value"]) for x in completed)
        if refunded != cents((payment.get("refunded_amount") or {"value":"0.00"})["value"]):
            raise RefundError("Список предыдущих возвратов не совпал с итогом платежа. Повторите сверку позднее.")
        old_receipts = []
        for refund in completed:
            found = await self.yoo.pages("/receipts", {"refund_id": refund["id"]})
            found = [x for x in found if x.get("type") == "refund" and x.get("status") == "succeeded"]
            if len(found) != 1: raise RefundError("Не подтверждён чек предыдущего возврата. Нужна сверка.")
            old_receipts.extend(found)
        details = await self.customer_details(record,order)
        purchases = [i for i in receipts if i.get("type")=="payment" and i.get("status")=="succeeded"]
        if len(purchases)!=1: raise RefundError("Нужен один однозначный зарегистрированный чек исходной оплаты.")
        paid_items=[{"name":i["description"],"quantity":quantity(i["quantity"]),"unit_cents":cents(i["amount"]["value"])} for i in purchases[0].get("items",[])]
        if sum(i["quantity"]*i["unit_cents"] for i in paid_items)!=cents(payment["amount"]["value"]):
            raise RefundError("Итог исходного чека отличается от суммы платежа. Требуется уточнение.")
        if r['scope']=='compensation':
            from refund_compensation import compensation_target, compensation_receipt
            target=compensation_target(r,document_snapshot,paid_items)
            receipt=compensation_receipt(make_receipt(payment,receipts,old_receipts,target,details.get('receipt_contact') or {},'part'),r['compensation_cents'])
            excluded_gifts,gift_review=[],None
        else:
            items,excluded_gifts,gift_review = gift_checked_items(record,order,document_snapshot,items,paid_items,pid,sorted(x["id"] for x in completed))
            # A full refund may contain a zero-price technical shipping line in
            # MS even though no delivery was paid. Never invent its price.
            if r['scope']=='all':
                items=[i for i in items if not (i['delivery'] and i['unit_cents']==0 and not any(p['unit_cents']>0 and names_match(i['name'],p['name']) for p in paid_items))]
            receipt = make_receipt(payment,receipts,old_receipts,items,details.get("receipt_contact") or {},r["scope"],excluded_gifts)
        warnings = list(dict.fromkeys(details["warnings"]+customer_warnings(r,metadata)+customer_warnings(r,receipt["customer"])))
        financial_items = [{"name":i["description"],"quantity":quantity(i["quantity"]),
                            "unit_cents":cents(i["amount"]["value"]),"delivery":delivery(i["description"])} for i in receipt["items"]]
        amount = sum(i["quantity"]*i["unit_cents"] for i in financial_items)
        available = cents(payment["amount"]["value"]) - refunded
        if amount > available:
            raise RefundError(f"Сумма выбранных оплаченных позиций {rubles(amount)} превышает доступный остаток платежа {rubles(available)}. Оплачено: {rubles(cents(payment['amount']['value']))}; уже возвращено: {rubles(refunded)}. Нужна сверка истории платежа и чеков.")
        if amount < 100 or 0 < available - amount < 100:
            raise RefundError("Сумма возврата недоступна с учётом уже проведённых возвратов и ограничений ЮKassa.")
        body = {"payment_id":pid, "amount":money(amount), "description":("Возврат заказа " + r["order_number"])[:250],
                "metadata":{"shopcrm_refund_request":str(record["id"]),"brand":"shopcrm"}}
        # A full original payment refund reuses its registered receipt automatically.
        # Every partial refund carries the exact original fiscal positions.
        if amount != cents(payment["amount"]["value"]):
            body["receipt"] = receipt
        snapshot = {"order":{"id":order["id"],"name":order["name"],"updated":order.get("updated"),"state":order.get("state",{}).get("name"),"items":document_snapshot},
                    "financial_items":financial_items,"payment_id":pid,"amount_cents":amount,"available_cents":available,"payment_amount_cents":cents(payment["amount"]["value"]),
                    "body":body,"customer_hash":customer_hash,"customer":{**details["customer"],**receipt["customer"]},"customer_warnings":warnings,
                    "settings_hash":hashed(SETTINGS),"source_hash":record["source_hash"],"previous_refunds":sorted(x["id"] for x in completed),
                    "receipt_contact":receipt["customer"]}
        if lookup: snapshot["payment_lookup"] = lookup
        if r.get("item_selector") or items != document_snapshot: snapshot["selected_items"] = items
        if r['scope']=='compensation':snapshot['compensation']={'amount_cents':amount,'item_stays_with_customer':True,'item_selector':r.get('item_selector')}
        if gift_review: snapshot.update(gift_review=gift_review,excluded_gifts=excluded_gifts)
        snapshot["hash"] = approval_hash(snapshot)
        return snapshot

    async def execute(self, number, revision, actor):
        from yandex_refunds import is_yandex
        if (self.store.get(number).get("snapshot") or {}).get("existing_refund"):
            raise RefundError("Возврат уже проведён. Повторная отправка денег запрещена; подтвердите отправку существующего чека.")
        if is_yandex(self.store.get(number)): return await self.split.execute(number,revision,actor)
        if not OWNER or actor != OWNER: raise RefundError("Проведение доступно только владельцу.")
        r = self.store.get(number)
        if r["revision"] != revision or r["state"] != "ready" or not r["snapshot"]: raise RefundError("Сначала подготовьте актуальную карточку возврата.")
        async with self.payment_locks.setdefault(r["payment_id"], asyncio.Lock()):
            try: fresh = await self.preview(r, r["ms_order_id"])
            except GiftClarification as error:
                self.store.preparation_error(number,revision,error)
                raise
            except RefundError as error:
                self.store.preparation_error(number,revision,error)
                raise
            if fresh["hash"] != r["snapshot"]["hash"]:
                if fresh.get("existing_refund"):
                    self.store.prepare(number,revision,fresh)
                    raise RefundError("Возврат уже найден в платёжном сервисе. Подтвердите отправку документа сотрудникам.")
                self.store.update(number,state="needs_info",last_error="Документ, платёж или чеки изменились после проверки. Подготовьте карточку заново.")
                raise RefundError("Данные изменились; возврат не отправлен. Подготовьте карточку заново.")
            self.yoo.enabled()
            claimed = self.store.claim(number,revision,actor)
            try:
                body = {**claimed["snapshot"]["body"], "metadata": {**claimed["snapshot"]["body"]["metadata"], "operation_key": claimed["idempotence_key"]}}
                result = await self.yoo.call("POST","/refunds",body=body,key=claimed["idempotence_key"])
            except UncertainRefund as error:
                self.store.update(number,state="uncertain",last_error=str(error)); raise
            except RefundError as error:
                pending=self.store.update(number,state="uncertain",last_error=str(error))
                try:return await self.check(pending)
                except RefundError as lookup_error:
                    return self.store.update(number,state="uncertain",last_error=str(error)+"\nПроверка операции: "+str(lookup_error)+" Новая денежная операция не создаётся.")
            except Exception:
                self.store.update(number,state="uncertain",last_error="Не удалось сохранить результат. Проверяем операцию без повторного проведения."); raise
            return await self.accept_result(claimed, result)

    async def accept_result(self, record, result):
        if (record.get("refund_id") and result.get("id") != record["refund_id"]) or result.get("payment_id") != record["payment_id"] or cents((result.get("amount") or {}).get("value","0")) != record["amount_cents"] or (result.get("amount") or {}).get("currency") != "RUB" or result.get("status") not in {"pending","succeeded","canceled"} or not result.get("id"):
            self.store.update(record["id"],state="uncertain",last_error="Ответ ЮKassa не совпал с подтверждённой операцией.")
            raise RefundError("Не удалось сверить ответ ЮKassa. Требуется проверка операции.")
        state = {"pending":"pending","succeeded":"succeeded","canceled":"failed"}[result["status"]]
        fields = {"refund_id":result["id"],"refund_status":result["status"],"receipt_status":"succeeded" if record.get("receipt_json") else result.get("receipt_registration",record.get("receipt_status")),"state":state,"last_error":None,"failure_reason":None}
        if state == "succeeded": fields["succeeded_at"] = record.get("succeeded_at") or now()
        if state == "failed":
            reason=str((result.get("cancellation_details") or {}).get("reason","причина не указана"))[:100]
            fields["failure_reason"]=reason
            if reason=="insufficient_funds":
                fields["last_error"]="ЮKassa отменила возврат: недостаточно средств магазина для возвратов (insufficient_funds). Деньги клиенту не возвращены. Автоматическая повторная отправка не выполняется."
            else:fields["last_error"]="ЮKassa отменила возврат: "+reason
        saved = self.store.record_insufficient_funds(record,fields) if fields["failure_reason"]=="insufficient_funds" else self.store.update(record["id"],**fields)
        if state == "succeeded" and not saved["ms_synced"]:
            try: await self.sync_ms(saved)
            except RefundError as error: self.store.update(saved["id"],last_error=str(error))
        return self.store.get(record["id"])

    async def retry_after_funds(self, number, revision, actor):
        if not OWNER or actor!=OWNER:raise RefundError("Повторную проверку запускает только владелец.")
        record=self.store.get(number)
        if record["revision"]!=revision or not waiting_funds(record):raise RefundError("Заявка больше не ожидает средств. Откройте текущую карточку.")
        async with self.payment_locks.setdefault(record["payment_id"],asyncio.Lock()):
            record=self.store.get(number)
            if record["revision"]!=revision or not waiting_funds(record):raise RefundError("Заявка уже изменилась.")
            response=await self.yoo.get("/refunds/"+record["refund_id"])
            verified=await self.accept_result(record,response)
            if not waiting_funds(verified):raise RefundError("Предыдущая операция не отменена из-за недостатка средств. Повторная отправка запрещена; проверьте её статус.")
            order_id=verified["ms_order_id"]
            fresh=self.store.restart_after_funds(verified,actor)
            try:
                snapshot=await self.preview(fresh,order_id)
                return self.store.prepare(number,fresh["revision"],snapshot)
            except RefundError as error:
                self.store.preparation_error(number,fresh["revision"],error)
                return self.store.get(number)

    async def check(self, record, force=False):
        from yandex_refunds import is_yandex
        if (record.get("snapshot") or {}).get("existing_refund") and record["state"] in ACTIVE | {"succeeded","failed"}:
            from completed_refunds import check_completed
            return await check_completed(self,record)
        if is_yandex(record): return await self.split.check(record, force=force) if force else await self.split.check(record)
        if force and record.get('refund_id') and record['state'] in {'succeeded','failed'}:
            return await self.accept_result(record,await self.yoo.get('/refunds/'+record['refund_id']))
        if record["state"] == "succeeded":
            if record["receipt_status"] == "pending" and record["refund_id"]:
                return await self.accept_result(record,await self.yoo.get("/refunds/"+record["refund_id"]))
            if not record["ms_synced"]:
                try: await self.sync_ms(record)
                except RefundError as error: self.store.update(record["id"],last_error=str(error))
            return self.store.get(record["id"])
        if record["state"] not in ACTIVE and not (record["state"]=="failed" and record["attempted_at"]):return record
        if record["refund_id"]:
            return await self.accept_result(record,await self.yoo.get("/refunds/"+record["refund_id"]))
        rows = await self.yoo.pages("/refunds",{"payment_id":record["payment_id"]})
        found = [x for x in rows if str((x.get("metadata") or {}).get("shopcrm_refund_request")) == str(record["id"]) and (x.get("metadata") or {}).get("brand") == "shopcrm" and (x.get("metadata") or {}).get("operation_key") == record["idempotence_key"]]
        if len(found) == 1: return await self.accept_result(record,found[0])
        detail=(record.get("last_error") or "").split("\nПроверка операции:",1)[0]
        prefix=detail+"\nПроверка операции: " if "ЮKassa: HTTP" in detail else ""
        return self.store.update(record["id"],state="uncertain",last_error=prefix+"Результат отправки пока не найден. Новая финансовая операция не создаётся.")

    async def receipt(self, record):
        """Read a registered receipt for this exact refund; never create a receipt."""
        from yandex_refunds import is_yandex
        if is_yandex(record): return await self.split.confirmation(record)
        if record["state"] != "succeeded" or not record.get("refund_id") or record.get("receipt_json"):
            return record
        rows = await self.yoo.pages("/receipts", {"refund_id":record["refund_id"]})
        rows = [r for r in rows if r.get("type") == "refund" and r.get("refund_id") == record["refund_id"]]
        if not rows: return self.store.update(record["id"],receipt_status="pending",receipt_error=None)
        if len(rows) != 1: raise RefundError("Найдено несколько чеков возврата. Нужна сверка перед отправкой картинки.")
        receipt = rows[0]
        if receipt.get("payment_id") and receipt["payment_id"] != record["payment_id"]:
            raise RefundError("Чек относится к другому платежу; картинка не отправлена.")
        if receipt.get("status") != "succeeded":
            if receipt.get("status") not in {"pending","canceled"}: raise RefundError("Неизвестный статус чека возврата.")
            return self.store.update(record["id"],receipt_status=receipt["status"],receipt_error=None)
        values=registered_yoo_receipt(receipt,record)
        return self.store.update(record["id"],receipt_status="succeeded",receipt_json=values,receipt_error=None)

    async def sync_ms(self, record):
        if record["state"] != "succeeded" or not record["succeeded_at"]: raise RefundError("Платёжный сервис ещё не подтвердил успешный возврат.")
        await self._sync_ms_document(record, record["succeeded_at"])

    async def book_pending_split(self, record):
        self.split.pending_identity(record)
        if record.get("split_booked_at") and record["ms_synced"]: return
        note = (f'[SHOPCRM-SPLIT-REFUND:{record["id"]}] '
                'Внутренняя отметка оформления возврата. Яндекс принял операцию в обработку (PENDING); '
                'завершение и зачисление клиенту пока не подтверждены. Фактический результат отслеживается в боте.')
        await self._sync_ms_document(record,record["attempted_at"],note)
        self.store.update(record["id"],split_booked_at=record["attempted_at"],receipt_error=None)

    async def _sync_ms_document(self, record, timestamp, booking_note=None):
        prefix = "Яндекс принял возврат в обработку." if booking_note else "Деньги возвращены."
        order, positions = await self.document(record["ms_order_id"])
        if order.get("name") != record["snapshot"]["order"]["name"]:
            raise RefundError(prefix+" Имя документа изменилось; требуется сверка МойСклад.")
        if await self.customer(record, order) != record["snapshot"].get("customer_hash"):
            raise RefundError(prefix+" Покупатель изменился; требуется сверка МойСклад.")
        terminal = norm(order.get("state",{}).get("name")) == "возврат проведен"
        expected = record["snapshot"]["order"]["items"]
        check_order = {**order,"state":{"name":"Возврат"}}
        document_scope = "all" if record["snapshot"].get("selected_items") else record["request"]["scope"]
        if document_items(check_order,positions,document_scope) != expected:
            raise RefundError(prefix+" Состав документа МойСклад изменился — требуется ручная сверка статуса.")
        if not terminal and not return_state(order.get("state",{}).get("name")):
            raise RefundError(prefix+" Статус документа МойСклад изменён другим сотрудником — требуется сверка.")
        # Custom time attributes are stored by MoySklad with minute precision.
        date = datetime.fromisoformat(timestamp).astimezone(MOSCOW).strftime("%Y-%m-%d %H:%M:00")
        def has_date(document):
            return any(str(a.get("value", "")).split(".")[0] == date for a in document.get("attributes", [])
                       if norm(a.get("name")) == "дата проведения возврата средств")
        description = str(order.get("description") or "")
        notes = [booking_note] if booking_note else []
        if record['snapshot'].get('compensation'):
            notes.append(f'[SHOPCRM-COMPENSATION:{record["id"]}] Компенсация {rubles(record["amount_cents"])}. Вещь остаётся у клиента; физический возврат товара этой операцией не оформлялся.')
        selected = record["snapshot"].get("selected_items")
        if selected:
            selection = "; ".join(f'{i["name"]} — {i["quantity"]} шт.' for i in selected)
            notes.append(f'[SHOPCRM-ITEM-REFUND:{record["id"]}] Частичный возврат: {selection}. '
                         f'Сумма: {rubles(record["amount_cents"])}. Остальные товары и доставка этой операцией не возвращались.')
        if record["snapshot"].get("excluded_gifts"):
            gifts="; ".join(i["name"] for i in record["snapshot"]["excluded_gifts"])
            notes.append(f'[SHOPCRM-GIFT-REFUND:{record["id"]}] Владелец подтвердил подарок: {gifts}. Денежный возврат за подарок исключён.')
        has_note = all(note in description for note in notes)
        if terminal and has_date(order) and has_note:
            self.store.update(record["id"],ms_synced=1,last_error=None)
            return
        metadata = await self.ms("GET","/entity/customerorder/metadata")
        states = [s for s in metadata.get("states",[]) if norm(s.get("name")) == "возврат проведен"]
        data = await self.ms("GET","/entity/customerorder/metadata/attributes",params={"limit":1000})
        dates = [a for a in data.get("rows",[]) if norm(a.get("name")) == "дата проведения возврата средств" and a.get("type") == "time"]
        if len(states) != 1 or len(dates) != 1: raise RefundError(prefix+" Не найден однозначный статус или поле даты возврата МойСклад.")
        payload = {"state":{"meta":states[0]["meta"]},"attributes":[{"meta":dates[0]["meta"],"value":date}]}
        if not has_note:
            payload["description"] = "\n\n".join([description.rstrip()]+[note for note in notes if note not in description]).lstrip()
        await self.ms("PUT","/entity/customerorder/"+record["ms_order_id"],body=payload)
        verified = await self.ms("GET","/entity/customerorder/"+record["ms_order_id"],params={"expand":"state"})
        if norm(verified.get("state",{}).get("name")) != "возврат проведен" or not has_date(verified) or any(note not in str(verified.get("description") or "") for note in notes):
            raise RefundError(prefix+" МойСклад не подтвердил статус, дату или пояснение — нужна сверка.")
        self.store.update(record["id"],ms_synced=1,last_error=None)
