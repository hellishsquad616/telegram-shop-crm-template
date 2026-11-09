import asyncio
import logging
import os
import re
import tempfile
import time
from contextlib import suppress
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
    TypeHandler,
)
from analytics_helpers import gender_counts
from database import Database, ORDER_STATUSES
from import_tilda_csv import import_paid_orders, read_rows
from integrations.cdek import CdekClient, CdekError
from integrations.tilda_promocodes import TildaPromocodeClient, TildaPromocodeError
from order_csv import export_orders_csv
from sales_categories import parse_rules
from order_check import MENU_CHECK_ORDER
from staff_access import (MENU_STAFF, staff_list, begin_access, receive_access_id,
                          access_callback, block_revoked_staff)


logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)


logger = logging.getLogger(__name__)


logging.getLogger("httpx").setLevel(logging.WARNING)


(
    READONLY_SEARCH, STATUS_SEARCH_QUERY, ACCESS_ID, VERIFY_ORDERS_FILE,
    PROMO_CODE, PROMO_TYPE, PROMO_VALUE, PROMO_LIMIT, PROMO_EXPIRY,
    PROMO_DESCRIPTION, PROMO_CONFIRM, REVENUE_DATE_RANGE,
    CATEGORY_NAME, CATEGORY_RULES, SALES_DATE_RANGE, PRODUCT_SEARCH, PROMO_STATS_DATES,
) = range(17)

PROJECT_DIR = Path(__file__).resolve().parent


MOSCOW_TZ = ZoneInfo("Europe/Moscow")


STATUS_LABELS = {
    "new": "Новый",
    "paid": "Оплачен",
    "processing": "В обработке",
    "partially_returned": "Частичный возврат",
    "returned": "Возврат",
    "cancelled": "Отменено",
    "exchange": "Обмен",
    "exchange_inbound": "Обмен-к-нам",
    "deposit": "Депозит",
    "refund_completed": "Возврат проведён",
}


MENU_ORDERS = "📦 Заказы"
MENU_OPERATIONS = "🔎 Операции с заказами"


MENU_ANALYTICS = "📊 Аналитика"


MENU_SETTINGS = "⚙️ Настройки"


MENU_PROMOCODES = "🎟 Промокоды"

MENU_REFUNDS = "💳 Возвраты средств"


MENU_HOME = "🏠 Главное меню"


MENU_PATTERN = r"^(📦 Заказы|🔎 Операции с заказами|🛠 Операции с заказами|🔎 Проверить заказ|📊 Аналитика|⚙️ Настройки|🎟 Промокоды|💳 Возвраты средств|👥 Права сотрудников|🏠 Главное меню)$"


def db(context: ContextTypes.DEFAULT_TYPE) -> Database:
    return context.application.bot_data["database"]


def cdek(context: ContextTypes.DEFAULT_TYPE) -> CdekClient:
    return context.application.bot_data["cdek"]


def tilda_promo(context: ContextTypes.DEFAULT_TYPE) -> TildaPromocodeClient:
    return context.application.bot_data["tilda_promocodes"]




def user_id(update: Update) -> Optional[int]:
    return update.effective_user.id if update.effective_user else None


def has_permission(
    update: Update, context: ContextTypes.DEFAULT_TYPE, permission: str
) -> bool:
    telegram_id = user_id(update)
    return bool(telegram_id and db(context).has_permission(telegram_id, permission))


async def deny(update: Update) -> None:
    text = "У вас нет прав для этого раздела. Отправьте /id администратору."
    if update.callback_query:
        await update.callback_query.answer(text, show_alert=True)
    elif update.effective_message:
        await update.effective_message.reply_text(text)


def money(kopecks: int) -> str:
    return f"{kopecks / 100:,.2f} ₽".replace(",", " ").replace(".", ",")


def short(value: Any, fallback: str = "—") -> str:
    text = str(value or "").strip()
    return text if text else fallback


def main_menu(staff: Dict[str, Any]) -> InlineKeyboardMarkup:
    buttons: List[List[InlineKeyboardButton]] = []
    if staff["can_orders"]:
        buttons.append([InlineKeyboardButton("📦 Заказы", callback_data="menu:orders")])
    if staff.get("can_operations"):
        buttons.append([InlineKeyboardButton(MENU_OPERATIONS, callback_data="menu:operations")])
    if staff.get("can_check_order"):
        buttons.append([InlineKeyboardButton(MENU_CHECK_ORDER, callback_data="ock:start")])
    if staff["can_analytics"]:
        buttons.append([InlineKeyboardButton("📊 Аналитика", callback_data="menu:analytics")])
    if staff["can_settings"]:
        buttons.append([InlineKeyboardButton("⚙️ Настройки", callback_data="menu:settings")])
    if staff.get("can_promocodes"):
        buttons.append([InlineKeyboardButton("🎟 Промокоды", callback_data="menu:promocodes")])
    if staff.get("can_staff"):
        buttons.append([InlineKeyboardButton(MENU_STAFF, callback_data="settings:staff")])
    from refund_settings import OWNER
    if OWNER and staff.get("telegram_id") == OWNER:
        buttons.append([InlineKeyboardButton(MENU_REFUNDS, callback_data="cr:home")])
    return InlineKeyboardMarkup(buttons)


def main_reply_keyboard(staff: Dict[str, Any]) -> ReplyKeyboardMarkup:
    available: List[str] = []
    if staff["can_orders"]:
        available.append(MENU_ORDERS)
    if staff.get("can_operations"):
        available.append(MENU_OPERATIONS)
    if staff.get("can_check_order"):
        available.append(MENU_CHECK_ORDER)
    if staff["can_analytics"]:
        available.append(MENU_ANALYTICS)
    if staff["can_settings"]:
        available.append(MENU_SETTINGS)
    if staff.get("can_promocodes"):
        available.append(MENU_PROMOCODES)
    if staff.get("can_staff"):
        available.append(MENU_STAFF)
    rows = [available[index:index + 2] for index in range(0, len(available), 2)]
    from refund_settings import OWNER
    if OWNER and staff.get("telegram_id") == OWNER:
        rows.append([MENU_REFUNDS])
    rows.append([MENU_HOME])
    return ReplyKeyboardMarkup(
        rows,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Выберите раздел",
    )


def back_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(MENU_HOME, callback_data="menu:main")]]
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    staff = db(context).get_staff(update.effective_user.id)
    if not staff:
        await update.message.reply_text(
            "Shop CRM CRM\n\nДоступ пока не выдан. "
            f"Передайте администратору ваш Telegram ID: {update.effective_user.id}"
        )
        return
    await update.message.reply_text(
        "Shop CRM — продажи и статистика. Выберите раздел на клавиатуре ниже.",
        reply_markup=main_reply_keyboard(staff),
    )


async def show_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user and update.effective_message:
        await update.effective_message.reply_text(f"Ваш Telegram ID: {update.effective_user.id}")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    staff = db(context).get_staff(user_id(update) or 0)
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text("Операция отменена.")
        if staff and update.callback_query.message:
            await update.callback_query.message.reply_text(
                "Выберите раздел:", reply_markup=main_reply_keyboard(staff)
            )
    elif update.effective_message:
        await update.effective_message.reply_text(
            "Операция отменена.",
            reply_markup=main_reply_keyboard(staff) if staff else None,
        )
    return ConversationHandler.END


