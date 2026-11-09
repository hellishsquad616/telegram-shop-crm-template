"""Per-button staff access, with persistent revocation and owner protection."""
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import ApplicationHandlerStop, ConversationHandler


MENU_STAFF = "👥 Права сотрудников"
PERMISSION_LABELS = {
    "orders": "📦 Заказы и продажи",
    "operations": "🔎 Просмотр и поиск заказов, CSV",
    "check_order": "🔎 Проверить заказ в МС",
    "analytics": "📊 Аналитика",
    "promocodes": "🎟 Промокоды",
    "settings": "⚙️ Настройки, интеграции и импорт",
    "staff": MENU_STAFF,
}
ROLE_LABELS = {"admin": "Администратор", "manager": "Менеджер продаж",
               "analyst": "Аналитик", "viewer": "Только заказы", "custom": "Свои права"}
ACCESS_ID = 2  # Existing CRM conversation state; preserve the other state numbers.


def database(context):
    return context.application.bot_data["database"]


async def authorized(update, context):
    user = update.effective_user
    message = update.effective_message
    if (user and message and message.chat.type == "private"
            and database(context).has_permission(user.id, "staff")):
        return True
    if update.callback_query:
        await update.callback_query.answer("Нет права управлять сотрудниками. Раздел доступен в личном чате.", show_alert=True)
    elif message:
        await message.reply_text("Нет права управлять сотрудниками. Раздел доступен в личном чате.")
    return False


async def block_revoked_staff(update, context):
    user = update.effective_user
    if user and database(context).is_staff_revoked(user.id):
        context.user_data.clear()
        if update.callback_query:
            await update.callback_query.answer("Доступ к боту закрыт.", show_alert=True)
        elif update.effective_message:
            await update.effective_message.reply_text("Доступ к боту закрыт.", reply_markup=ReplyKeyboardRemove())
        raise ApplicationHandlerStop


