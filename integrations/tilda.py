import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Dict, List, Tuple
from zoneinfo import ZoneInfo


PAYMENT_FIELD_RE = re.compile(r"^payment\[([^\]]+)\]$")
PRODUCT_FIELD_RE = re.compile(r"^payment\[products\]\[(\d+)\]\[([^\]]+)\]$")
PRODUCT_OPTION_FIELD_RE = re.compile(
    r"^payment\[products\]\[(\d+)\]\[options\]\[(\d+)\]\[([^\]]+)\]$"
)
DELIVERY_COST_RE = re.compile(r"=\s*([0-9.,]+)\s*$")
MOSCOW = ZoneInfo("Europe/Moscow")


def money_to_kopecks(value: Any) -> int:
    if value in (None, ""):
        return 0
    cleaned = str(value).replace(" ", "").replace(",", ".")
    try:
        return int((Decimal(cleaned) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except InvalidOperation:
        return 0


def _json_value(value: Any, default: Any) -> Any:
    if isinstance(value, (list, dict)):
        return value
    if not value:
        return default
    try:
        return json.loads(str(value))
    except (json.JSONDecodeError, TypeError):
        return default


def _pick(data: Dict[str, Any], *names: str, default: Any = "") -> Any:
    lowered = {str(key).lower(): value for key, value in data.items()}
    for name in names:
        value = lowered.get(name.lower())
        if value not in (None, ""):
            return value
    return default


def parse_tilda_created_at(payload: Dict[str, Any]) -> int:
    """Parse an order timestamp from known Tilda webhook/CSV fields."""
    payment = _payment_data(payload)
    raw = _pick(
        payload,
        "created",
        "created_at",
        "order_created",
        "order_created_at",
        "payment_date",
        "payment_datetime",
        default=_pick(
            payment,
            "created",
            "created_at",
            "date",
            "payment_date",
            "payment_datetime",
        ),
    )
    text = str(raw or "").strip()
    if not text:
        return 0
    if re.fullmatch(r"\d{10,13}", text):
        value = int(text)
        return value // 1000 if value > 9_999_999_999 else value
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
    if parsed is None:
        for pattern in (
            "%Y-%m-%d %H:%M:%S",
            "%d.%m.%Y %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%d.%m.%Y %H:%M",
        ):
            try:
                parsed = datetime.strptime(text, pattern)
                break
            except ValueError:
                continue
    if parsed is None:
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=MOSCOW)
    return int(parsed.timestamp())


def _payment_data(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Собирает payment как из JSON, так и из плоских полей формы Tilda."""
    value = _json_value(_pick(payload, "payment", default={}), {})
    payment = dict(value) if isinstance(value, dict) else {}
    products: Dict[int, Dict[str, Any]] = {}

    existing_products = payment.get("products")
    if isinstance(existing_products, list):
        products.update(
            (index, dict(product))
            for index, product in enumerate(existing_products)
            if isinstance(product, dict)
        )

    for raw_key, field_value in payload.items():
        key = str(raw_key)
        option_match = PRODUCT_OPTION_FIELD_RE.fullmatch(key)
        if option_match:
            product_index = int(option_match.group(1))
            option_index = int(option_match.group(2))
            product = products.setdefault(product_index, {})
            options = product.setdefault("options", [])
            if not isinstance(options, list):
                options = []
                product["options"] = options
            while len(options) <= option_index:
                options.append({})
            options[option_index][option_match.group(3)] = field_value
            continue
        product_match = PRODUCT_FIELD_RE.fullmatch(key)
        if product_match:
            index = int(product_match.group(1))
            products.setdefault(index, {})[product_match.group(2)] = field_value
            continue
        field_match = PAYMENT_FIELD_RE.fullmatch(key)
        if field_match:
            payment[field_match.group(1)] = field_value

    if products:
        payment["products"] = [products[index] for index in sorted(products)]
    return payment


def _product_size(product: Dict[str, Any]) -> str:
    direct = _pick(product, "size", "razmer", "Размер")
    if direct:
        return str(direct)
    options = _json_value(_pick(product, "options", default=[]), [])
    if isinstance(options, dict):
        for key, value in options.items():
            if str(key).lower() in {"size", "размер"}:
                return str(value)
    if isinstance(options, list):
        for option in options:
            if not isinstance(option, dict):
                continue
            name = str(_pick(option, "option", "name", "title")).lower()
            if "размер" in name or "size" in name:
                return str(_pick(option, "variant", "value", "name"))
    return ""


def normalize_tilda_order(payload: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """Преобразует Webhook Tilda в независимую модель CRM.

    Поля Tilda зависят от имён переменных конкретной формы, поэтому здесь
    поддерживаются типовые русские и английские названия. Исходный payload
    всегда сохраняется целиком для последующей точной настройки маппинга.
    """
    payment = _payment_data(payload)

    raw_products = _pick(payload, "products", "product", default=None)
    if raw_products is None:
        raw_products = _pick(payment, "products", "items", default=[])
    products = _json_value(raw_products, [])
    if isinstance(products, dict):
        products = list(products.values())
    if not isinstance(products, list):
        products = []

    order_number = _pick(
        payload, "orderid", "order_id", "order_number", "Номер заказа",
        default=_pick(payment, "orderid", "order_id", "order_number"),
    )
    tranid = str(_pick(payload, "tranid", default=""))
    if not order_number:
        order_number = tranid or _pick(payload, "formid")

    payment_method = str(
        _pick(
            payload, "payment_method", "paymentsystem", "payment_system", "Способ оплаты",
            default=_pick(payment, "system", "sys", "payment_system", "type"),
        )
    )
    if "split" in payment_method.lower() or "сплит" in payment_method.lower():
        payment_method = "Яндекс Сплит"
    elif payment_method:
        payment_method = "Полная оплата"

    delivery_method = str(
        _pick(
            payload,
            "order_delivery_name",
            "delivery_method",
            default=_pick(payment, "delivery", "delivery_name"),
        )
    )
    delivery_cost_kopecks = money_to_kopecks(
        _pick(
            payload,
            "delivery_price",
            "delivery_cost",
            default=_pick(payment, "delivery_price", "delivery_cost"),
        )
    )
    if not delivery_cost_kopecks:
        delivery_cost_match = DELIVERY_COST_RE.search(delivery_method)
        if delivery_cost_match:
            delivery_cost_kopecks = money_to_kopecks(delivery_cost_match.group(1))

    items: List[Dict[str, Any]] = []
    for product in products:
        if not isinstance(product, dict):
            continue
        items.append(
            {
                "product_name": str(_pick(product, "name", "title", "product", default="Товар")),
                "sku": str(_pick(product, "externalid", "sku", "code", default="")),
                "size": _product_size(product),
                "quantity": int(float(_pick(product, "quantity", "qty", default=1) or 1)),
                "unit_price_kopecks": money_to_kopecks(
                    _pick(product, "price", "unit_price", "amount", default=0)
                ),
            }
        )

    total_value = _pick(
        payload, "amount", "total", "total_amount", "Сумма",
        default=_pick(payment, "amount", "total", "total_amount", default=0),
    )
    if not total_value and items:
        total_kopecks = sum(item["unit_price_kopecks"] * item["quantity"] for item in items)
    else:
        total_kopecks = money_to_kopecks(total_value)

    order = {
        "order_number": str(order_number),
        "customer_name": str(
            _pick(
                payload, "name", "fio", "customer_name", "ФИО", "Имя",
                default=_pick(payment, "delivery_fio", "fio", "name"),
            )
        ),
        "phone": str(
            _pick(payload, "phone", "Телефон", default=_pick(payment, "delivery_phone", "phone"))
        ),
        "email": str(
            _pick(
                payload,
                "email",
                "e-mail",
                "Почта",
                default=_pick(payment, "delivery_email", "email"),
            )
        ),
        "telegram_username": str(
            _pick(
                payload,
                "telegram",
                "telegram_username",
                "username",
                "Ваш_username_в_Telegram",
            )
        ),
        "delivery_address": str(
            _pick(
                payload,
                "address",
                "delivery_address",
                "Адрес",
                default=_pick(payment, "delivery_address", "address"),
            )
        ),
        "delivery_method": delivery_method,
        "delivery_city": str(
            _pick(
                payload,
                "city",
                "delivery_city",
                "Город",
                default=_pick(payment, "delivery_city", "city"),
            )
        ),
        "delivery_cost_kopecks": delivery_cost_kopecks,
        "payment_method": payment_method,
        "promo_code": str(
            _pick(
                payload,
                "promocode",
                "promo_code",
                "Промокод",
                default=_pick(payment, "promocode", "promo_code"),
            )
        ),
        "discount_kopecks": money_to_kopecks(
            _pick(
                payload,
                "discount",
                "discount_amount",
                default=_pick(payment, "discount", "discount_amount"),
            )
        ),
        "total_kopecks": total_kopecks,
        "currency": str(_pick(payment, "currency", default="RUB")),
        "status": "paid",
        "source": "tilda",
        "created_at": parse_tilda_created_at(payload) or None,
        "tilda_tranid": tranid or None,
        "cdek_uuid": _pick(payload, "cdek_uuid", "delivery_order_uuid", default=None),
        "cdek_number": _pick(payload, "cdek_number", "track_number", "tracking", default=None),
        "raw_source": payload,
    }
    return order, items


def is_paid_tilda_payload(payload: Dict[str, Any]) -> bool:
    payment = _payment_data(payload)
    transaction_id = _pick(
        payload,
        "paymentid",
        "payment_id",
        "payment[systranid]",
        default=_pick(payment, "systranid", "paymentid", "payment_id"),
    )
    if str(transaction_id or "").strip():
        return True
    status = str(
        _pick(
            payload,
            "payment_status",
            "status_payment",
            default=_pick(payment, "status", "payment_status"),
        )
        or ""
    ).strip().lower()
    return status in {"paid", "оплачен", "оплачено", "success", "succeeded"}