async def menu_main(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    staff = db(context).get_staff(user_id(update) or 0)
    if not query or not staff:
        await deny(update)
        return
    await query.answer()
    await query.edit_message_reply_markup(reply_markup=None)
    if query.message:
        await query.message.reply_text(
            "🏠 Главное меню — выберите раздел:",
            reply_markup=main_reply_keyboard(staff),
        )


async def finish_inline_navigation(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Закрывает текущий диалог при переходе по встроенной кнопке «назад»."""
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    destination = str(query.data or "")
    context.user_data.clear()
    if destination == "menu:main":
        await menu_main(update, context)
    elif destination == "menu:orders":
        await orders_summary(update, context)
    elif destination == "menu:operations":
        await operations_menu(update, context)
    elif destination == "menu:analytics":
        await analytics_menu(update, context)
    elif destination == "menu:settings":
        await settings_menu(update, context)
    elif destination == "menu:promocodes":
        await promocodes_menu(update, context)
    elif destination.startswith("status:list:"):
        await list_status_orders(update, context)
    return ConversationHandler.END


def orders_summary_text(context: ContextTypes.DEFAULT_TYPE) -> str:
    stats = db(context).sales_summary("all")
    records = db(context).dashboard()["orders_count"]
    return (
        "📦 Заказы и продажи\n\n"
        f"Всего заказов в CRM: {records}\n"
        f"Заказов с активными продажами: {stats['orders_count']}\n"
        f"Продано товаров: {stats['items_quantity']} шт.\n"
        f"Товарная выручка: {money(stats['revenue_kopecks'])}\n\n"
        "Выберите изделие или дневной отчёт. Поиск заказов — в разделе операций."
    )


async def orders_summary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    if not has_permission(update, context, "orders"):
        await deny(update)
        return
    await query.answer()
    await query.edit_message_text(orders_summary_text(context), reply_markup=orders_keyboard(context))


def orders_keyboard(context: ContextTypes.DEFAULT_TYPE) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("📊 Все товары", callback_data="orders:sales:all")]]
    rows.extend([[InlineKeyboardButton(category["label"], callback_data=f"orders:sales:{category['key']}")]
                 for category in db(context).list_sales_categories()])
    rows.extend([
        [InlineKeyboardButton("🛍 Выбрать товар", callback_data="products:open")],
        [InlineKeyboardButton("☀️ Дневная выручка", callback_data="orders:sales:today")],
        [InlineKeyboardButton(MENU_HOME, callback_data="menu:main")],
    ])
    return InlineKeyboardMarkup(rows)


def sales_report_text(context: ContextTypes.DEFAULT_TYPE, report: str, start_at=None, end_at=None, label="За всё время") -> str:
    if report == "today":
        start_at, end_at, _ = sales_period("today")
        return revenue_report_text(context, start_at, end_at, "today")
    database = db(context)
    category = database.get_sales_report_group(report) if report != "all" else None
    title = category["label"] if category else "📊 Все товары"
    stats = database.sales_summary(report, start_at, end_at)
    top = database.sales_top_items(report, start_at, end_at, limit=10)
    lines = [title, label, "", f"Заказов: {stats['orders_count']}",
             f"Продано товаров: {stats['items_quantity']} шт.",
             f"Сумма товаров до скидок: {money(stats['gross_kopecks'])}",
             f"Товарная выручка: {money(stats['revenue_kopecks'])}", "", "Топ-10 товаров / размеров:"]
    lines.extend(f"• {item['product_name'][:100]} / {short(item['size'])} — {item['quantity']} шт. · {money(item['revenue_kopecks'])}" for item in top)
    if not top:
        lines.append("Пока нет продаж за этот период.")
    lines.extend(["", "Выручка учитывает скидки, без доставки; исключены неоплаченные и отменённые продажи."])
    return "\n".join(lines)[:3900]




async def sales_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not has_permission(update, context, "orders"):
        await deny(update)
        return
    report = str(query.data).rsplit(":", 1)[1]
    try:
        text = sales_report_text(context, report)
    except ValueError as error:
        await query.answer(str(error), show_alert=True)
        return
    await query.answer()
    await query.edit_message_text(text, reply_markup=sales_report_keyboard(report))


def status_filter_keyboard(context: ContextTypes.DEFAULT_TYPE) -> InlineKeyboardMarkup:
    counts = db(context).status_counts()
    buttons = [
        [
            InlineKeyboardButton(
                f"{STATUS_LABELS.get(status, status)} — {counts.get(status, 0)}",
                callback_data=f"status:list:{status}:0",
            )
        ]
        for status in ORDER_STATUSES
    ]
    buttons.extend(
        [
            [InlineKeyboardButton("← К операциям", callback_data="menu:operations")],
        ]
    )
    return InlineKeyboardMarkup(buttons)


async def status_filter_menu(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if not query or not has_permission(update, context, "operations"):
        await deny(update)
        return
    await query.answer()
    await query.edit_message_text(
        "Выберите статус заказа:", reply_markup=status_filter_keyboard(context)
    )


async def list_status_orders(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if not query or not has_permission(update, context, "operations"):
        await deny(update)
        return
    _, _, status, raw_page = query.data.split(":", 3)
    if status not in ORDER_STATUSES:
        await query.answer("Неизвестный статус", show_alert=True)
        return
    page = max(0, int(raw_page))
    page_size = 10
    total = db(context).status_counts().get(status, 0)
    rows = db(context).list_orders_by_status(
        status, limit=page_size, offset=page * page_size
    )
    buttons = [
        [
            InlineKeyboardButton(
                order_choice_label(row), callback_data=f"order:readonly:{row['id']}"
            )
        ]
        for row in rows
    ]
    navigation = []
    if page > 0:
        navigation.append(
            InlineKeyboardButton(
                "←", callback_data=f"status:list:{status}:{page - 1}"
            )
        )
    if (page + 1) * page_size < total:
        navigation.append(
            InlineKeyboardButton(
                "→", callback_data=f"status:list:{status}:{page + 1}"
            )
        )
    if navigation:
        buttons.append(navigation)
    buttons.extend(
        [
            [
                InlineKeyboardButton(
                    "🔎 Найти в этом статусе",
                    callback_data=f"status:search:{status}",
                )
            ],
            [
                InlineKeyboardButton(
                    "📥 Выгрузить CSV",
                    callback_data=f"export:csv:status:{status}",
                )
            ],
            [InlineKeyboardButton("← К статусам", callback_data="orders:statuses")],
        ]
    )
    summary = db(context).status_summary(status)
    item_lines = [
        f"• {row['product_name']} / {short(row['size'])}: "
        f"{row['quantity']} шт. — {money(row['amount'])}"
        for row in summary["items"][:12]
    ]
    if len(summary["items"]) > 12:
        item_lines.append(f"…и ещё позиций: {len(summary['items']) - 12}")
    summary_text = (
        f"{STATUS_LABELS.get(status, status)}: {total}\n"
        f"Сумма заказов: {money(summary['orders_amount'])}\n"
        f"Активных товаров: {summary['items_quantity']} шт. "
        f"на {money(summary['items_amount'])}\n\n"
        + ("\n".join(item_lines) if item_lines else "Активных товаров нет")
    )
    await query.answer()
    await query.edit_message_text(
        summary_text + f"\n\nСтраница {page + 1}. Выберите заказ:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def begin_status_search(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    if not query or not has_permission(update, context, "operations"):
        await deny(update)
        return ConversationHandler.END
    status = query.data.rsplit(":", 1)[1]
    if status not in ORDER_STATUSES:
        await query.answer("Неизвестный статус", show_alert=True)
        return ConversationHandler.END
    context.user_data["status_search"] = status
    await query.answer()
    await query.edit_message_text(
        f"Поиск среди заказов «{STATUS_LABELS.get(status, status)}».\n"
        "Введите номер заказа, телефон или ФИО:",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("← К списку", callback_data=f"status:list:{status}:0")]]
        ),
    )
    return STATUS_SEARCH_QUERY


async def receive_status_search(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if not update.message or not has_permission(update, context, "operations"):
        await deny(update)
        return ConversationHandler.END
    status = str(context.user_data.pop("status_search", ""))
    rows = db(context).search_orders(update.message.text, limit=30, status=status)
    if not rows:
        await update.message.reply_text(
            "В этом статусе заказы не найдены.",
            reply_markup=status_filter_keyboard(context),
        )
        return ConversationHandler.END
    buttons = [
        [
            InlineKeyboardButton(
                order_choice_label(row), callback_data=f"order:readonly:{row['id']}"
            )
        ]
        for row in rows
    ]
    buttons.append(
        [InlineKeyboardButton("← К списку", callback_data=f"status:list:{status}:0")]
    )
    await update.message.reply_text(
        f"Найдено: {len(rows)}", reply_markup=InlineKeyboardMarkup(buttons)
    )
    return ConversationHandler.END


def csv_export_keyboard(context: ContextTypes.DEFAULT_TYPE) -> InlineKeyboardMarkup:
    counts = db(context).status_counts()
    rows = [
        [
            InlineKeyboardButton(
                f"{STATUS_LABELS.get(status, status)} — {counts.get(status, 0)}",
                callback_data=f"export:csv:status:{status}",
            )
        ]
        for status in ORDER_STATUSES
    ]
    rows.insert(
        0,
        [
            InlineKeyboardButton(
                f"Все заказы — {sum(counts.values())}", callback_data="export:csv:all"
            )
        ],
    )
    rows.append([InlineKeyboardButton("← К операциям", callback_data="menu:operations")])
    return InlineKeyboardMarkup(rows)


async def csv_export_menu(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if not query or not has_permission(update, context, "operations"):
        await deny(update)
        return
    await query.answer()
    await query.edit_message_text(
        "Какие заказы выгрузить в CSV?", reply_markup=csv_export_keyboard(context)
    )


async def export_orders_to_csv(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if not query or not has_permission(update, context, "operations"):
        await deny(update)
        return
    data = str(query.data or "")
    label = "orders"
    if data == "export:csv:all":
        order_ids = db(context).all_order_ids()
        label = "all"
    elif data.startswith("export:csv:status:"):
        status = data.rsplit(":", 1)[-1]
        if status not in ORDER_STATUSES:
            await query.answer("Неизвестный статус", show_alert=True)
            return
        order_ids = db(context).order_ids_by_status(status)
        label = status
    else:
        order_ids = []
    if not order_ids:
        await query.answer("Нет заказов для выгрузки", show_alert=True)
        return
    await query.answer("Готовлю CSV…")
    orders = db(context).orders_for_export(order_ids)
    document = BytesIO(export_orders_csv(orders))
    document.name = f"orders_{label}_{datetime.now(MOSCOW_TZ):%Y-%m-%d}.csv"
    if query.message:
        await query.message.reply_document(
            document=document,
            filename=document.name,
            caption=(
                f"Заказов: {len(orders)}. Формат Tilda; в конце добавлены "
                "статус CRM, адрес, город и трек СДЭК."
            ),
        )


async def analytics_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    if not has_permission(update, context, "analytics"):
        await deny(update)
        return
    await query.answer()
    await query.edit_message_text(
        analytics_text(context), reply_markup=analytics_keyboard()
    )


def analytics_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📱 Статистика Telegram", callback_data="analytics:social")],
        [InlineKeyboardButton(MENU_HOME, callback_data="menu:main")],
    ])








def preset_order_period(period: str) -> tuple[datetime, datetime, str]:
    today = datetime.now(MOSCOW_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "yesterday":
        return today - timedelta(days=1), today, "вчера"
    days, label = {
        "3days": (3, "последние 3 дня"),
        "week": (7, "последние 7 дней"),
        "month": (30, "последние 30 дней"),
    }[period]
    return today - timedelta(days=days - 1), today + timedelta(days=1), label








def analytics_text(context: ContextTypes.DEFAULT_TYPE) -> str:
    data = db(context).analytics()
    genders = gender_counts(data["customer_names"])
    all_orders = max(1, int(data["orders_count"]))
    city_lines = [
        f"{index}. {row['city']} — {row['count']} ({row['count'] / all_orders * 100:.1f}%)"
        for index, row in enumerate(data["top_cities"], 1)
    ]
    item_lines = []
    for index, item in enumerate(db(context).sales_top_items("all", limit=10), 1):
        size = f" / {item['size']}" if item["size"] else ""
        item_lines.append(
            f"{index}. {item['product_name']}{size} — {item['quantity']} шт."
        )
    city_text = "\n".join(city_lines) if city_lines else "Нет данных"
    items_text = "\n".join(item_lines) if item_lines else "Нет данных"
    return (
        "📊 Аналитика\n\n"
        f"Заказов: {data['orders_count']}\n"
        f"Сумма заказов: {money(data['orders_amount'])}\n\n"
        "Покупатели по имени:\n"
        f"• Мужчины: {genders['male']}\n"
        f"• Женщины: {genders['female']}\n"
        f"• Не удалось определить: {genders['unknown']}\n\n"
        "Топ-3 города от всех заказов:\n"
        f"{city_text}\n\n"
        f"Город указан у {data['known_city_count']} из {data['orders_count']} заказов.\n\n"
        "Получение заказа:\n"
        f"• Доставка: {data['delivery_count']}\n"
        f"• Самовывоз: {data['pickup_count']}\n"
        f"• Не определено: {data['delivery_unknown_count']}\n"
        f"Расходы на доставку СДЭК: {money(data['cdek_delivery_amount'])}\n\n"
        "Топ-10 позиций/размеров:\n"
        f"{items_text}"
    )


SOCIAL_PLATFORM_LABELS = {
    "telegram": "Telegram @example_shop",
}


def social_delta(current: int, previous: Optional[int]) -> str:
    if previous is None:
        return "нет данных"
    difference = int(current) - int(previous)
    return f"{difference:+d}"


async def collect_social_followers(application: Application) -> Dict[str, Dict[str, Any]]:
    database: Database = application.bot_data["database"]
    now = datetime.now(MOSCOW_TZ)
    try:
        count = int(await application.bot.get_chat_member_count(
            os.getenv("TELEGRAM_SOCIAL_CHAT", "").strip()
        ))
        database.save_social_followers("telegram", now.date().isoformat(), count, "telegram_bot_api", int(now.timestamp()))
        return {"telegram": {"count": count, "snapshot_date": now.date().isoformat()}}
    except (TelegramError, ValueError) as error:
        return {"telegram": {"error": str(error)}}


def social_period_text(
    database: Database, platform: str, current: int, today: datetime
) -> str:
    week_date = (today.date() - timedelta(days=7)).isoformat()
    month_date = (today.date() - timedelta(days=30)).isoformat()
    week = database.social_followers_on(platform, week_date)
    month = database.social_followers_on(platform, month_date)
    week_count = int(week["followers_count"]) if week else None
    month_count = int(month["followers_count"]) if month else None
    return (
        f"📱 {SOCIAL_PLATFORM_LABELS[platform]}\n\n"
        f"Сейчас: {current}\n"
        f"Неделю назад: {week_count if week_count is not None else 'нет данных'}"
        f" ({social_delta(current, week_count)})\n"
        f"Месяц назад: {month_count if month_count is not None else 'нет данных'}"
        f" ({social_delta(current, month_count)})"
    )


async def social_network_stats(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if not query or not has_permission(update, context, "analytics"):
        await deny(update)
        return
    await query.answer("Обновляю данные Telegram…")
    results = await collect_social_followers(context.application)
    database = db(context)
    now = datetime.now(MOSCOW_TZ)
    yesterday_date = (now.date() - timedelta(days=1)).isoformat()
    lines = ["📱 Статистика Telegram", ""]
    for platform, label in SOCIAL_PLATFORM_LABELS.items():
        value = results.get(platform) or {}
        if "count" not in value:
            lines.append(f"{label}: недоступно — {value.get('error', 'ошибка получения')}")
            continue
        current = int(value["count"])
        yesterday = database.social_followers_on(platform, yesterday_date)
        yesterday_count = int(yesterday["followers_count"]) if yesterday else None
        suffix = (
            f"({social_delta(current, yesterday_count)})"
            if yesterday_count is not None
            else "(вчера: нет данных)"
        )
        lines.append(f"{label}: сегодня {current} {suffix}")
    lines.extend(
        [
            "",
            "История начинает накапливаться с первого снимка и обновляется автоматически.",
        ]
    )
    await query.edit_message_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🔄 Обновить", callback_data="analytics:social")],
                [InlineKeyboardButton("← К аналитике", callback_data="menu:analytics")],
            ]
        ),
    )
    if query.message:
        for platform in SOCIAL_PLATFORM_LABELS:
            value = results.get(platform) or {}
            if "count" in value:
                await query.message.reply_text(
                    social_period_text(database, platform, int(value["count"]), now)
                )


def settings_keyboard(staff=None) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🛍 Товары для статистики", callback_data="categories:list:0")],
        [InlineKeyboardButton("🔌 Интеграции", callback_data="settings:integrations")],
        [InlineKeyboardButton("📥 Импорт недостающих из CSV Тильды", callback_data="settings:verify_orders")],
        [InlineKeyboardButton(MENU_HOME, callback_data="menu:main")],
    ]
    if staff is None or staff.get("can_staff"):
        rows.insert(1, [InlineKeyboardButton(MENU_STAFF, callback_data="settings:staff")])
    return InlineKeyboardMarkup(rows)


async def settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    if not has_permission(update, context, "settings"):
        await deny(update)
        return
    await query.answer()
    await query.edit_message_text("⚙️ Настройки:", reply_markup=settings_keyboard(db(context).get_staff(user_id(update) or 0)))


PROMOCODE_STATUS_LABELS = {
    "pending_tilda": "🟡 Передаётся в Tilda",
    "sync_failed": "🔴 Ошибка передачи в Tilda",
    "active": "🟢 Активен",
    "deleted": "⚫️ Удалён",
}


PROMOCODE_CODE_RE = re.compile(r"^[A-Za-z0-9]{1,20}$")


def tilda_promocodes_url() -> str:
    value = os.getenv("TILDA_PROMOCODES_URL", "https://tilda.cc/projects/").strip()
    return value if value.startswith("https://") else "https://tilda.cc/projects/"


def _normalized_decimal(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def parse_promocode_discount(raw: str, discount_type: str) -> str:
    cleaned = str(raw or "").strip().replace(" ", "").replace(",", ".")
    try:
        value = Decimal(cleaned)
    except InvalidOperation as error:
        raise ValueError("Введите скидку числом, например 10 или 1500") from error
    if not value.is_finite() or value <= 0:
        raise ValueError("Скидка должна быть больше нуля")
    if discount_type == "percent" and value > 100:
        raise ValueError("Процентная скидка не может быть больше 100%")
    if discount_type not in {"percent", "fixed"}:
        raise ValueError("Сначала выберите тип скидки")
    if value.as_tuple().exponent < -2:
        raise ValueError("Можно указать не более двух знаков после запятой")
    return _normalized_decimal(value)


def parse_promocode_expiry(raw: str) -> str:
    cleaned = str(raw or "").strip().lower()
    if cleaned in {"0", "-", "нет", "без срока"}:
        return ""
    try:
        parsed = datetime.strptime(cleaned, "%d.%m.%Y").date()
    except ValueError as error:
        raise ValueError("Введите дату в формате ДД.ММ.ГГГГ или 0 без срока") from error
    if parsed < datetime.now().date():
        raise ValueError("Дата окончания уже прошла")
    return parsed.isoformat()


def promocode_discount_text(promo: Dict[str, Any]) -> str:
    value = str(promo["discount_value"])
    return f"{value}%" if promo["discount_type"] == "percent" else f"{value} ₽"


def promocode_expiry_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "без ограничения"
    try:
        return datetime.strptime(text, "%Y-%m-%d").strftime("%d.%m.%Y")
    except ValueError:
        return text


def parse_promocode_channel_message(raw: str) -> Dict[str, Any]:
    lines = [line.strip() for line in str(raw or "").splitlines() if line.strip()]
    if len(lines) != 3:
        raise ValueError(
            "Используйте ровно три строки: название, процент/сумма, количество использований"
        )
    code = lines[0].upper()
    if not PROMOCODE_CODE_RE.fullmatch(code):
        raise ValueError("Название: только латинские буквы и цифры, не более 20 символов")
    discount_raw = lines[1].strip()
    if discount_raw.endswith("%"):
        discount_type = "percent"
        discount_raw = discount_raw[:-1]
    else:
        discount_type = "fixed"
        discount_raw = re.sub(r"(?i)(₽|руб\.?|rub)$", "", discount_raw).strip()
    discount_value = parse_promocode_discount(discount_raw, discount_type)
    try:
        usage_limit = int(lines[2].replace(" ", ""))
    except ValueError as error:
        raise ValueError("Количество использований должно быть целым числом") from error
    if usage_limit <= 0:
        raise ValueError("Количество использований должно быть больше нуля")
    return {
        "code": code,
        "discount_type": discount_type,
        "discount_value": discount_value,
        "usage_limit": usage_limit,
        "expires_on": "",
        "description": "Создано автоматически из канала «Промокоды»",
    }


def promocode_location_matches(
    database: Database, chat_id: int, thread_id: Optional[int]
) -> bool:
    bound_chat = database.get_setting("promocodes_chat_id")
    bound_thread = database.get_setting("promocodes_thread_id")
    return bool(
        bound_chat
        and bound_thread
        and str(int(chat_id)) == bound_chat
        and str(int(thread_id or 0)) == bound_thread
    )


def promocode_text(promo: Dict[str, Any]) -> str:
    limit = int(promo.get("usage_limit") or 0)
    used = int(promo.get("paid_orders_count") or 0)
    created = datetime.fromtimestamp(int(promo["created_at"])).strftime("%d.%m.%Y %H:%M")
    text = (
        f"🎟 Промокод {promo['code']}\n"
        f"Статус: {PROMOCODE_STATUS_LABELS.get(promo['status'], promo['status'])}\n\n"
        f"Скидка: {promocode_discount_text(promo)}\n"
        f"Лимит применений: {limit if limit else 'без ограничения'}\n"
        f"Оплаченных заказов с кодом: {used}\n"
        f"Срок действия: {promocode_expiry_text(promo.get('expires_on'))}\n"
        f"Описание: {short(promo.get('description'))}\n\n"
        f"Создан: {created}\n"
        f"Создал Telegram ID: {promo['created_by']}"
    )
    if promo.get("sync_error"):
        text += f"\n\nОшибка синхронизации: {short(promo['sync_error'])[:500]}"
    return text


def promocodes_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📊 Статистика продаж", callback_data="ps:r:a:today:0")],
            [InlineKeyboardButton("➕ Добавить промокод", callback_data="promo:add")],
            [InlineKeyboardButton("📚 История промокодов", callback_data="promo:list:0")],
            [InlineKeyboardButton(MENU_HOME, callback_data="menu:main")],
        ]
    )


async def promocodes_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not has_permission(update, context, "promocodes"):
        await deny(update)
        return ConversationHandler.END
    context.user_data.clear()
    counts = db(context).promocode_counts()
    text = (
        "🎟 Промокоды\n\n"
        f"Активных: {counts['active']}\n"
        f"Передаются в Tilda: {counts['pending_tilda']}\n"
        f"Ошибок синхронизации: {counts['sync_failed']}\n"
        f"Удалённых в истории: {counts['deleted']}\n\n"
        "Новые промокоды автоматически создаются в Tilda. Удаление защищено: "
        "бот может удалить только созданные им коды."
    )
    if query:
        await query.answer()
        await query.edit_message_text(text, reply_markup=promocodes_keyboard())
    elif update.effective_message:
        await update.effective_message.reply_text(text, reply_markup=promocodes_keyboard())
    return ConversationHandler.END


async def bind_promocodes_chat(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return
    already_bound = db(context).get_setting("promocodes_chat_id")
    staff_allowed = has_permission(update, context, "promocodes") or has_permission(
        update, context, "settings"
    )
    first_named_channel = bool(
        chat.type == "channel"
        and "промокод" in str(chat.title or "").casefold()
        and not already_bound
    )
    if not staff_allowed and not first_named_channel:
        await message.reply_text("Привязать канал может администратор бота.")
        return
    db(context).set_setting("promocodes_chat_id", str(chat.id))
    db(context).set_setting(
        "promocodes_thread_id", str(int(message.message_thread_id or 0))
    )
    baseline = db(context).baseline_promocode_notifications()
    await message.reply_text(
        "✅ Канал «Промокоды» подключён.\n\n"
        "Отправляйте новые промокоды тремя строками:\n"
        "DEMO10\n10%\n100\n\n"
        "Для скидки суммой во второй строке напишите, например, 1500 ₽. "
        f"Старые использования отмечены без уведомлений: {baseline}."
    )


async def ingest_promocode_channel(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat or not message.text or message.text.startswith("/"):
        return
    if not promocode_location_matches(
        db(context), chat.id, message.message_thread_id
    ):
        return
    try:
        promo_data = parse_promocode_channel_message(message.text)
        promocode_id = db(context).create_promocode(
            **promo_data,
            created_by=user_id(update) or 0,
            source_chat_id=chat.id,
            source_message_id=message.message_id,
        )
    except ValueError as error:
        await message.reply_text(f"❌ Промокод не создан: {error}")
        return
    saved = db(context).get_promocode(promocode_id)
    try:
        remote_id = await tilda_promo(context).create(saved)
        db(context).activate_promocode(promocode_id, user_id(update) or 0, remote_id)
    except TildaPromocodeError as error:
        logger.warning("Channel Tilda promocode create failed: %s", error)
        db(context).fail_promocode_sync(promocode_id, str(error))
        await message.reply_text(
            f"❌ Промокод {promo_data['code']} не создан в Tilda: {error}"
        )
        return
    discount = (
        f"{promo_data['discount_value']}%"
        if promo_data["discount_type"] == "percent"
        else f"{promo_data['discount_value']} ₽"
    )
    await message.reply_text(
        "✅ Промокод сформирован\n"
        f"Код: {promo_data['code']}\n"
        f"Скидка: {discount}\n"
        f"Количество использований: {promo_data['usage_limit']}"
    )


async def begin_promocode_create(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    if not query or not has_permission(update, context, "promocodes"):
        await deny(update)
        return ConversationHandler.END
    context.user_data.clear()
    context.user_data["promocode"] = {}
    await query.answer()
    await query.edit_message_text(
        "Введите название промокода.\n\n"
        "Только латинские буквы и цифры, не более 20 символов. Например: DEMO10",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("Отмена", callback_data="flow:cancel")]]
        ),
    )
    return PROMO_CODE


async def receive_promocode_code(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    message = update.effective_message
    if not message or not has_permission(update, context, "promocodes"):
        await deny(update)
        return ConversationHandler.END
    code = str(message.text or "").strip().upper()
    if not PROMOCODE_CODE_RE.fullmatch(code):
        await message.reply_text(
            "Название должно содержать только латинские буквы и цифры — до 20 символов."
        )
        return PROMO_CODE
    context.user_data.setdefault("promocode", {})["code"] = code
    await message.reply_text(
        f"Промокод: {code}\n\nВыберите тип скидки:",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Процент", callback_data="promo:type:percent")],
                [InlineKeyboardButton("Фиксированная сумма", callback_data="promo:type:fixed")],
                [InlineKeyboardButton("Отмена", callback_data="flow:cancel")],
            ]
        ),
    )
    return PROMO_TYPE


async def choose_promocode_type(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    if not query or not has_permission(update, context, "promocodes"):
        await deny(update)
        return ConversationHandler.END
    discount_type = query.data.rsplit(":", 1)[1]
    if discount_type not in {"percent", "fixed"}:
        await query.answer("Неизвестный тип", show_alert=True)
        return PROMO_TYPE
    context.user_data.setdefault("promocode", {})["discount_type"] = discount_type
    example = "10 для скидки 10%" if discount_type == "percent" else "1500 для скидки 1 500 ₽"
    await query.answer()
    await query.edit_message_text(f"Введите размер скидки числом. Например: {example}")
    return PROMO_VALUE


async def receive_promocode_value(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    message = update.effective_message
    if not message or not has_permission(update, context, "promocodes"):
        await deny(update)
        return ConversationHandler.END
    promo = context.user_data.setdefault("promocode", {})
    try:
        promo["discount_value"] = parse_promocode_discount(
            str(message.text or ""), str(promo.get("discount_type") or "")
        )
    except ValueError as error:
        await message.reply_text(str(error))
        return PROMO_VALUE
    await message.reply_text(
        "Введите максимальное количество применений.\n"
        "Отправьте 0, если ограничение не нужно."
    )
    return PROMO_LIMIT


async def receive_promocode_limit(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    message = update.effective_message
    if not message or not has_permission(update, context, "promocodes"):
        await deny(update)
        return ConversationHandler.END
    try:
        usage_limit = int(str(message.text or "").strip())
        if usage_limit < 0:
            raise ValueError
    except ValueError:
        await message.reply_text("Введите целое число 0 или больше.")
        return PROMO_LIMIT
    context.user_data.setdefault("promocode", {})["usage_limit"] = usage_limit
    await message.reply_text(
        "Введите дату окончания в формате ДД.ММ.ГГГГ.\n"
        "Отправьте 0, если срок действия не ограничен."
    )
    return PROMO_EXPIRY


async def receive_promocode_expiry(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    message = update.effective_message
    if not message or not has_permission(update, context, "promocodes"):
        await deny(update)
        return ConversationHandler.END
    try:
        expires_on = parse_promocode_expiry(str(message.text or ""))
    except ValueError as error:
        await message.reply_text(str(error))
        return PROMO_EXPIRY
    context.user_data.setdefault("promocode", {})["expires_on"] = expires_on
    await message.reply_text(
        "Введите описание для внутренней истории — до 255 символов.\n"
        "Отправьте -, если описание не нужно."
    )
    return PROMO_DESCRIPTION


async def receive_promocode_description(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    message = update.effective_message
    if not message or not has_permission(update, context, "promocodes"):
        await deny(update)
        return ConversationHandler.END
    description = str(message.text or "").strip()
    if description == "-":
        description = ""
    if len(description) > 255:
        await message.reply_text("Описание не должно быть длиннее 255 символов.")
        return PROMO_DESCRIPTION
    promo = context.user_data.setdefault("promocode", {})
    promo["description"] = description
    preview = dict(promo)
    preview.update(
        {
            "status": "pending_tilda",
            "created_at": int(time.time()),
            "created_by": user_id(update) or 0,
            "paid_orders_count": 0,
        }
    )
    await message.reply_text(
        promocode_text(preview) + "\n\nСохранить промокод в CRM?",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("✅ Сохранить", callback_data="promo:save")],
                [InlineKeyboardButton("Отмена", callback_data="flow:cancel")],
            ]
        ),
    )
    return PROMO_CONFIRM


async def save_promocode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not has_permission(update, context, "promocodes"):
        await deny(update)
        return ConversationHandler.END
    promo = context.user_data.get("promocode") or {}
    required = {"code", "discount_type", "discount_value", "usage_limit", "expires_on", "description"}
    if not required.issubset(promo):
        await query.answer("Данные формы потеряны. Создайте промокод заново.", show_alert=True)
        context.user_data.clear()
        return ConversationHandler.END
    try:
        promocode_id = db(context).create_promocode(
            **promo, created_by=user_id(update) or 0
        )
    except ValueError as error:
        await query.answer(str(error), show_alert=True)
        return PROMO_CONFIRM
    saved = db(context).get_promocode(promocode_id)
    await query.answer("Создаю в Tilda…")
    await query.edit_message_text(
        promocode_text(saved) + "\n\n⏳ Создаю и проверяю промокод в Tilda…"
    )
    try:
        remote_id = await tilda_promo(context).create(saved)
        db(context).activate_promocode(
            promocode_id, user_id(update) or 0, remote_id
        )
    except TildaPromocodeError as error:
        logger.warning("Tilda promocode create failed: %s", error)
        db(context).fail_promocode_sync(promocode_id, str(error))
    context.user_data.clear()
    saved = db(context).get_promocode(promocode_id)
    suffix = (
        "\n\n✅ Промокод создан и проверен в Tilda."
        if saved["status"] == "active"
        else "\n\nПовторите передачу после проверки настроек Tilda."
    )
    await query.edit_message_text(
        promocode_text(saved) + suffix,
        reply_markup=promocode_card_keyboard(saved),
    )
    return ConversationHandler.END


def promocode_card_keyboard(promo: Dict[str, Any]) -> InlineKeyboardMarkup:
    buttons: List[List[InlineKeyboardButton]] = []
    if promo["status"] in {"pending_tilda", "sync_failed"}:
        buttons.append(
            [InlineKeyboardButton("🔄 Повторить передачу", callback_data=f"promo:retry:{promo['id']}")]
        )
    if promo["status"] != "deleted":
        buttons.append(
            [InlineKeyboardButton("🗑 Удалить промокод", callback_data=f"promo:delete:{promo['id']}")]
        )
    buttons.append([InlineKeyboardButton("📊 Статистика по коду", callback_data=f"ps:r:p{promo['id']}:all:0")])
    buttons.append([InlineKeyboardButton("← К истории", callback_data="promo:list:0")])
    return InlineKeyboardMarkup(buttons)


async def retry_promocode_sync(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if not query or not has_permission(update, context, "promocodes"):
        await deny(update)
        return
    promocode_id = int(query.data.rsplit(":", 1)[1])
    promo = db(context).get_promocode(promocode_id)
    if not promo or promo["status"] not in {"pending_tilda", "sync_failed"}:
        await query.answer("Промокод уже обработан", show_alert=True)
        return
    await query.answer("Повторяю передачу…")
    await query.edit_message_text(
        promocode_text(promo) + "\n\n⏳ Проверяю промокод в Tilda…"
    )
    try:
        remote_id = await tilda_promo(context).create(promo)
        db(context).activate_promocode(
            promocode_id, user_id(update) or 0, remote_id
        )
    except TildaPromocodeError as error:
        logger.warning("Tilda promocode retry failed: %s", error)
        db(context).fail_promocode_sync(promocode_id, str(error))
    promo = db(context).get_promocode(promocode_id)
    suffix = (
        "\n\n✅ Промокод создан и проверен в Tilda."
        if promo["status"] == "active"
        else "\n\nПередать промокод пока не удалось."
    )
    await query.edit_message_text(
        promocode_text(promo) + suffix,
        reply_markup=promocode_card_keyboard(promo),
    )


async def promocodes_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not has_permission(update, context, "promocodes"):
        await deny(update)
        return
    try:
        offset = max(0, int(query.data.rsplit(":", 1)[1]))
    except (ValueError, AttributeError):
        offset = 0
    page_size = 8
    rows = db(context).list_promocodes(page_size + 1, offset)
    visible = rows[:page_size]
    buttons = [
        [
            InlineKeyboardButton(
                f"{PROMOCODE_STATUS_LABELS.get(row['status'], '')[:1]} {row['code']} — {promocode_discount_text(row)}",
                callback_data=f"promo:view:{row['id']}",
            )
        ]
        for row in visible
    ]
    navigation: List[InlineKeyboardButton] = []
    if offset:
        navigation.append(
            InlineKeyboardButton("← Назад", callback_data=f"promo:list:{max(0, offset - page_size)}")
        )
    if len(rows) > page_size:
        navigation.append(
            InlineKeyboardButton("Далее →", callback_data=f"promo:list:{offset + page_size}")
        )
    if navigation:
        buttons.append(navigation)
    buttons.append([InlineKeyboardButton("← К промокодам", callback_data="menu:promocodes")])
    await query.answer()
    await query.edit_message_text(
        "📚 История промокодов\n\n"
        + ("Выберите промокод:" if visible else "Промокодов пока нет."),
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def view_promocode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not has_permission(update, context, "promocodes"):
        await deny(update)
        return
    promo = db(context).get_promocode(int(query.data.rsplit(":", 1)[1]))
    if not promo:
        await query.answer("Промокод не найден", show_alert=True)
        return
    await query.answer()
    await query.edit_message_text(
        promocode_text(promo), reply_markup=promocode_card_keyboard(promo)
    )


async def begin_promocode_activation(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if not query or not has_permission(update, context, "promocodes"):
        await deny(update)
        return
    promo = db(context).get_promocode(int(query.data.rsplit(":", 1)[1]))
    if not promo or promo["status"] != "pending_tilda":
        await query.answer("Промокод уже обработан", show_alert=True)
        return
    await query.answer()
    await query.edit_message_text(
        promocode_text(promo)
        + "\n\nПодтвердите только после того, как промокод с такими же данными "
        "сохранён в Tilda.",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🌐 Открыть Tilda", url=tilda_promocodes_url())],
                [InlineKeyboardButton("✅ Код добавлен в Tilda", callback_data=f"promo:activate_confirm:{promo['id']}")],
                [InlineKeyboardButton("← К промокоду", callback_data=f"promo:view:{promo['id']}")],
            ]
        ),
    )


async def confirm_promocode_activation(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if not query or not has_permission(update, context, "promocodes"):
        await deny(update)
        return
    promocode_id = int(query.data.rsplit(":", 1)[1])
    try:
        db(context).activate_promocode(promocode_id, user_id(update) or 0)
    except ValueError as error:
        await query.answer(str(error), show_alert=True)
        return
    promo = db(context).get_promocode(promocode_id)
    await query.answer("Промокод активирован")
    await query.edit_message_text(
        promocode_text(promo), reply_markup=promocode_card_keyboard(promo)
    )


async def begin_promocode_delete(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if not query or not has_permission(update, context, "promocodes"):
        await deny(update)
        return
    promo = db(context).get_promocode(int(query.data.rsplit(":", 1)[1]))
    if not promo or promo["status"] == "deleted":
        await query.answer("Промокод уже удалён", show_alert=True)
        return
    buttons = [
        [InlineKeyboardButton("🗑 Удалить в Tilda и CRM", callback_data=f"promo:delete_confirm:{promo['id']}")],
        [InlineKeyboardButton("← К промокоду", callback_data=f"promo:view:{promo['id']}")],
    ]
    await query.answer()
    await query.edit_message_text(
        promocode_text(promo)
        + "\n\n⚠️ Удаление необратимо. Бот проверит защитную метку и не сможет "
        "удалить старый или созданный вручную промокод.",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def confirm_promocode_delete(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if not query or not has_permission(update, context, "promocodes"):
        await deny(update)
        return
    promocode_id = int(query.data.rsplit(":", 1)[1])
    promo = db(context).get_promocode(promocode_id)
    if not promo or promo["status"] == "deleted":
        await query.answer("Промокод уже удалён", show_alert=True)
        return
    await query.answer("Проверяю и удаляю…")
    await query.edit_message_text(
        promocode_text(promo) + "\n\n⏳ Проверяю принадлежность коду и удаляю…"
    )
    try:
        await tilda_promo(context).delete(promo)
        db(context).delete_promocode(promocode_id, user_id(update) or 0)
    except (ValueError, TildaPromocodeError) as error:
        logger.warning("Tilda promocode delete failed: %s", error)
        promo = db(context).get_promocode(promocode_id)
        await query.edit_message_text(
            promocode_text(promo)
            + f"\n\n❌ Удаление остановлено: {str(error)[:500]}",
            reply_markup=promocode_card_keyboard(promo),
        )
        return
    promo = db(context).get_promocode(promocode_id)
    await query.edit_message_text(
        promocode_text(promo)
        + "\n\n✅ Промокод удалён из Tilda. Запись сохранена в истории.",
        reply_markup=promocode_card_keyboard(promo),
    )


async def begin_verify_orders(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    if not query or not has_permission(update, context, "settings"):
        await deny(update)
        return ConversationHandler.END
    context.user_data.clear()
    await query.answer()
    await query.edit_message_text(
        "Выгрузите заказы из Tilda в CSV и отправьте файл сюда.\n\n"
        "Бот проверит только оплаченные заказы, пропустит дубли и добавит "
        "недостающие. Перед импортом будет создана резервная копия базы.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("← К настройкам", callback_data="menu:settings")]]
        ),
    )
    return VERIFY_ORDERS_FILE


async def receive_verify_orders(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    message = update.effective_message
    document = message.document if message else None
    if not message or not has_permission(update, context, "settings"):
        await deny(update)
        return ConversationHandler.END
    if not document or not str(document.file_name or "").lower().endswith(".csv"):
        await message.reply_text("Отправьте CSV-файл из Tilda.")
        return VERIFY_ORDERS_FILE
    if int(document.file_size or 0) > 20 * 1024 * 1024:
        await message.reply_text("Файл больше 20 МБ. Сделайте выгрузку за меньший период.")
        return VERIFY_ORDERS_FILE

    await message.reply_text("Проверяю оплаченные заказы…")
    try:
        with tempfile.TemporaryDirectory(prefix="shopcrm_tilda_") as temp_dir:
            csv_path = Path(temp_dir) / "tilda_orders.csv"
            telegram_file = await document.get_file()
            await telegram_file.download_to_drive(custom_path=csv_path)
            rows = read_rows(csv_path)
            backup_path = (
                PROJECT_DIR
                / "backups"
                / f"before_tilda_check_{int(time.time())}.sqlite3"
            )
            database = db(context)
            database.backup_to(backup_path)
            result = import_paid_orders(database, rows)
    except (OSError, ValueError, TelegramError) as error:
        logger.exception("Не удалось проверить CSV Tilda")
        await message.reply_text(
            f"Не удалось обработать CSV: {error}", reply_markup=settings_keyboard(db(context).get_staff(user_id(update) or 0))
        )
        return ConversationHandler.END

    if result["imported"] == 0 and result["invalid"] == 0:
        headline = "✅ Все оплаченные заказы из файла уже есть в системе."
    else:
        headline = f"✅ Проверка завершена. Добавлено заказов: {result['imported']}."
    await message.reply_text(
        f"{headline}\n\n"
        f"Строк в файле: {result['rows']}\n"
        f"Оплаченных: {result['paid']}\n"
        f"Неоплаченных пропущено: {result['unpaid']}\n"
        f"Дублей пропущено: {result['duplicates']}\n"
        f"Ошибочных строк: {result['invalid']}",
        reply_markup=settings_keyboard(db(context).get_staff(user_id(update) or 0)),
    )
    return ConversationHandler.END


def cdek_not_expected(order: Dict[str, Any]) -> bool:
    delivery = str(order.get("delivery_method") or "").lower()
    return order.get("status") in {
        "cancelled", "returned", "refund_completed"
    } or any(
        marker in delivery
        for marker in ("самовывоз", "pickup", "международ", "international")
    )




async def persistent_menu_button(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    message = update.effective_message
    staff = db(context).get_staff(user_id(update) or 0)
    if not message or not staff:
        await deny(update)
        return ConversationHandler.END
    context.user_data.clear()
    selected = str(message.text or "")
    if selected == MENU_STAFF:
        await staff_list(update, context)
    elif selected == MENU_CHECK_ORDER:
        await context.application.bot_data["order_check"].begin(update, context)
    elif selected == MENU_REFUNDS:
        await context.application.bot_data["customer_refunds_ui"].home(update, context)
    elif selected == MENU_HOME:
        await message.reply_text(
            "🏠 Главное меню — выберите раздел:",
            reply_markup=main_reply_keyboard(staff),
        )
    elif selected == MENU_ORDERS:
        if not staff["can_orders"]:
            await deny(update)
        else:
            await message.reply_text(orders_summary_text(context), reply_markup=orders_keyboard(context))
    elif selected in {MENU_OPERATIONS, "🛠 Операции с заказами"}:
        if not staff.get("can_operations"):
            await deny(update)
        else:
            await message.reply_text("🔎 Просмотр заказов:", reply_markup=operations_keyboard())
    elif selected == MENU_ANALYTICS:
        if not staff["can_analytics"]:
            await deny(update)
        else:
            await message.reply_text(
                analytics_text(context), reply_markup=analytics_keyboard()
            )
    elif selected == MENU_SETTINGS:
        if not staff["can_settings"]:
            await deny(update)
        else:
            await message.reply_text("⚙️ Настройки:", reply_markup=settings_keyboard(db(context).get_staff(user_id(update) or 0)))
    elif selected == MENU_PROMOCODES:
        if not staff.get("can_promocodes"):
            await deny(update)
        else:
            counts = db(context).promocode_counts()
            await message.reply_text(
                "🎟 Промокоды\n\n"
                f"Активных: {counts['active']}\n"
                f"Ожидают синхронизации с Tilda: {counts['pending_tilda']}\n"
                f"Удалённых в истории: {counts['deleted']}",
                reply_markup=promocodes_keyboard(),
            )
    return ConversationHandler.END


async def begin_check_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await context.application.bot_data["order_check"].begin(update, context)
    return ConversationHandler.END


def persistent_menu_handler() -> MessageHandler:
    return MessageHandler(filters.Regex(MENU_PATTERN), persistent_menu_button)


async def integrations_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    if not has_permission(update, context, "settings"):
        await deny(update)
        return
    cdek_status = "✅ настроен" if cdek(context).configured else "❌ не настроен"
    tilda_status = "✅ настроен" if os.getenv("TILDA_WEBHOOK_SECRET") else "❌ не настроен"
    promo_status = "✅ настроены" if tilda_promo(context).configured else "❌ не настроены"
    storage = db(context).health_report()
    storage_status = "✅ исправна" if storage["integrity"] == "ok" else "❌ повреждена"
    webhook_base = os.getenv("PUBLIC_BASE_URL", "https://ВАШ-ДОМЕН")
    text = (
        "🔌 Интеграции\n\n"
        f"СДЭК: {cdek_status}\n"
        f"Tilda Webhook: {tilda_status}\n\n"
        f"Промокоды Tilda: {promo_status}\n\n"
        f"База CRM: {storage_status}\n"
        f"Заказов: {storage['orders']}; событий: {storage['order_events']}\n"
        f"Размер базы: {storage['size_bytes'] / 1024 / 1024:.2f} МБ\n\n"
        f"URL для Tilda:\n{webhook_base}/webhooks/tilda/<секрет из .env>\n\n"
        "Секреты задаются только в .env на сервере и не вводятся в Telegram."
    )
    await query.answer()
    await query.edit_message_text(text, reply_markup=settings_keyboard(db(context).get_staff(user_id(update) or 0)))


def order_choice_label(order: Dict[str, Any]) -> str:
    status = STATUS_LABELS.get(str(order.get("status")), str(order.get("status") or "—"))
    created = format_order_created_at(order.get("created_at"), compact=True)
    value = (
        f"№{order['order_number']} | {created} | {status} | "
        f"{short(order.get('customer_name'))}"
    )
    return value[:64]


def format_order_created_at(value: Any, compact: bool = False) -> str:
    try:
        moment = datetime.fromtimestamp(int(value), MOSCOW_TZ)
    except (TypeError, ValueError, OSError):
        return "дата неизвестна" if compact else "не указаны"
    return moment.strftime("%d.%m %H:%M" if compact else "%d.%m.%Y %H:%M МСК")


async def begin_readonly_search(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    query = update.callback_query
    if not query or not has_permission(update, context, "operations"):
        await deny(update)
        return ConversationHandler.END
    await query.answer()
    await query.edit_message_text(
        "Введите ФИО, номер телефона или номер заказа для просмотра:",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("← К операциям", callback_data="menu:operations")]]
        ),
    )
    return READONLY_SEARCH


async def receive_readonly_search(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    if not update.message or not has_permission(update, context, "operations"):
        await deny(update)
        return ConversationHandler.END
    results = db(context).search_orders(update.message.text, limit=30)
    if not results:
        await update.message.reply_text("Заказы не найдены.", reply_markup=operations_keyboard())
        return ConversationHandler.END
    if len(results) == 1:
        await send_readonly_order_card(update.message.chat_id, results[0]["id"], context)
        return ConversationHandler.END
    buttons = [
        [
            InlineKeyboardButton(
                order_choice_label(row), callback_data=f"order:readonly:{row['id']}"
            )
        ]
        for row in results
    ]
    buttons.append([InlineKeyboardButton("← К операциям", callback_data="menu:operations")])
    await update.message.reply_text(
        f"Найдено заказов: {len(results)}. Выберите нужный:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    return ConversationHandler.END


async def order_for_display(
    context: ContextTypes.DEFAULT_TYPE, order_id: int
) -> Optional[Dict[str, Any]]:
    """Refresh a missing CDEK track before rendering an order card."""
    database = db(context)
    order = database.get_order(order_id)
    if (
        not order
        or order.get("cdek_number")
        or cdek_not_expected(order)
        or not cdek(context).configured
    ):
        return order
    database.mark_cdek_sync_attempt(order_id)
    try:
        await sync_order_cdek_track(database, cdek(context), order)
    except Exception as error:
        logger.warning(
            "Автоматическая проверка трека СДЭК для заказа %s: %s",
            order_id,
            error,
        )
        return order
    return database.get_order(order_id) or order


async def send_readonly_order_card(
    chat_id: int, order_id: int, context: ContextTypes.DEFAULT_TYPE
) -> None:
    order = await order_for_display(context, order_id)
    if not order:
        await context.bot.send_message(chat_id, "Заказ не найден.")
        return
    await context.bot.send_message(
        chat_id,
        order_text(order),
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("🔎 Другой заказ", callback_data="orders:browse")],
                [InlineKeyboardButton("← К операциям", callback_data="menu:operations")],
                [InlineKeyboardButton(MENU_HOME, callback_data="menu:main")],
            ]
        ),
    )


async def view_readonly_order(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if not query or not (has_permission(update, context, "orders") or has_permission(update, context, "operations")):
        await deny(update)
        return
    await query.answer()
    await send_readonly_order_card(
        query.message.chat_id, int(query.data.rsplit(":", 1)[1]), context
    )


def order_text(order: Dict[str, Any]) -> str:
    item_lines = []
    for item in order["items"]:
        if item["item_status"] != "active":
            continue
        size = f", размер {item['size']}" if item["size"] else ""
        item_lines.append(
            f"• {item['product_name']}{size} × {item['quantity']} — "
            f"{money(item['unit_price_kopecks'] * item['quantity'])}"
        )
    result = (
        f"📦 Заказ №{order['order_number']}\n"
        f"Статус: {STATUS_LABELS.get(order['status'], order['status'])}\n"
        f"Дата и время: {format_order_created_at(order.get('created_at'))}\n\n"
        f"ФИО: {short(order['customer_name'])}\n"
        f"Телефон: {short(order['phone'])}\n"
        f"Email: {short(order.get('email'))}\n"
        f"Telegram: {short(order['telegram_username'])}\n"
        f"Адрес: {short(order['delivery_address'])}\n"
        f"Доставка: {short(order.get('delivery_method'))}\n"
        f"Оплата: {short(order['payment_method'])}\n"
        f"Промокод: {short(order['promo_code'])}\n"
        f"Скидка: {money(order['discount_kopecks'])}\n"
        f"Сумма: {money(order['total_kopecks'])}\n"
        f"Трек-номер: {short(order['cdek_number'], 'не указан')}\n\n"
        f"Товары в заказе сейчас:\n" + ("\n".join(item_lines) if item_lines else "—")
    )
    return result


async def sync_order_cdek_track(
    database: Database,
    client: CdekClient,
    order: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    if order.get("cdek_uuid"):
        response = await client.get_order(str(order["cdek_uuid"]))
    else:
        response = await client.find_order_by_im_number(str(order["order_number"]))
    if not response:
        return None
    entity = response.get("entity")
    if not isinstance(entity, dict):
        return None
    cdek_uuid = str(entity.get("uuid") or order.get("cdek_uuid") or "").strip()
    cdek_number = str(entity.get("cdek_number") or order.get("cdek_number") or "").strip()
    if not cdek_uuid and not cdek_number:
        return None
    is_new_number = database.save_cdek_order_data(
        int(order["id"]), cdek_uuid, cdek_number, response
    )
    return {
        "uuid": cdek_uuid,
        "number": cdek_number,
        "is_new_number": is_new_number,
    }


async def run_cdek_track_sync(application: Application) -> None:
    database: Database = application.bot_data["database"]
    client: CdekClient = application.bot_data["cdek"]
    if not client.configured:
        return
    lookback_days = max(1, int(os.getenv("CDEK_SYNC_LOOKBACK_DAYS", "30") or 30))
    orders = database.list_orders_for_cdek_sync(lookback_days=lookback_days)
    for order in orders:
        database.mark_cdek_sync_attempt(int(order["id"]))
        try:
            result = await sync_order_cdek_track(database, client, order)
        except CdekError as error:
            logger.warning("Фоновая проверка СДЭК для заказа %s: %s", order["id"], error)
            if "авторизац" in str(error).lower() or "HTTP 401" in str(error):
                break
            continue


async def cdek_track_sync_loop(application: Application) -> None:
    interval = max(60, int(os.getenv("CDEK_SYNC_INTERVAL_SECONDS", "300") or 300))
    while True:
        try:
            await run_cdek_track_sync(application)
        except Exception:
            logger.exception("Непредвиденная ошибка фоновой синхронизации СДЭК")
        await asyncio.sleep(interval)


async def run_promocode_notifications(application: Application) -> None:
    database: Database = application.bot_data["database"]
    raw_chat_id = database.get_setting("promocodes_chat_id")
    raw_thread_id = database.get_setting("promocodes_thread_id")
    if not raw_chat_id or not raw_thread_id:
        return
    try:
        chat_id = int(raw_chat_id)
    except ValueError:
        logger.error("Некорректный promocodes_chat_id: %s", raw_chat_id)
        return
    for usage in database.pending_promocode_notifications(limit=50):
        text = (
            "✅ Промокод использован клиентом\n"
            f"Промокод: {usage['code']}\n"
            f"ФИО: {short(usage['customer_name'])}\n"
            f"Заказ №{usage['order_number']}"
        )
        kwargs: Dict[str, Any] = {}
        thread_id = int(raw_thread_id)
        if thread_id:
            kwargs["message_thread_id"] = thread_id
        if usage.get("source_message_id") and int(usage.get("source_chat_id") or 0) == chat_id:
            kwargs["reply_to_message_id"] = int(usage["source_message_id"])
        try:
            await application.bot.send_message(chat_id, text, **kwargs)
        except TelegramError:
            if not kwargs:
                logger.exception(
                    "Не удалось отправить использование промокода %s", usage["code"]
                )
                continue
            try:
                fallback_kwargs = (
                    {"message_thread_id": thread_id} if thread_id else {}
                )
                await application.bot.send_message(chat_id, text, **fallback_kwargs)
            except TelegramError:
                logger.exception(
                    "Не удалось отправить использование промокода %s", usage["code"]
                )
                continue
        database.mark_promocode_notification(
            int(usage["promocode_id"]), int(usage["order_id"])
        )


async def promocode_notification_loop(application: Application) -> None:
    interval = max(10, int(os.getenv("PROMOCODE_SYNC_INTERVAL_SECONDS", "30") or 30))
    while True:
        try:
            await run_promocode_notifications(application)
        except Exception:
            logger.exception("Ошибка проверки использований промокодов")
        await asyncio.sleep(interval)


async def social_stats_snapshot_loop(application: Application) -> None:
    interval = max(
        3600, int(os.getenv("SOCIAL_STATS_INTERVAL_SECONDS", "21600") or 21600)
    )
    while True:
        try:
            result = await collect_social_followers(application)
            for platform, value in result.items():
                if value.get("error"):
                    logger.info(
                        "Social stats unavailable for %s: %s",
                        platform,
                        value["error"],
                    )
        except Exception:
            logger.exception("Ошибка фонового снимка статистики соцсетей")
        await asyncio.sleep(interval)


async def start_background_tasks(application: Application) -> None:
    if application.bot_data.get("gift_scarves_ui"):
        application.bot_data["gift_scarves_task"] = asyncio.create_task(
            application.bot_data["gift_scarves_ui"].worker(application), name="gift-scarves")
    if application.bot_data.get("customer_refunds_ui"):
        application.bot_data["refunds_task"] = asyncio.create_task(
            application.bot_data["customer_refunds_ui"].worker(application), name="refund-reconciliation")
    if application.bot_data["cdek"].configured:
        application.bot_data["cdek_sync_task"] = asyncio.create_task(
            cdek_track_sync_loop(application), name="cdek-track-sync"
        )
    application.bot_data["promocode_notification_task"] = asyncio.create_task(
        promocode_notification_loop(application), name="promocode-notifications"
    )
    application.bot_data["social_stats_task"] = asyncio.create_task(
        social_stats_snapshot_loop(application), name="social-stats"
    )


async def stop_background_tasks(application: Application) -> None:
    for key in (
        "gift_scarves_task",
        "refunds_task",
        "cdek_sync_task",
        "promocode_notification_task",
        "social_stats_task",
    ):
        task = application.bot_data.get(key)
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Ошибка при обработке %r", update, exc_info=context.error)


def parse_admin_ids(raw: str) -> FrozenSet[int]:
    try:
        return frozenset(int(value.strip()) for value in raw.split(",") if value.strip())
    except ValueError as error:
        raise RuntimeError("ADMIN_IDS должен содержать Telegram ID через запятую") from error


def sales_report_keyboard(category: str, start_at=None, end_at=None) -> InlineKeyboardMarkup:
    if category == "today":
        return revenue_keyboard()
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("За всё время", callback_data=f"sales:period:{category}:all"),
         InlineKeyboardButton("Сегодня", callback_data=f"sales:period:{category}:today")],
        [InlineKeyboardButton("Вчера", callback_data=f"sales:period:{category}:yesterday"),
         InlineKeyboardButton("Неделя", callback_data=f"sales:period:{category}:week"),
         InlineKeyboardButton("Месяц", callback_data=f"sales:period:{category}:month")],
        [InlineKeyboardButton("📅 Выбрать даты", callback_data=f"sales:dates:{category}")],
        [InlineKeyboardButton("👁 Заказы с этими товарами", callback_data=f"sales:orders:{category}:{start_at or 0}:{end_at or 0}:0")],
        [InlineKeyboardButton("🛍 Выбрать товар", callback_data="products:open")],
        [InlineKeyboardButton("← К заказам", callback_data="menu:orders")],
    ])


def sales_period(period: str):
    if period == "all":
        return None, None, "За всё время"
    if period == "today":
        start = datetime.now(MOSCOW_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        end, label = start + timedelta(days=1), "сегодня"
    else:
        start, end, label = preset_order_period(period)
    return int(start.timestamp()), int(end.timestamp()), f"{label}: {start:%d.%m.%Y}–{(end-timedelta(days=1)):%d.%m.%Y} МСК"


async def sales_report_period(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not has_permission(update, context, "orders"):
        await deny(update)
        return
    _, _, category, period = str(query.data).split(":")
    try:
        start_at, end_at, label = sales_period(period)
        text = sales_report_text(context, category, start_at, end_at, label)
    except (ValueError, KeyError) as error:
        await query.answer(str(error), show_alert=True)
        return
    await query.answer()
    await query.edit_message_text(text, reply_markup=sales_report_keyboard(category, start_at, end_at))


async def begin_sales_dates(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not has_permission(update, context, "orders"):
        await deny(update)
        return ConversationHandler.END
    category = str(query.data).rsplit(":", 1)[1]
    try:
        db(context).sales_category_condition(category)
    except ValueError as error:
        await query.answer(str(error), show_alert=True)
        return ConversationHandler.END
    context.user_data["sales_dates_category"] = category
    await query.answer()
    await query.edit_message_text("Введите дату или период: 09.10.2026 или 01.10.2026 — 09.10.2026 (МСК).", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Отмена", callback_data="flow:cancel")]]))
    return SALES_DATE_RANGE


async def receive_sales_dates(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.effective_message
    if not message or not has_permission(update, context, "orders"):
        await deny(update)
        return ConversationHandler.END
    values = re.findall(r"\d{2}\.\d{2}\.\d{4}", str(message.text or ""))
    try:
        if len(values) not in (1, 2):
            raise ValueError("Введите одну или две даты в формате ДД.ММ.ГГГГ.")
        start = datetime.strptime(values[0], "%d.%m.%Y").replace(tzinfo=MOSCOW_TZ)
        last = datetime.strptime(values[-1], "%d.%m.%Y").replace(tzinfo=MOSCOW_TZ)
        if last < start or (last-start).days > 3660:
            raise ValueError("Проверьте порядок дат; максимальный период — 10 лет.")
        end = last + timedelta(days=1)
        category = context.user_data["sales_dates_category"]
        label = f"{start:%d.%m.%Y}–{last:%d.%m.%Y} МСК"
        text = sales_report_text(context, category, int(start.timestamp()), int(end.timestamp()), label)
    except (ValueError, KeyError) as error:
        await message.reply_text(str(error))
        return SALES_DATE_RANGE
    await message.reply_text(text, reply_markup=sales_report_keyboard(category, int(start.timestamp()), int(end.timestamp())))
    return ConversationHandler.END


async def sales_orders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not has_permission(update, context, "orders"):
        await deny(update)
        return
    _, _, category, raw_start, raw_end, raw_page = str(query.data).split(":")
    page = max(0, int(raw_page))
    start_at, end_at = int(raw_start) or None, int(raw_end) or None
    if start_at is not None and end_at is not None:
        start = datetime.fromtimestamp(start_at, MOSCOW_TZ)
        last = datetime.fromtimestamp(end_at, MOSCOW_TZ) - timedelta(days=1)
        label = f"{start:%d.%m.%Y}–{last:%d.%m.%Y} МСК"
    else:
        label = "за всё время"
    try:
        rows = db(context).sales_category_orders(category, start_at, end_at, limit=11, offset=page*10)
    except ValueError as error:
        await query.answer(str(error), show_alert=True)
        return
    buttons = [[InlineKeyboardButton(order_choice_label(row), callback_data=f"order:readonly:{row['id']}")] for row in rows[:10]]
    navigation = []
    if page:
        navigation.append(InlineKeyboardButton("←", callback_data=f"sales:orders:{category}:{raw_start}:{raw_end}:{page-1}"))
    if len(rows) > 10:
        navigation.append(InlineKeyboardButton("→", callback_data=f"sales:orders:{category}:{raw_start}:{raw_end}:{page+1}"))
    if navigation:
        buttons.append(navigation)
    if category.startswith("i"):
        buttons.append([InlineKeyboardButton("🛍 Другой товар", callback_data="products:open")])
    buttons.append([InlineKeyboardButton("← К заказам", callback_data="menu:orders")])
    group = db(context).get_sales_report_group(category) if category != "all" else None
    title = group["label"] if group else "Все товары"
    await query.answer()
    await query.edit_message_text(f"{title[:100]}\nЗаказы с продажами: {label}\nСтраница {page+1}." + ("\nЗаказов нет." if not rows else "\nВыберите заказ:"), reply_markup=InlineKeyboardMarkup(buttons))


def products_picker(context: ContextTypes.DEFAULT_TYPE, page: int = 0):
    query = str(context.user_data.get("product_search", ""))
    products = db(context).sales_products_catalog(query, limit=9, offset=page * 8)
    rows = [
        [InlineKeyboardButton(product["product_name"][:90], callback_data=f"orders:sales:i{product['id']}")]
        for product in products[:8]
    ]
    navigation = []
    if page:
        navigation.append(InlineKeyboardButton("←", callback_data=f"products:page:{page - 1}"))
    if len(products) > 8:
        navigation.append(InlineKeyboardButton("→", callback_data=f"products:page:{page + 1}"))
    if navigation:
        rows.append(navigation)
    rows.append([InlineKeyboardButton("🔎 Найти товар", callback_data="products:search")])
    if query:
        rows.append([InlineKeyboardButton("Все товары", callback_data="products:open")])
    rows.append([InlineKeyboardButton("← К заказам", callback_data="menu:orders")])
    text = "🛍 Выберите товар\n\nПродажи за всё время, количество, выручка, размеры и заказы с этим изделием."
    if query:
        text += f"\nПоиск: {query}"
    text += f"\nСтраница {page + 1}."
    if not products:
        text += "\nТовары не найдены." if query else "\nТовары появятся после получения заказов."
    return text, InlineKeyboardMarkup(rows)


async def sales_products_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not has_permission(update, context, "orders"):
        await deny(update)
        return
    page = 0
    if str(query.data) == "products:open":
        context.user_data.pop("product_search", None)
    else:
        page = max(0, int(str(query.data).rsplit(":", 1)[1]))
    text, keyboard = products_picker(context, page)
    await query.answer()
    await query.edit_message_text(text, reply_markup=keyboard)


async def begin_product_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not has_permission(update, context, "orders"):
        await deny(update)
        return ConversationHandler.END
    await query.answer()
    await query.edit_message_text(
        "Введите название товара или артикул из Тильды:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Отмена", callback_data="flow:cancel")]]),
    )
    return PRODUCT_SEARCH


async def receive_product_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.effective_message
    if not message or not has_permission(update, context, "orders"):
        await deny(update)
        return ConversationHandler.END
    query = str(message.text or "").strip()
    if not query or len(query) > 200:
        await message.reply_text("Введите название или артикул длиной от 1 до 200 символов.")
        return PRODUCT_SEARCH
    context.user_data["product_search"] = query
    text, keyboard = products_picker(context)
    await message.reply_text(text, reply_markup=keyboard)
    return ConversationHandler.END


async def sales_categories_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not has_permission(update, context, "settings"):
        await deny(update)
        return
    page = max(0, int(str(query.data).rsplit(":", 1)[1]))
    categories = db(context).list_sales_categories(include_hidden=True)
    rows = [[InlineKeyboardButton(("👁 " if category["is_active"] else "🙈 ") + category["label"], callback_data=f"categories:view:{category['key']}")] for category in categories[page*8:page*8+8]]
    nav = []
    if page:
        nav.append(InlineKeyboardButton("←", callback_data=f"categories:list:{page-1}"))
    if len(categories) > page*8+8:
        nav.append(InlineKeyboardButton("→", callback_data=f"categories:list:{page+1}"))
    if nav:
        rows.append(nav)
    rows.extend([[InlineKeyboardButton("➕ Добавить товар / категорию", callback_data="categories:add")],
                 [InlineKeyboardButton("← К настройкам", callback_data="menu:settings")]])
    await query.answer()
    await query.edit_message_text("🛍 Товары для статистики\n\nКатегории связаны с названиями и артикулами товаров в заказах Тильды. Новое правило сразу учитывает прошлые и будущие продажи.\nКатегории могут пересекаться. Скрытие убирает только отчёт, данные заказов сохраняются.", reply_markup=InlineKeyboardMarkup(rows))


def sales_category_card(database: Database, key: str):
    entry = database.get_sales_category(key)
    if not entry:
        raise ValueError("Категория не найдена.")
    rules = "\n".join((rule["kind"] + ":" if rule["kind"] != "contains" else "") + rule["value"] for rule in entry["rules"])
    text = f"{entry['label']}\n{'В отчётах' if entry['is_active'] else 'Скрыта'}\n\nПравила (любое совпадение):\n{rules}"
    if entry["is_active"]:
        stats = database.sales_summary(key)
        text += f"\n\nЗаказов: {stats['orders_count']}\nТоваров: {stats['items_quantity']} шт.\nВыручка: {money(stats['revenue_kopecks'])}"
    return text[:3900], InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Название и правила", callback_data=f"categories:edit:{key}")],
        [InlineKeyboardButton("🙈 Скрыть" if entry["is_active"] else "👁 Показать", callback_data=f"categories:toggle:{key}")],
        [InlineKeyboardButton("← К товарам", callback_data="categories:list:0")],
    ])


async def view_sales_category(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not has_permission(update, context, "settings"):
        await deny(update)
        return
    key = str(query.data).rsplit(":", 1)[1]
    try:
        if str(query.data).startswith("categories:toggle:"):
            entry = db(context).get_sales_category(key)
            if not entry:
                raise ValueError("Категория не найдена.")
            db(context).set_sales_category_active(key, not entry["is_active"])
        text, keyboard = sales_category_card(db(context), key)
    except ValueError as error:
        await query.answer(str(error), show_alert=True)
        return
    await query.answer()
    await query.edit_message_text(text, reply_markup=keyboard)


async def begin_sales_category(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not has_permission(update, context, "settings"):
        await deny(update)
        return ConversationHandler.END
    context.user_data.pop("category_draft", None)
    key = str(query.data).rsplit(":", 1)[1] if str(query.data).startswith("categories:edit:") else None
    entry = db(context).get_sales_category(key) if key else None
    if key and not entry:
        await query.answer("Категория не найдена", show_alert=True)
        return ConversationHandler.END
    context.user_data["category_draft"] = {"key": key, "entry": entry}
    await query.answer()
    await query.edit_message_text("Введите название товара или категории (до 60 символов)." + (f"\nСейчас: {entry['label']}\nОтправьте «-», чтобы оставить название." if entry else ""), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Отмена", callback_data="flow:cancel")]]))
    return CATEGORY_NAME


async def receive_category_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.effective_message
    if not message or not has_permission(update, context, "settings"):
        await deny(update)
        return ConversationHandler.END
    draft = context.user_data.get("category_draft")
    if draft is None:
        await message.reply_text("Откройте добавление категории заново.")
        return ConversationHandler.END
    label = str(message.text or "").strip()
    if label == "-" and draft["entry"]:
        label = draft["entry"]["label"]
    if not label or len(label) > 60:
        await message.reply_text("Название должно содержать от 1 до 60 символов.")
        return CATEGORY_NAME
    draft["label"] = label
    await message.reply_text("Введите правила, каждое с новой строки:\nWINDBREAKER — часть названия\nветровк — часть русского названия\nname:DEMO WINDBREAKER BLACK — точное название\nsku:WB-001 — точный externalid / артикул из Тильды\n\nДостаточно любого совпадения. Регистр не учитывается. Можно выбрать товар из полученных заказов Тильды кнопкой ниже." + ("\nОтправьте «-», чтобы оставить текущие правила." if draft["entry"] else ""), reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("🛍 Выбрать товар из Тильды", callback_data="categories:pick:0")],
        [InlineKeyboardButton("Отмена", callback_data="flow:cancel")],
    ]))
    return CATEGORY_RULES


async def pick_category_product(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not has_permission(update, context, "settings"):
        await deny(update)
        return ConversationHandler.END
    catalog = db(context).tilda_sales_catalog()
    context.user_data["category_catalog"] = catalog
    page = max(0, int(str(query.data).rsplit(":", 1)[1]))
    rows = [[InlineKeyboardButton(f"{item['product_name']} · {item['sku'] or 'без артикула'}"[:85], callback_data=f"categories:product:{index}")] for index, item in enumerate(catalog[page*8:page*8+8], page*8)]
    nav = []
    if page:
        nav.append(InlineKeyboardButton("←", callback_data=f"categories:pick:{page-1}"))
    if len(catalog) > page*8+8:
        nav.append(InlineKeyboardButton("→", callback_data=f"categories:pick:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("Отмена", callback_data="flow:cancel")])
    await query.answer()
    await query.edit_message_text("Выберите товар или введите правила текстом." + ("\nТоваров пока нет. Можно задать правила заранее, до первых заказов." if not catalog else "\nПри наличии артикула используем его, иначе — точное название."), reply_markup=InlineKeyboardMarkup(rows))
    return CATEGORY_RULES


async def receive_category_rules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not has_permission(update, context, "settings"):
        await deny(update)
        return ConversationHandler.END
    draft = context.user_data.get("category_draft")
    if not draft or "label" not in draft:
        await cancel(update, context)
        return ConversationHandler.END
    query = update.callback_query
    try:
        if query:
            index = int(str(query.data).rsplit(":", 1)[1])
            product = context.user_data.get("category_catalog", [])[index]
            rules = [{"kind": "sku" if product["sku"] else "name", "value": product["sku"] or product["product_name"]}]
        else:
            raw = str(update.effective_message.text or "").strip()
            rules = draft["entry"]["rules"] if raw == "-" and draft["entry"] else parse_rules(raw)
        key = db(context).save_sales_category(draft["label"], rules, draft["key"])
        text, keyboard = sales_category_card(db(context), key)
    except (ValueError, IndexError) as error:
        if query:
            await query.answer("Товар недоступен, откройте список заново.", show_alert=True)
        else:
            await update.effective_message.reply_text(str(error))
        return CATEGORY_RULES
    context.user_data.pop("category_draft", None)
    context.user_data.pop("category_catalog", None)
    if query:
        await query.answer("Сохранено")
        await query.edit_message_text("✅ Категория сохранена.\n\n" + text, reply_markup=keyboard)
    else:
        await update.effective_message.reply_text("✅ Категория сохранена.\n\n" + text, reply_markup=keyboard)
    return ConversationHandler.END


async def retired_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message:
        await update.effective_message.reply_text("Изменения заказов больше не выполняются через бота. Откройте /menu для продаж, статистики и промокодов.")


async def retired_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Old messages may still contain buttons; none can change an order."""
    if update.callback_query:
        await update.callback_query.answer("Эта функция удалена. Откройте /menu: доступны продажи, статистика и промокоды.", show_alert=True)


def operations_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔎 Найти заказ", callback_data="orders:browse")],
        [InlineKeyboardButton("📋 Заказы по статусам", callback_data="orders:statuses")],
        [InlineKeyboardButton("📥 Выгрузить заказы CSV", callback_data="export:csv:menu")],
        [InlineKeyboardButton(MENU_HOME, callback_data="menu:main")],
    ])


