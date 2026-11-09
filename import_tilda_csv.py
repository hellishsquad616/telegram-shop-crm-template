import argparse
import csv
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence, Tuple
from zoneinfo import ZoneInfo

from database import Database
from integrations.tilda import money_to_kopecks


PAID_TILDA_STATUSES = {"ready for send", "sent", "error"}
PRODUCT_WITH_SIZE_RE = re.compile(
    r"^\s*(.*?)\s*\(Размер:\s*([^)]*)\)\s*-\s*(\d+)x([0-9.,]+)\s*=\s*([0-9.,]+)\s*$"
)
PRODUCT_WITHOUT_SIZE_RE = re.compile(
    r"^\s*(.*?)\s*-\s*(\d+)x([0-9.,]+)\s*=\s*([0-9.,]+)\s*$"
)
DELIVERY_COST_RE = re.compile(r"=\s*([0-9.,]+)\s*$")
MOSCOW = ZoneInfo("Europe/Moscow")


def is_paid_row(row: Dict[str, str]) -> bool:
    payment_id = str(row.get("paymentid") or "").strip()
    status = str(row.get("tilda_status") or "").strip().lower()
    return bool(payment_id and status in PAID_TILDA_STATUSES)


def parse_products(value: str) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for raw_item in filter(None, (part.strip() for part in value.split("; "))):
        match = PRODUCT_WITH_SIZE_RE.fullmatch(raw_item)
        if match:
            name, size, quantity, unit_price, _line_total = match.groups()
        else:
            match = PRODUCT_WITHOUT_SIZE_RE.fullmatch(raw_item)
            if not match:
                raise ValueError(f"Не удалось разобрать товар: {raw_item}")
            name, quantity, unit_price, _line_total = match.groups()
            size = ""
        items.append(
            {
                "product_name": name.strip(),
                "sku": "",
                "size": size.strip(),
                "quantity": int(quantity),
                "unit_price_kopecks": money_to_kopecks(unit_price),
            }
        )
    if not items:
        raise ValueError("У заказа отсутствуют товары")
    return items


def row_created_at(row: Dict[str, str]) -> int:
    value = datetime.strptime(row["created"].strip(), "%Y-%m-%d %H:%M:%S")
    return int(value.replace(tzinfo=MOSCOW).timestamp())


def delivery_cost(value: str) -> int:
    match = DELIVERY_COST_RE.search(value)
    return money_to_kopecks(match.group(1)) if match else 0


def row_to_order(row: Dict[str, str]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    payment_system = str(row.get("paymentsystem") or "").strip().lower()
    payment_method = "Яндекс Сплит" if "split" in payment_system else "Полная оплата"
    delivery_method = str(row.get("order_delivery_name") or "").strip()
    order = {
        "order_number": str(row.get("tilda_orderid") or "").strip(),
        "customer_name": str(row.get("name") or "").strip(),
        "phone": str(row.get("phone") or "").strip(),
        "email": str(row.get("email") or "").strip(),
        "telegram_username": str(row.get("ваш_username_в_telegram") or "").strip(),
        "delivery_address": "",
        "delivery_method": delivery_method,
        "delivery_city": "",
        "delivery_cost_kopecks": delivery_cost(delivery_method),
        "payment_method": payment_method,
        "promo_code": str(row.get("promocode") or "").strip(),
        "discount_kopecks": money_to_kopecks(row.get("discount")),
        "total_kopecks": money_to_kopecks(row.get("amount")),
        "currency": str(row.get("tilda_order_currency") or "RUB").strip() or "RUB",
        "status": "paid",
        "source": "tilda_csv",
        "created_at": row_created_at(row),
        "raw_source": dict(row),
    }
    if not order["order_number"]:
        raise ValueError("У заказа отсутствует tilda_orderid")
    return order, parse_products(str(row.get("product") or ""))


def read_rows(path: Path) -> List[Dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        required = {"created", "tilda_orderid", "tilda_status", "paymentid", "product"}
        missing = required.difference(reader.fieldnames or [])
        if missing:
            raise ValueError(f"В CSV отсутствуют столбцы: {', '.join(sorted(missing))}")
        return [dict(row) for row in reader]


def import_paid_orders(
    database: Database, rows: Sequence[Dict[str, str]], dry_run: bool = False
) -> Dict[str, int]:
    counts = {
        "rows": len(rows),
        "paid": 0,
        "unpaid": 0,
        "duplicates": 0,
        "imported": 0,
        "invalid": 0,
    }
    seen = set()
    for row in rows:
        if not is_paid_row(row):
            counts["unpaid"] += 1
            continue
        counts["paid"] += 1
        order_number = str(row.get("tilda_orderid") or "").strip()
        if not order_number or order_number in seen:
            counts["invalid" if not order_number else "duplicates"] += 1
            continue
        seen.add(order_number)
        if database.get_order_by_number(order_number):
            counts["duplicates"] += 1
            continue
        try:
            order, items = row_to_order(row)
        except (ValueError, TypeError):
            counts["invalid"] += 1
            continue
        if not dry_run:
            database.upsert_order(order, items)
        counts["imported"] += 1
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description="Импорт оплаченных заказов из CSV Tilda")
    parser.add_argument("csv_path", type=Path)
    parser.add_argument("--db", type=Path, default=Path(__file__).with_name("crm.sqlite3"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    database = Database(args.db)
    database.initialize()
    result = import_paid_orders(database, read_rows(args.csv_path), dry_run=args.dry_run)
    print(" ".join(f"{key}={value}" for key, value in result.items()))


if __name__ == "__main__":
    main()
