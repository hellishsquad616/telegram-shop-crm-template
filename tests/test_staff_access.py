import sqlite3
import tempfile
import unittest
import warnings
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram import Update
from telegram.ext import Application, ApplicationHandlerStop, ConversationHandler, TypeHandler

import bot
import staff_access as access
from database import ALL_PERMISSIONS, Database
from order_check import OrderCheck


class StaffAccessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Database(Path(self.temp.name) / "crm.sqlite3")
        self.database.initialize()
        self.database.ensure_owner(1)
        self.database.set_staff_role(2, "manager", 1)
        self.context = SimpleNamespace(application=SimpleNamespace(bot_data={"database": self.database}), user_data={})

    def update(self, data=None, text="", user=1, chat_type="private"):
        message = SimpleNamespace(text=text, chat=SimpleNamespace(type=chat_type), reply_text=AsyncMock())
        query = SimpleNamespace(data=data, answer=AsyncMock(), edit_message_text=AsyncMock(), message=message) if data else None
        return SimpleNamespace(effective_user=SimpleNamespace(id=user), effective_message=message,
                               message=message if not query else None, callback_query=query)

    def test_granular_flags_and_revocation_survive_initialization_and_admin_bootstrap(self):
        self.database.set_staff_permission(2, "orders", False, 1)
        self.database.set_staff_permission(2, "check_order", True, 1)
        self.database.set_staff_permission(2, "operations", False, 1)
        self.database.initialize()
        self.database.ensure_admins([1, 2])
        self.assertFalse(self.database.has_permission(2, "orders"))
        self.assertTrue(self.database.has_permission(2, "check_order"))
        self.assertFalse(self.database.has_permission(2, "operations"))
        self.database.remove_staff(2, 1)
        self.database.initialize()
        self.database.ensure_admins([1, 2])
        self.assertIsNone(self.database.get_staff(2))
        self.assertTrue(self.database.is_staff_revoked(2))
        self.assertFalse(any(self.database.has_permission(2, key) for key in ALL_PERMISSIONS))
        self.assertNotIn(2, [row["telegram_id"] for row in self.database.list_staff()])

    def test_owner_cannot_be_removed_or_have_permissions_changed(self):
        for role in ["admin", "manager", "blocked", "custom"]:
            with self.assertRaises(ValueError):
                self.database.set_staff_role(1, role, 2)
        with self.assertRaises(ValueError):
            self.database.set_staff_permission(1, "staff", False, 2)
        with self.assertRaises(ValueError):
            self.database.remove_staff(1, 2)
        self.assertTrue(all(self.database.has_permission(1, key) for key in ALL_PERMISSIONS))

    async def test_each_main_button_is_independent(self):
        self.database.set_staff_role(2, "custom", 1)
        for key in access.PERMISSION_LABELS:
            self.database.set_staff_permission(2, key, True, 1)
            person = self.database.get_staff(2)
            labels = [b.text for row in bot.main_reply_keyboard(person).keyboard for b in row]
            inline = [b.callback_data for row in bot.main_menu(person).inline_keyboard for b in row]
            self.assertEqual(len(labels), 2, (key, labels))
            self.assertEqual(len(inline), 1, (key, inline))
            self.database.set_staff_permission(2, key, False, 1)
        self.database.set_staff_permission(2, "check_order", True, 1)
        ui = OrderCheck(SimpleNamespace(), self.database, lambda text: False)
        self.assertTrue(ui.allowed(self.update(user=2)))
        self.database.set_staff_permission(2, "check_order", False, 1)
        self.database.set_staff_permission(2, "orders", True, 1)
        self.assertFalse(ui.allowed(self.update(user=2)))

    async def test_old_operation_buttons_cannot_bypass_restriction(self):
        self.database.set_staff_permission(2, "operations", False, 1)
        cases = [(bot.operations_menu, "menu:operations"),
                 (bot.status_filter_menu, "orders:statuses"),
                 (bot.list_status_orders, "status:list:paid:0"),
                 (bot.begin_status_search, "status:search:paid"),
                 (bot.begin_readonly_search, "orders:browse"),
                 (bot.csv_export_menu, "export:csv:menu"),
                 (bot.export_orders_to_csv, "export:csv:all")]
        for handler, callback in cases:
            update = self.update(data=callback, user=2)
            await handler(update, self.context)
            update.callback_query.answer.assert_awaited_once()
            self.assertTrue(update.callback_query.answer.call_args.kwargs.get("show_alert"), handler.__name__)
            update.callback_query.edit_message_text.assert_not_awaited()
        for handler in [bot.receive_status_search, bot.receive_readonly_search]:
            update = self.update(text="123", user=2)
            self.assertEqual(await handler(update, self.context), ConversationHandler.END)
            self.assertIn("нет прав", update.message.reply_text.call_args.args[0])

    async def test_settings_permission_does_not_grant_staff_management(self):
        self.database.set_staff_permission(2, "settings", True, 1)
        before = self.database.get_staff(2)
        for callback in ["acl:set:2:staff:1", "acl:role:2:admin", "acl:confirm:1"]:
            update = self.update(data=callback, user=2)
            await access.access_callback(update, self.context)
            self.assertTrue(update.callback_query.answer.call_args.kwargs["show_alert"])
        self.assertEqual(self.database.get_staff(2), before)
        settings = bot.settings_keyboard(before)
        self.assertNotIn("settings:staff", [b.callback_data for row in settings.inline_keyboard for b in row])
        result = await access.receive_access_id(self.update(text="3", user=2), self.context)
        self.assertEqual(result, ConversationHandler.END)
        self.assertIsNone(self.database.get_staff(3))

    async def test_add_restore_and_permission_switch_are_explicit_and_idempotent(self):
        self.assertEqual(await access.receive_access_id(self.update(text="3"), self.context), ConversationHandler.END)
        self.assertFalse(any(self.database.has_permission(3, key) for key in ALL_PERMISSIONS))
        for _ in range(2):
            await access.access_callback(self.update(data="acl:set:3:check_order:1"), self.context)
        self.assertTrue(self.database.has_permission(3, "check_order"))
        self.assertFalse(self.database.has_permission(3, "orders"))
        await access.access_callback(self.update(data="acl:confirm:3"), self.context)
        for callback in ["acl:set:3:orders:1", "acl:role:3:admin"]:
            await access.access_callback(self.update(data=callback), self.context)
            self.assertIsNone(self.database.get_staff(3))
        await access.receive_access_id(self.update(text="3"), self.context)
        self.assertFalse(any(self.database.has_permission(3, key) for key in ALL_PERMISSIONS))

    async def test_delete_requires_confirmation_and_keeps_orders(self):
        order_id = self.database.upsert_order({"order_number": "test", "status": "paid"}, [])
        update = self.update(data="acl:delete:2")
        await access.access_callback(update, self.context)
        self.assertIsNotNone(self.database.get_staff(2))
        keyboard = update.callback_query.edit_message_text.call_args.kwargs["reply_markup"]
        self.assertEqual(keyboard.inline_keyboard[0][0].callback_data, "acl:confirm:2")
        await access.access_callback(self.update(data="acl:confirm:2"), self.context)
        self.assertIsNone(self.database.get_staff(2))
        self.assertIsNotNone(self.database.get_order(order_id))
        for callback in ["acl:confirm:1", "acl:set:1:orders:0", "acl:role:1:viewer"]:
            await access.access_callback(self.update(data=callback), self.context)
        self.assertTrue(self.database.has_permission(1, "orders"))

    async def test_revocation_guard_blocks_old_callbacks_commands_and_support_messages(self):
        self.database.remove_staff(2, 1)
        for update in [self.update(data="menu:orders", user=2), self.update(text="/start", user=2),
                       self.update(text="Прошу возврат", user=2, chat_type="supergroup")]:
            self.context.user_data["active_lookup"] = True
            with self.assertRaises(ApplicationHandlerStop):
                await access.block_revoked_staff(update, self.context)
            self.assertFalse(self.context.user_data)
        await access.block_revoked_staff(self.update(user=9, chat_type="supergroup"), self.context)

    async def test_staff_access_rechecked_after_permission_loss_and_not_available_in_groups(self):
        self.database.set_staff_permission(2, "staff", True, 1)
        await access.begin_access(self.update(data="settings:access", user=2), self.context)
        self.database.set_staff_permission(2, "staff", False, 1)
        await access.receive_access_id(self.update(text="3", user=2), self.context)
        self.assertIsNone(self.database.get_staff(3))
        update = self.update(data="acl:set:2:orders:0", chat_type="supergroup")
        await access.access_callback(update, self.context)
        self.assertTrue(self.database.has_permission(2, "orders"))

    async def test_invalid_telegram_ids_do_not_create_records(self):
        for value in ["0", "-5", "abc", str(2**64)]:
            self.assertEqual(await access.receive_access_id(self.update(text=value), self.context), access.ACCESS_ID)
        self.assertEqual(len(self.database.list_staff()), 2)

    async def test_list_exits_add_user_conversation(self):
        await access.begin_access(self.update(data="settings:access"), self.context)
        self.assertEqual(await access.staff_list(self.update(data="acl:list:0"), self.context), ConversationHandler.END)

    def test_handlers_recognize_new_callbacks_before_retired_action(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            application = Application.builder().token("123456:test").build()
            bot.register_handlers(application)
        from telegram.ext import CallbackQueryHandler
        for data in ["acl:view:2", "acl:set:2:orders:0", "acl:role:2:manager", "acl:confirm:2", "acl:list:0"]:
            handler = next(h for h in application.handlers[0] if isinstance(h, CallbackQueryHandler)
                           and (h.pattern is None or h.pattern.match(data)))
            self.assertIn(handler.callback, {access.access_callback, access.staff_list})


class LegacyMigrationTests(unittest.TestCase):
    def test_legacy_users_keep_actual_visible_buttons_but_revoked_users_stay_blocked(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "legacy.sqlite3"
            with sqlite3.connect(path) as c:
                c.execute("CREATE TABLE staff (telegram_id INTEGER PRIMARY KEY, role TEXT, can_orders INTEGER, can_operations INTEGER, can_analytics INTEGER, can_settings INTEGER, can_promocodes INTEGER, can_warehouse INTEGER, is_active INTEGER, added_by INTEGER, created_at INTEGER, updated_at INTEGER)")
                c.execute("INSERT INTO staff VALUES(2,'viewer',1,0,0,0,0,0,1,1,0,0)")
                c.execute("INSERT INTO staff VALUES(3,'blocked',0,0,0,0,0,0,0,1,0,0)")
                c.execute("INSERT INTO staff VALUES(4,'admin',1,1,1,1,1,1,1,1,0,0)")
            db = Database(path)
            db.initialize()
            self.assertTrue(db.has_permission(2, "operations"))
            self.assertTrue(db.has_permission(2, "check_order"))
            self.assertFalse(db.has_permission(2, "staff"))
            self.assertTrue(db.has_permission(4, "staff"))
            db.ensure_admins([3])
            self.assertTrue(db.is_staff_revoked(3))