async def operations_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not has_permission(update, context, "operations"):
        await deny(update)
        return
    await query.answer()
    await query.edit_message_text("🔎 Просмотр заказов:", reply_markup=operations_keyboard())


def revenue_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("☀️ Выручка за сегодня", callback_data="revenue:period:today")],
        [InlineKeyboardButton("💰 Выручка за вчера", callback_data="revenue:period:yesterday"),
         InlineKeyboardButton("За 3 дня", callback_data="revenue:period:3days")],
        [InlineKeyboardButton("За неделю", callback_data="revenue:period:week"),
         InlineKeyboardButton("За месяц", callback_data="revenue:period:month")],
        [InlineKeyboardButton("📅 Выбрать даты", callback_data="revenue:dates")],
        [InlineKeyboardButton("← К товарам", callback_data="menu:orders")],
        [InlineKeyboardButton(MENU_HOME, callback_data="menu:main")],
    ])


def revenue_report_text(context: ContextTypes.DEFAULT_TYPE, start_at: int, end_at: int, label: str) -> str:
    database = db(context)
    stats = database.sales_summary("all", start_at, end_at)
    orders = database.order_period_summary(start_at, end_at)
    start = datetime.fromtimestamp(start_at, MOSCOW_TZ)
    last = datetime.fromtimestamp(end_at, MOSCOW_TZ) - timedelta(days=1)
    if label == "today":
        now = datetime.now(MOSCOW_TZ)
        title = f"☀️ Дневная выручка — {now:%d.%m.%Y}"
        period = f"Период: 00:00–{now:%H:%M} МСК"
    else:
        title = f"💰 Выручка: {label}"
        period = f"Период: {start:%d.%m.%Y}–{last:%d.%m.%Y} МСК"
    lines = [title, period, "", f"Заказов с продажами: {stats['orders_count']}",
             f"Всего: {stats['items_quantity']} шт. · {money(stats['revenue_kopecks'])}",
             "", "По типам заказанных вещей:"]
    breakdown = database.sales_model_breakdown(start_at, end_at)
    lines.extend(f"• {row['model'][:80]} — {row['quantity']} шт. · {money(row['revenue_kopecks'])}" for row in breakdown[:25])
    if not breakdown:
        lines.append("Продаж за этот период пока нет.")
    if len(breakdown) > 25:
        lines.append(f"Ещё моделей: {len(breakdown) - 25}. Подробные отчёты — в товарах.")
    lines.extend(["", f"Всего заказов за период (все статусы): {orders['orders_count']}",
                  f"Полная сумма заказов, включая доставку: {money(orders['orders_amount'])}",
                  "", "Товарная выручка учитывает скидки, без доставки. Каждая проданная вещь учитывается один раз."])
    if label == "today":
        lines.append("Новый дневной отчёт начинается в 00:00 МСК.")
    return "\n".join(lines)[:3900]