def staff_page(db, page=0):
    people = db.list_staff()
    page = min(max(0, page), max(0, (len(people) - 1) // 8))
    rows = []
    for person in people[page * 8:page * 8 + 8]:
        ident = person["telegram_id"]
        label = "Владелец" if ident == db.protected_owner_id() else ROLE_LABELS.get(person["role"], person["role"])
        count = sum(bool(person.get("can_" + key)) for key in PERMISSION_LABELS)
        rows.append([InlineKeyboardButton(f"{ident} · {label} · {count}/7", callback_data=f"acl:view:{ident}")])
    navigation = []
    if page:
        navigation.append(InlineKeyboardButton("←", callback_data=f"acl:list:{page-1}"))
    if (page + 1) * 8 < len(people):
        navigation.append(InlineKeyboardButton("→", callback_data=f"acl:list:{page+1}"))
    if navigation:
        rows.append(navigation)
    rows.extend([[InlineKeyboardButton("➕ Добавить сотрудника", callback_data="settings:access")],
                 [InlineKeyboardButton("🏠 Главное меню", callback_data="menu:main")]])
    return f"👥 Права сотрудников — {len(people)}\n\nВыберите сотрудника. Страница {page+1}.", InlineKeyboardMarkup(rows)


def staff_card(db, ident):
    person = db.get_staff(ident)
    if not person:
        raise ValueError("Сотрудник удалён или не найден")
    protected = ident == db.protected_owner_id()
    rows = []
    for key, label in PERMISSION_LABELS.items():
        allowed = bool(person.get("can_" + key))
        callback = f"acl:owner:{ident}" if protected else f"acl:set:{ident}:{key}:{int(not allowed)}"
        rows.append([InlineKeyboardButton(("🟢 " if allowed else "🔴 ") + label, callback_data=callback)])
    if not protected:
        rows.extend([
            [InlineKeyboardButton("Профиль: менеджер", callback_data=f"acl:role:{ident}:manager"),
             InlineKeyboardButton("Профиль: аналитик", callback_data=f"acl:role:{ident}:analyst")],
            [InlineKeyboardButton("Все права", callback_data=f"acl:role:{ident}:admin"),
             InlineKeyboardButton("Только заказы", callback_data=f"acl:role:{ident}:viewer")],
            [InlineKeyboardButton("🗑 Удалить из бота", callback_data=f"acl:delete:{ident}")],
        ])
    rows.append([InlineKeyboardButton("← Сотрудники", callback_data="acl:list:0")])
    text = f"👤 Сотрудник {ident}\n\nНажмите на кнопку, чтобы включить или отключить доступ. Права сохраняются сразу."
    text += "\n\nВладелец: права защищены." if protected else ""
    text += "\n\n💳 Проведение возвратов средств доступно только владельцу."
    return text, InlineKeyboardMarkup(rows)


async def staff_list(update, context):
    if not await authorized(update, context):
        return ConversationHandler.END
    context.user_data.pop("access_target", None)
    query = update.callback_query
    page = int(str(query.data).rsplit(":", 1)[1]) if query and str(query.data).startswith("acl:list:") else 0
    text, keyboard = staff_page(database(context), page)
    if query:
        await query.answer()
        await query.edit_message_text(text, reply_markup=keyboard)
    else:
        await update.effective_message.reply_text(text, reply_markup=keyboard)
    return ConversationHandler.END


async def begin_access(update, context):
    if not await authorized(update, context):
        return ConversationHandler.END
    await update.callback_query.answer()
    await update.callback_query.edit_message_text(
        "Введите числовой Telegram ID сотрудника.\nУ нового сотрудника все кнопки будут выключены — включите нужные в его карточке.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("← Сотрудники", callback_data="acl:list:0")]]))
    return ACCESS_ID


async def receive_access_id(update, context):
    if not await authorized(update, context):
        return ConversationHandler.END
    try:
        ident = int(str(update.message.text).strip())
        if not 0 < ident <= 2**63 - 1:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Нужен положительный числовой Telegram ID.")
        return ACCESS_ID
    db = database(context)
    if not db.get_staff(ident):
        db.set_staff_role(ident, "custom", update.effective_user.id)
    text, keyboard = staff_card(db, ident)
    await update.message.reply_text(text, reply_markup=keyboard)
    return ConversationHandler.END


async def access_callback(update, context):
    if not await authorized(update, context):
        return ConversationHandler.END
    query, db = update.callback_query, database(context)
    bits = str(query.data).split(":")
    action, ident = bits[1], int(bits[2])
    try:
        if not db.get_staff(ident):
            raise ValueError("Сотрудник удалён или не найден")
        if action == "owner":
            raise ValueError("Права владельца защищены")
        if action in {"delete", "confirm"} and ident == db.protected_owner_id():
            raise ValueError("Владельца удалить нельзя")
        if action == "delete":
            text = f"Удалить сотрудника {ident}?\n\nДоступ к кнопкам, командам и старым сообщениям бота будет закрыт. История заказов сохранится."
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("Да, удалить и закрыть доступ", callback_data=f"acl:confirm:{ident}")],
                [InlineKeyboardButton("← Отмена", callback_data=f"acl:view:{ident}")]])
        elif action == "confirm":
            db.remove_staff(ident, update.effective_user.id)
            text, keyboard = staff_page(db)
            text = f"✅ Доступ {ident} закрыт.\n\n" + text
        else:
            if action == "set":
                if bits[3] not in PERMISSION_LABELS or bits[4] not in {"0", "1"}:
                    raise ValueError("Неизвестное право")
                db.set_staff_permission(ident, bits[3], bits[4] == "1", update.effective_user.id)
            elif action == "role":
                if bits[3] not in {"admin", "manager", "analyst", "viewer"}:
                    raise ValueError("Неизвестный профиль")
                db.set_staff_role(ident, bits[3], update.effective_user.id)
            text, keyboard = staff_card(db, ident)
        if not db.has_permission(update.effective_user.id, "staff"):
            text, keyboard = "Изменения сохранены. Ваш доступ к управлению сотрудниками закрыт.", None
        # Telegram rejects an identical edit after repeated clicks; the desired
        # permission value in the callback makes repeated writes idempotent.
        await query.answer("Сохранено" if action in {"set", "role", "confirm"} else None)
        from telegram.error import BadRequest
        try:
            await query.edit_message_text(text, reply_markup=keyboard)
        except BadRequest as error:
            if "message is not modified" not in str(error).lower():
                raise
    except ValueError as error:
        await query.answer(str(error), show_alert=True)
    return ConversationHandler.END
