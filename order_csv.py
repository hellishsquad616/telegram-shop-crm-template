import csv
import json
from datetime import datetime
from io import StringIO
from typing import Any, Dict, Iterable, List
from zoneinfo import ZoneInfo


MOSCOW = ZoneInfo("Europe/Moscow")
TILDA_COLUMNS = [
    "created",
    "tilda_orderid",
    "tilda_status",
    "paymentid",
    "product",
    "order_delivery_name",
    "paymentsystem",
    "name",
    "email",
    "phone",
    "ваш_username_в_telegram",
    "promocode",
    "discount",
    "amount",
    "tilda_order_currency",
]
CRM_COLUMNS = [
    "crm_status",
    "shipment_hold_until",
    "delivery_address",
    "delivery_city",
    "cdek_number",
]


def rubles(kopecks: Any) -> str:
    return f"{int(kopecks or 0) / 100:.2f}"


def product_text(items: Iterable[Dict[str, Any]]) -> str:
    values = []
    for item in items:
        if str(item.get("item_status") or "active") != "active":
            continue
        name = str(item.get("product_name") or "").strip()
        size = str(item.get("size") or "").strip()
        quantity = int(item.get("quantity") or 0)
        price = int(item.get("unit_price_kopecks") or 0)
        title = f"{name} (Размер: {size})" if size else name
        values.append(
            f"{title} - {quantity}x{rubles(price)} = {rubles(price * quantity)}"
        )
    return "; ".join(values)


def export_orders_csv(orders: Iterable[Dict[str, Any]]) -> bytes:
    prepared: List[Dict[str, Any]] = []
    extra_columns: List[str] = []
    known = set(TILDA_COLUMNS + CRM_COLUMNS)
    for order in orders:
        try:
            raw = json.loads(str(order.get("raw_source_json") or "{}"))
        except (TypeError, json.JSONDecodeError):
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        for key in raw:
            name = str(key)
            if name not in known and name not in extra_columns:
                extra_columns.append(name)
        created = datetime.fromtimestamp(int(order["created_at"]), MOSCOW)
        row = dict(raw)
        row.update(
            {
                "created": created.strftime("%Y-%m-%d %H:%M:%S"),
                "tilda_orderid": str(order.get("order_number") or ""),
                "tilda_status": str(raw.get("tilda_status") or "ready for send"),
                "paymentid": str(
                    raw.get("paymentid") or order.get("tilda_tranid") or ""
                ),
                "product": product_text(order.get("items") or []),
                "order_delivery_name": str(order.get("delivery_method") or ""),
                "paymentsystem": str(
                    raw.get("paymentsystem") or order.get("payment_method") or ""
                ),
                "name": str(order.get("customer_name") or ""),
                "email": str(order.get("email") or ""),
                "phone": str(order.get("phone") or ""),
                "ваш_username_в_telegram": str(
                    order.get("telegram_username") or ""
                ),
                "promocode": str(order.get("promo_code") or ""),
                "discount": rubles(order.get("discount_kopecks")),
                "amount": rubles(order.get("total_kopecks")),
                "tilda_order_currency": str(order.get("currency") or "RUB"),
                "crm_status": str(order.get("status") or ""),
                "shipment_hold_until": str(order.get("shipment_hold_until") or ""),
                "delivery_address": str(order.get("delivery_address") or ""),
                "delivery_city": str(order.get("delivery_city") or ""),
                "cdek_number": str(order.get("cdek_number") or ""),
            }
        )
        prepared.append(row)
    output = StringIO(newline="")
    columns = TILDA_COLUMNS + extra_columns + CRM_COLUMNS
    writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(prepared)
    return ("\ufeff" + output.getvalue()).encode("utf-8")