async def revenue_period(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not has_permission(update, context, "orders"):
        await deny(update)
        return
    period = str(query.data).rsplit(":", 1)[-1]
    if period == "orders":  # A button from the former analytics menu.
        period = "today"
    try:
        start_at, end_at, _ = sales_period(period)
        label = {"today": "today", "yesterday": "вчера", "3days": "последние 3 дня",
                 "week": "последние 7 дней", "month": "последние 30 дней"}[period]
        text = revenue_report_text(context, start_at, end_at, label)
    except (KeyError, ValueError, TypeError):
        await query.answer("Неизвестный период", show_alert=True)
        return
    await query.answer()
    await query.edit_message_text(text, reply_markup=revenue_keyboard())


async def begin_revenue_dates(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not has_permission(update, context, "orders"):
        await deny(update)
        return ConversationHandler.END
    await query.answer()
    await query.edit_message_text(
        "Введите дату или период в формате ДД.ММ.ГГГГ (МСК):\n14.10.2026\nили\n01.10.2026 — 14.10.2026",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Отмена", callback_data="flow:cancel")]]),
    )
    return REVENUE_DATE_RANGE


async def receive_revenue_dates(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.effective_message
    if not message or not has_permission(update, context, "orders"):
        await deny(update)
        return ConversationHandler.END
    values = re.findall(r"\d{2}\.\d{2}\.\d{4}", str(message.text or ""))
    try:
        if len(values) not in (1, 2):
            raise ValueError("Введите одну или две даты в формате ДД.ММ.ГГГГ.")
        start = datetime.strptime(values[0], "%d.%m.%Y").replace(tzinfo=MOSCOW_TZ)
        last = datetime.strptime(values[-1], "%d.%m.%Y").replace(tzinfo=MOSCOW_TZ)
        if last < start or (last - start).days > 3660:
            raise ValueError("Проверьте порядок дат; максимальный период — 10 лет.")
        end = last + timedelta(days=1)
        text = revenue_report_text(context, int(start.timestamp()), int(end.timestamp()), "выбранные даты")
    except ValueError as error:
        await message.reply_text(str(error))
        return REVENUE_DATE_RANGE
    await message.reply_text(text, reply_markup=revenue_keyboard())
    return ConversationHandler.END


PROMO_STAT_MONTHS = (
    'январь', 'февраль', 'март', 'апрель', 'май', 'июнь',
    'июль', 'август', 'сентябрь', 'октябрь', 'ноябрь', 'декабрь',
)


def promo_statistics_period(period: str):
    today = datetime.now(MOSCOW_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    if period == 'all':
        return None, None, 'За всё время'
    if period == 'today':
        start, end, label = today, today + timedelta(days=1), f'Сегодня · {today:%d.%m.%Y}'
    elif period == 'yesterday':
        start, end = today - timedelta(days=1), today
        label = f'Вчера · {start:%d.%m.%Y}'
    elif period == 'month':
        start = today.replace(day=1)
        end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
        label = f'Итого за {PROMO_STAT_MONTHS[start.month-1]} {start.year}'
    elif re.fullmatch(r'\d{8}(?:-\d{8})?', period):
        values = period.split('-')
        start = datetime.strptime(values[0], '%Y%m%d').replace(tzinfo=MOSCOW_TZ)
        last = datetime.strptime(values[-1], '%Y%m%d').replace(tzinfo=MOSCOW_TZ)
        if last < start or (last - start).days > 3660:
            raise ValueError('Проверьте порядок дат; максимальный период — 10 лет.')
        end = last + timedelta(days=1)
        label = f'{start:%d.%m.%Y}' if last == start else f'{start:%d.%m.%Y} — {last:%d.%m.%Y}'
    else:
        raise ValueError('Неизвестный период статистики.')
    return int(start.timestamp()), int(end.timestamp()), label


def promo_statistics_code(database: Database, scope: str) -> Optional[str]:
    if scope == 'a':
        return None
    if re.fullmatch(r'p\d+', scope):
        promo = database.get_promocode(int(scope[1:]))
        if promo:
            return str(promo['code'])
    if re.fullmatch(r'o\d+', scope):
        with database.connect() as connection:
            row = connection.execute('SELECT promo_code FROM orders WHERE id=?', (int(scope[1:]),)).fetchone()
        if row and str(row['promo_code'] or '').strip():
            return str(row['promo_code']).strip().upper()
    raise ValueError('Промокод не найден. Откройте статистику заново.')


def promo_model_suffix(model: str) -> str:
    if model == 'all':
        return ''
    if not re.fullmatch(r'm\d+|other', model):
        raise ValueError('Неизвестная модель.')
    return ':' + model


def promo_statistics_keyboard(scope: str, period: str, page: int, pages: int, model: str = 'all') -> InlineKeyboardMarkup:
    now = datetime.now(MOSCOW_TZ)
    suffix = promo_model_suffix(model)
    buttons = [[InlineKeyboardButton('🔄 Обновить', callback_data=f'ps:r:{scope}:{period}:{page}{suffix}')]]
    navigation = []
    if page:
        navigation.append(InlineKeyboardButton('←', callback_data=f'ps:r:{scope}:{period}:{page-1}{suffix}'))
    if page + 1 < pages:
        navigation.append(InlineKeyboardButton('→', callback_data=f'ps:r:{scope}:{period}:{page+1}{suffix}'))
    if navigation:
        buttons.append(navigation)
    buttons.extend([
        [InlineKeyboardButton('👕 Выбрать модель', callback_data=f'ps:models:{scope}:{period}:0{suffix}')],
        [InlineKeyboardButton('🎟 Выбрать промокод', callback_data=f'ps:codes:{scope}:{period}:0{suffix}')],
        [InlineKeyboardButton('Сегодня', callback_data=f'ps:r:{scope}:today:0{suffix}'),
         InlineKeyboardButton('Предыдущие дни', callback_data=f'ps:days:{scope}:0{suffix}')],
        [InlineKeyboardButton(f'Итого за {PROMO_STAT_MONTHS[now.month-1]}', callback_data=f'ps:r:{scope}:month:0{suffix}')],
        [InlineKeyboardButton('За всё время', callback_data=f'ps:r:{scope}:all:0{suffix}')],
        [InlineKeyboardButton('📅 Выбрать дату / период', callback_data=f'ps:dates:{scope}{suffix}')],
    ])
    if model != 'all':
        buttons.append([InlineKeyboardButton('Все модели', callback_data=f'ps:r:{scope}:{period}:0')])
    if scope.startswith('p'):
        buttons.append([InlineKeyboardButton('← К промокоду', callback_data=f'promo:view:{scope[1:]}')])
    buttons.extend([
        [InlineKeyboardButton('← К промокодам', callback_data='menu:promocodes')],
        [InlineKeyboardButton(MENU_HOME, callback_data='menu:main')],
    ])
    return InlineKeyboardMarkup(buttons)


def promo_statistics_report(context: ContextTypes.DEFAULT_TYPE, scope: str, period: str, page: int = 0, model: str = 'all'):
    database = db(context)
    code = promo_statistics_code(database, scope)
    start_at, end_at, label = promo_statistics_period(period)
    promo_model_suffix(model)
    report = database.promocode_sales_report(start_at, end_at, code, model=None if model == 'all' else model)
    page_size = 12
    pages = max(1, (len(report['rows']) + page_size - 1) // page_size)
    page = min(max(0, page), pages - 1)
    now = datetime.now(MOSCOW_TZ)
    title = f'🎟 Промокод {code}' if code is not None else '🎟 Статистика промокодов'
    lines = [title, label, f'По полученным заказам · {now:%d.%m.%Y %H:%M} МСК']
    if model != 'all':
        lines.append('Модель: ' + report['model_label'])
    lines.extend(['', 'Промокод — покупателей — заказов — вещей — стоимость вещей'
                  if model != 'all' else 'Промокод — покупателей — заказов — сумма заказов'])
    for row in report['rows'][page*page_size:(page+1)*page_size]:
        name = row['code'] if len(row['code']) <= 40 else row['code'][:37] + '…'
        quantity = f"{row['items_quantity']} шт. — " if model != 'all' else ''
        lines.append(f"{name} — {row['buyers_count']} чел. — {row['orders_count']} — {quantity}{money(row['amount_kopecks'])}")
    if not report['rows']:
        lines.append('Оплаченных заказов с промокодами за этот период нет.')
    lines.extend(['', f"Всего покупателей: {report['buyers_count']}",
                  f"Всего использований (заказов): {report['orders_count']} · разных кодов: {report['codes_count']}"])
    if model != 'all':
        lines.extend([f"Всего вещей: {report['items_quantity']} шт.",
                      f"Стоимость вещей: {money(report['amount_kopecks'])}"])
        if report['first_at'] is not None:
            first = datetime.fromtimestamp(report['first_at'], MOSCOW_TZ)
            lines.append(f'Первая покупка в выбранном периоде: {first:%d.%m.%Y %H:%M} МСК')
        lines.append('Только активные оплаченные позиции, без отмен, полных возвратов и бесплатных подарков. '
                     'Стоимость по позициям CRM после пропорционального распределения скидки, без доставки.')
    else:
        lines.extend([f"Сумма заказов: {money(report['amount_kopecks'])}",
                      'Только оплаченные продажи, без отмен и полных возвратов. Сумма после скидки, с доставкой.'])
    lines.append('Покупатели определяются по телефону, email или Telegram. Даты — по времени заказа в CRM (МСК).')
    if report['orders_without_contacts']:
        lines.append(f"Без контактов: {report['orders_without_contacts']} заказов; каждый посчитан отдельно.")
    if sum(row['buyers_count'] for row in report['rows']) > report['buyers_count']:
        lines.append('В общем итоге покупатель учитывается один раз, даже если использовал разные коды.')
    if pages > 1:
        lines.append(f'Страница {page+1} из {pages}. Итоги — по всем кодам периода.')
    return '\n'.join(lines), promo_statistics_keyboard(scope, period, page, pages, model)


async def promo_statistics_picker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not has_permission(update, context, 'promocodes'):
        await deny(update)
        return
    try:
        _, action, scope, period, raw_page, *tail = str(query.data).split(':')
        model = tail[0] if tail else 'all'
        suffix = promo_model_suffix(model)
        database = db(context)
        code = promo_statistics_code(database, scope)
        start_at, end_at, label = promo_statistics_period(period)
        page = max(0, int(raw_page))
        page_size = 8
        buttons = []
        if action == 'models':
            report = database.promocode_sales_report(start_at, end_at, code, model='all')
            entries = report['models']
            page = min(page, max(0, (len(entries)-1)//page_size))
            chunk = entries[page*page_size:(page+1)*page_size]
            title = f'👕 Модели · {code or "все промокоды"}\n{label}\n\nВыберите модель:'
            buttons.append([InlineKeyboardButton('Все модели', callback_data=f'ps:r:{scope}:{period}:0')])
            for entry in chunk:
                text = f"{entry['label'][:45]} · {entry['items_quantity']} шт. · {money(entry['amount_kopecks'])}"
                buttons.append([InlineKeyboardButton(text, callback_data=f"ps:r:{scope}:{period}:0:{entry['key']}")])
            more = (page+1)*page_size < len(entries)
            if not entries:
                title += '\nОплаченных товарных позиций за этот период нет.'
        elif action == 'codes':
            entries = database.promocode_statistics_codes(page_size+1, page*page_size)
            title = '🎟 Выберите промокод. Выбранные модель и период сохранятся.'
            buttons.append([InlineKeyboardButton('Все промокоды', callback_data=f'ps:r:a:{period}:0{suffix}')])
            for entry in entries[:page_size]:
                buttons.append([InlineKeyboardButton(entry['code'][:55], callback_data=f"ps:r:o{entry['order_id']}:{period}:0{suffix}")])
            more = len(entries) > page_size
        else:
            raise ValueError('Неизвестный раздел статистики.')
        navigation = []
        if page:
            navigation.append(InlineKeyboardButton('←', callback_data=f'ps:{action}:{scope}:{period}:{page-1}{suffix}'))
        if more:
            navigation.append(InlineKeyboardButton('→', callback_data=f'ps:{action}:{scope}:{period}:{page+1}{suffix}'))
        if navigation:
            buttons.append(navigation)
        buttons.extend([[InlineKeyboardButton('← К статистике', callback_data=f'ps:r:{scope}:{period}:0{suffix}')],
                        [InlineKeyboardButton(MENU_HOME, callback_data='menu:main')]])
    except ValueError as error:
        await query.answer(str(error), show_alert=True)
        return
    await query.answer()
    await query.edit_message_text(title, reply_markup=InlineKeyboardMarkup(buttons))


async def show_promo_statistics(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not has_permission(update, context, 'promocodes'):
        await deny(update)
        return
    try:
        _, _, scope, period, page, *tail = str(query.data).split(':')
        text, keyboard = promo_statistics_report(context, scope, period, int(page), tail[0] if tail else 'all')
    except ValueError as error:
        await query.answer(str(error), show_alert=True)
        return
    await query.answer()
    try:
        await query.edit_message_text(text, reply_markup=keyboard)
    except BadRequest as error:
        if 'message is not modified' not in str(error).lower():
            raise


async def promo_statistics_previous_days(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not has_permission(update, context, 'promocodes'):
        await deny(update)
        return
    _, _, scope, raw_offset, *tail = str(query.data).split(':')
    try:
        promo_statistics_code(db(context), scope)
        model = tail[0] if tail else 'all'
        suffix = promo_model_suffix(model)
    except ValueError as error:
        await query.answer(str(error), show_alert=True)
        return
    offset = min(3650, max(0, int(raw_offset)))
    today = datetime.now(MOSCOW_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    buttons = []
    for days in range(offset+1, offset+8):
        day = today - timedelta(days=days)
        buttons.append([InlineKeyboardButton(f'{day:%d.%m.%Y}', callback_data=f'ps:r:{scope}:{day:%Y%m%d}:0{suffix}')])
    navigation = []
    if offset:
        navigation.append(InlineKeyboardButton('← Новее', callback_data=f'ps:days:{scope}:{max(0, offset-7)}{suffix}'))
    if offset < 3650:
        navigation.append(InlineKeyboardButton('Старше →', callback_data=f'ps:days:{scope}:{offset+7}{suffix}'))
    buttons.append(navigation)
    buttons.extend([
        [InlineKeyboardButton('📅 Другая дата / период', callback_data=f'ps:dates:{scope}{suffix}')],
        [InlineKeyboardButton('← К статистике', callback_data=f'ps:r:{scope}:today:0{suffix}')],
    ])
    await query.answer()
    await query.edit_message_text('📅 Выберите день статистики промокодов:', reply_markup=InlineKeyboardMarkup(buttons))


async def navigate_promo_statistics(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop('promo_stats_scope', None)
    context.user_data.pop('promo_stats_model', None)
    if str(update.callback_query.data).startswith('ps:days:'):
        await promo_statistics_previous_days(update, context)
    elif str(update.callback_query.data).startswith(('ps:models:', 'ps:codes:')):
        await promo_statistics_picker(update, context)
    else:
        await show_promo_statistics(update, context)
    return ConversationHandler.END


async def begin_promo_statistics_dates(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not has_permission(update, context, 'promocodes'):
        await deny(update)
        return ConversationHandler.END
    _, _, scope, *tail = str(query.data).split(':')
    try:
        promo_statistics_code(db(context), scope)
        model = tail[0] if tail else 'all'
        promo_model_suffix(model)
    except ValueError as error:
        await query.answer(str(error), show_alert=True)
        return ConversationHandler.END
    context.user_data['promo_stats_scope'] = scope
    context.user_data['promo_stats_model'] = model
    await query.answer()
    await query.edit_message_text(
        'Введите дату или период:\n19.10.2026\nили\n01.10.2026 — 19.10.2026\n\nПоследняя дата включается. Время — МСК.',
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton('Отмена', callback_data='flow:cancel')]]),
    )
    return PROMO_STATS_DATES


async def receive_promo_statistics_dates(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.effective_message
    if not message or not has_permission(update, context, 'promocodes'):
        await deny(update)
        return ConversationHandler.END
    match = re.fullmatch(r'(\d{2}\.\d{2}\.\d{4})(?:\s*[—–-]\s*(\d{2}\.\d{2}\.\d{4}))?', str(message.text or '').strip())
    if not match:
        await message.reply_text('Введите дату ДД.ММ.ГГГГ или период ДД.ММ.ГГГГ — ДД.ММ.ГГГГ.')
        return PROMO_STATS_DATES
    try:
        first = datetime.strptime(match.group(1), '%d.%m.%Y')
        last = datetime.strptime(match.group(2) or match.group(1), '%d.%m.%Y')
        period = first.strftime('%Y%m%d') + ('-' + last.strftime('%Y%m%d') if last != first else '')
        scope = context.user_data.get('promo_stats_scope', 'a')
        text, keyboard = promo_statistics_report(context, scope, period, model=context.user_data.get('promo_stats_model', 'all'))
    except (ValueError, OverflowError) as error:
        await message.reply_text(str(error))
        return PROMO_STATS_DATES
    context.user_data.pop('promo_stats_scope', None)
    context.user_data.pop('promo_stats_model', None)
    await message.reply_text(text, reply_markup=keyboard)
    return ConversationHandler.END


def register_handlers(application: Application) -> None:
    readonly_flow = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(begin_readonly_search, pattern=r"^(orders:browse|op:view)$")
        ],
        states={
            READONLY_SEARCH: [
                persistent_menu_handler(),
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND, receive_readonly_search
                ),
            ]
        },
        fallbacks=[
            persistent_menu_handler(),
            CallbackQueryHandler(
                finish_inline_navigation, pattern=r"^menu:(main|orders|operations|analytics|settings|promocodes)$"
            ),
            CallbackQueryHandler(cancel, pattern=r"^flow:cancel$"),
            CommandHandler("cancel", cancel),
        ],
    )
    status_search_flow = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(
                begin_status_search, pattern=r"^status:search:[a-z_]+$"
            )
        ],
        states={
            STATUS_SEARCH_QUERY: [
                persistent_menu_handler(),
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND, receive_status_search
                ),
            ]
        },
        fallbacks=[
            persistent_menu_handler(),
            CallbackQueryHandler(
                finish_inline_navigation,
                pattern=r"^(menu:(main|orders)|status:list:[a-z_]+:\d+)$",
            ),
            CallbackQueryHandler(cancel, pattern=r"^flow:cancel$"),
            CommandHandler("cancel", cancel),
        ],
    )
    verify_orders_flow = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(
                begin_verify_orders, pattern=r"^settings:verify_orders$"
            )
        ],
        states={
            VERIFY_ORDERS_FILE: [
                persistent_menu_handler(),
                MessageHandler(filters.Document.ALL, receive_verify_orders),
                MessageHandler(filters.ALL & ~filters.COMMAND, receive_verify_orders),
            ]
        },
        fallbacks=[
            persistent_menu_handler(),
            CallbackQueryHandler(
                finish_inline_navigation, pattern=r"^menu:(main|settings|promocodes)$"
            ),
            CallbackQueryHandler(cancel, pattern=r"^flow:cancel$"),
            CommandHandler("cancel", cancel),
        ],
    )
    promocode_flow = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(begin_promocode_create, pattern=r"^promo:add$")
        ],
        states={
            PROMO_CODE: [
                persistent_menu_handler(),
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND, receive_promocode_code
                ),
            ],
            PROMO_TYPE: [
                CallbackQueryHandler(
                    choose_promocode_type, pattern=r"^promo:type:(percent|fixed)$"
                )
            ],
            PROMO_VALUE: [
                persistent_menu_handler(),
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND, receive_promocode_value
                ),
            ],
            PROMO_LIMIT: [
                persistent_menu_handler(),
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND, receive_promocode_limit
                ),
            ],
            PROMO_EXPIRY: [
                persistent_menu_handler(),
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND, receive_promocode_expiry
                ),
            ],
            PROMO_DESCRIPTION: [
                persistent_menu_handler(),
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND, receive_promocode_description
                ),
            ],
            PROMO_CONFIRM: [
                CallbackQueryHandler(save_promocode, pattern=r"^promo:save$")
            ],
        },
        fallbacks=[
            persistent_menu_handler(),
            CallbackQueryHandler(
                finish_inline_navigation, pattern=r"^menu:(main|settings)$"
            ),
            CallbackQueryHandler(cancel, pattern=r"^flow:cancel$"),
            CommandHandler("cancel", cancel),
        ],
    )
    access_flow = ConversationHandler(
        allow_reentry=True,
        entry_points=[CallbackQueryHandler(begin_access, pattern=r"^settings:access$")],
        states={
            ACCESS_ID: [
                persistent_menu_handler(),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_access_id),
                CallbackQueryHandler(access_callback, pattern=r"^acl:(view|owner|delete|confirm):[1-9]\d*$|^acl:set:[1-9]\d*:[a-z_]+:[01]$|^acl:role:[1-9]\d*:(admin|manager|analyst|viewer)$"),
            ]
        },
        fallbacks=[
            CallbackQueryHandler(staff_list, pattern=r"^(settings:staff|acl:list:\d+)$"),
            persistent_menu_handler(),
            CallbackQueryHandler(cancel, pattern=r"^flow:cancel$"),
            CommandHandler("cancel", cancel),
        ],
    )
    category_flow = ConversationHandler(
        entry_points=[CallbackQueryHandler(begin_sales_category, pattern=r"^categories:(add|edit:[a-z0-9]+)$")],
        states={
            CATEGORY_NAME: [persistent_menu_handler(), MessageHandler(filters.TEXT & ~filters.COMMAND, receive_category_name)],
            CATEGORY_RULES: [
                persistent_menu_handler(),
                CallbackQueryHandler(pick_category_product, pattern=r"^categories:pick:\d+$"),
                CallbackQueryHandler(receive_category_rules, pattern=r"^categories:product:\d+$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_category_rules),
            ],
        },
        fallbacks=[persistent_menu_handler(), CallbackQueryHandler(cancel, pattern=r"^flow:cancel$"),
                   CallbackQueryHandler(finish_inline_navigation, pattern=r"^menu:(main|orders|operations|analytics|settings|promocodes)$"),
                   CommandHandler("cancel", cancel)],
    )
    sales_dates_flow = ConversationHandler(
        entry_points=[CallbackQueryHandler(begin_sales_dates, pattern=r"^sales:dates:[a-z0-9]+$")],
        states={SALES_DATE_RANGE: [persistent_menu_handler(), MessageHandler(filters.TEXT & ~filters.COMMAND, receive_sales_dates)]},
        fallbacks=[persistent_menu_handler(), CallbackQueryHandler(cancel, pattern=r"^flow:cancel$"),
                   CallbackQueryHandler(finish_inline_navigation, pattern=r"^menu:(main|orders|operations|analytics|settings|promocodes)$"),
                   CommandHandler("cancel", cancel)],
    )
    revenue_dates_flow = ConversationHandler(
        entry_points=[CallbackQueryHandler(begin_revenue_dates, pattern=r"^(revenue:dates|analytics:orders:custom)$")],
        states={REVENUE_DATE_RANGE: [persistent_menu_handler(), MessageHandler(filters.TEXT & ~filters.COMMAND, receive_revenue_dates)]},
        fallbacks=[persistent_menu_handler(), CallbackQueryHandler(cancel, pattern=r"^flow:cancel$"),
                   CallbackQueryHandler(finish_inline_navigation, pattern=r"^menu:(main|orders|operations|analytics|settings|promocodes)$"),
                   CommandHandler("cancel", cancel)],
    )
    application.add_handler(revenue_dates_flow)
    application.add_handler(CallbackQueryHandler(revenue_period, pattern=r"^(revenue:period:(today|yesterday|3days|week|month)|analytics:orders(?::(?:yesterday|3days|week|month))?)$"))
    application.add_handler(CallbackQueryHandler(operations_menu, pattern=r"^menu:operations$"))
    application.add_handler(category_flow)
    application.add_handler(sales_dates_flow)
    application.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(begin_product_search, pattern=r"^products:search$")],
        states={PRODUCT_SEARCH: [
            persistent_menu_handler(),
            MessageHandler(filters.TEXT & ~filters.COMMAND, receive_product_search),
        ]},
        fallbacks=[
            persistent_menu_handler(),
            CallbackQueryHandler(cancel, pattern=r"^flow:cancel$"),
            CallbackQueryHandler(finish_inline_navigation, pattern=r"^menu:(main|orders|operations|analytics|settings|promocodes)$"),
            CommandHandler("cancel", cancel),
        ],
    ))
    application.add_handler(CallbackQueryHandler(sales_products_menu, pattern=r"^products:(open|page:\d+)$"))
    application.add_handler(CallbackQueryHandler(sales_categories_menu, pattern=r"^categories:list:\d+$"))
    application.add_handler(CallbackQueryHandler(view_sales_category, pattern=r"^categories:(view|toggle):[a-z0-9]+$"))
    application.add_handler(CallbackQueryHandler(sales_report_period, pattern=r"^sales:period:[a-z0-9]+:(all|today|yesterday|3days|week|month)$"))
    application.add_handler(CallbackQueryHandler(sales_orders, pattern=r"^sales:orders:[a-z0-9]+:\d+:\d+:\d+$"))

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("menu", start))
    application.add_handler(CommandHandler("id", show_id))
    application.add_handler(
        CommandHandler("bind_promocodes_chat", bind_promocodes_chat)
    )
    promo_statistics_dates_flow = ConversationHandler(
        entry_points=[CallbackQueryHandler(begin_promo_statistics_dates, pattern=r"^ps:dates:(a|[po]\d+)(?::(?:m\d+|other))?$")],
        allow_reentry=True,
        states={PROMO_STATS_DATES: [persistent_menu_handler(), MessageHandler(filters.TEXT & ~filters.COMMAND, receive_promo_statistics_dates)]},
        fallbacks=[persistent_menu_handler(), CallbackQueryHandler(cancel, pattern=r"^flow:cancel$"),
                   CallbackQueryHandler(navigate_promo_statistics, pattern=r"^ps:(r|days|models|codes):"),
                   CallbackQueryHandler(finish_inline_navigation, pattern=r"^menu:(main|orders|operations|analytics|settings|promocodes)$"),
                   CommandHandler("cancel", cancel)],
    )
    application.add_handler(promo_statistics_dates_flow)
    application.add_handler(CallbackQueryHandler(show_promo_statistics, pattern=r"^ps:r:(a|[po]\d+):(today|yesterday|month|all|\d{8}(?:-\d{8})?):\d+(?::(?:m\d+|other))?$"))
    application.add_handler(CallbackQueryHandler(promo_statistics_previous_days, pattern=r"^ps:days:(a|[po]\d+):\d+(?::(?:m\d+|other))?$"))
    application.add_handler(CallbackQueryHandler(promo_statistics_picker, pattern=r"^ps:(models|codes):(a|[po]\d+):(today|yesterday|month|all|\d{8}(?:-\d{8})?):\d+(?::(?:m\d+|other))?$"))

    application.add_handler(readonly_flow)
    application.add_handler(status_search_flow)
    application.add_handler(verify_orders_flow)
    application.add_handler(promocode_flow)
    application.add_handler(access_flow)
    application.add_handler(CallbackQueryHandler(access_callback, pattern=r"^acl:(view|owner|delete|confirm):[1-9]\d*$|^acl:set:[1-9]\d*:[a-z_]+:[01]$|^acl:role:[1-9]\d*:(admin|manager|analyst|viewer)$"))
    application.add_handler(CallbackQueryHandler(staff_list, pattern=r"^acl:list:\d+$"))
    # Conversations must see the command before the standalone handler so they
    # close their state instead of consuming the employee's next message.
    for handlers in application.handlers.values():
        for handler in handlers:
            if isinstance(handler, ConversationHandler):
                handler.fallbacks.append(CommandHandler("promocodes", promocodes_menu))
                handler.fallbacks.append(CommandHandler("check_order", begin_check_order))
    application.add_handler(CommandHandler("promocodes", promocodes_menu))
    application.add_handler(CommandHandler("check_order", begin_check_order))
    application.add_handler(persistent_menu_handler())
    application.add_handler(CallbackQueryHandler(menu_main, pattern=r"^menu:main$"))
    application.add_handler(CallbackQueryHandler(orders_summary, pattern=r"^menu:orders$"))
    application.add_handler(
        CallbackQueryHandler(
            sales_report, pattern=r"^orders:sales:[a-z0-9]+$"
        )
    )
    application.add_handler(
        CallbackQueryHandler(csv_export_menu, pattern=r"^export:csv:menu$")
    )
    application.add_handler(
        CallbackQueryHandler(
            export_orders_to_csv,
            pattern=r"^export:csv:(all|status:[a-z_]+)$",
        )
    )
    application.add_handler(CallbackQueryHandler(analytics_menu, pattern=r"^menu:analytics$"))
    application.add_handler(
        CallbackQueryHandler(social_network_stats, pattern=r"^analytics:social$")
    )
    application.add_handler(CallbackQueryHandler(settings_menu, pattern=r"^menu:settings$"))
    application.add_handler(
        CallbackQueryHandler(promocodes_menu, pattern=r"^menu:promocodes$")
    )
    application.add_handler(
        CallbackQueryHandler(promocodes_history, pattern=r"^promo:list:\d+$")
    )
    application.add_handler(
        CallbackQueryHandler(view_promocode, pattern=r"^promo:view:\d+$")
    )
    application.add_handler(
        CallbackQueryHandler(retry_promocode_sync, pattern=r"^promo:retry:\d+$")
    )
    application.add_handler(
        CallbackQueryHandler(begin_promocode_delete, pattern=r"^promo:delete:\d+$")
    )
    application.add_handler(
        CallbackQueryHandler(
            confirm_promocode_delete, pattern=r"^promo:delete_confirm:\d+$"
        )
    )
    application.add_handler(CallbackQueryHandler(integrations_status, pattern=r"^settings:integrations$"))
    application.add_handler(CallbackQueryHandler(staff_list, pattern=r"^settings:staff$"))
    application.add_handler(CallbackQueryHandler(status_filter_menu, pattern=r"^(orders:statuses|op:statuses)$"))
    application.add_handler(
        CallbackQueryHandler(list_status_orders, pattern=r"^status:list:[a-z_]+:\d+$")
    )
    application.add_handler(
        CallbackQueryHandler(view_readonly_order, pattern=r"^order:(readonly|view):\d+$")
    )
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, ingest_promocode_channel),
        group=1,
    )
    application.add_handler(CallbackQueryHandler(retired_action))
    application.add_handler(CommandHandler(["edit", "changes", "bind_changes_chat"], retired_command))
    application.add_error_handler(error_handler)


def main() -> None:
    load_dotenv(PROJECT_DIR / ".env")
    token = os.getenv("BOT_TOKEN", "")
    if not token:
        raise RuntimeError("Не найден BOT_TOKEN в .env")
    admins = parse_admin_ids(os.getenv("ADMIN_IDS", ""))
    if not admins:
        raise RuntimeError("Не заполнен ADMIN_IDS в .env")
    database = Database(Path(os.getenv("CRM_DB_PATH") or str(PROJECT_DIR / "crm.sqlite3")))
    database.initialize()
    daily_backup = PROJECT_DIR / "backups" / f"crm_{datetime.now(MOSCOW_TZ):%Y-%m-%d}.sqlite3"
    if not daily_backup.exists():
        database.backup_to(daily_backup)
    database.ensure_admins(admins)
    from refund_settings import OWNER
    database.ensure_owner(OWNER or min(admins))
    cdek_client = CdekClient(
        os.getenv("CDEK_CLIENT_ID", ""),
        os.getenv("CDEK_CLIENT_SECRET", ""),
        os.getenv("CDEK_API_URL", "https://api.cdek.ru/v2"),
        shipment_creation_enabled=False,
    )
    tilda_promocode_client = TildaPromocodeClient(
        os.getenv("TILDA_EMAIL", ""),
        os.getenv("TILDA_PASSWORD", ""),
        os.getenv("TILDA_PROMOCODES_PROJECT_ID", ""),
        state_path=PROJECT_DIR / ".auth" / "tilda_state.json",
    )
    application = (
        Application.builder()
        .token(token)
        .connect_timeout(20)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(20)
        .get_updates_connect_timeout(20)
        .get_updates_read_timeout(60)
        .get_updates_write_timeout(30)
        .get_updates_pool_timeout(20)
        .post_init(start_background_tasks)
        .post_shutdown(stop_background_tasks)
        .build()
    )
    application.bot_data["database"] = database
    application.bot_data["cdek"] = cdek_client
    application.bot_data["tilda_promocodes"] = tilda_promocode_client
    application.bot_data["admin_ids"] = admins
    application.add_handler(TypeHandler(Update, block_revoked_staff), group=-1000)
    from customer_refunds_ui import register_refunds
    refunds = register_refunds(application, PROJECT_DIR / "customer_refunds.sqlite3", PROJECT_DIR, "",
                               lambda text: bool(re.fullmatch(MENU_PATTERN, text)))
    refunds.service.crm_path = database.path
    from support_order_info import register_order_information
    register_order_information(application, refunds.service, database)
    from order_check import register_order_check
    register_order_check(application, refunds.service, database,
                         lambda text: bool(re.fullmatch(MENU_PATTERN, text)))
    from gift_scarves import register_gifts
    register_gifts(application, refunds.service, database, PROJECT_DIR / "gift_scarves.sqlite3")
    register_handlers(application)
    logger.info("Shop CRM CRM Bot запущен")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
