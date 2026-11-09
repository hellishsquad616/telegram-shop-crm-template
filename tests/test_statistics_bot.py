import tempfile
import unittest
import warnings
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch
from datetime import datetime, timedelta
from integrations.tilda_promocodes import TildaPromocodeError

from telegram.ext import Application, CallbackQueryHandler, ConversationHandler, CommandHandler
from telegram import Chat, Message, MessageEntity, Update, User
import bot
from database import Database


class StatisticsBotTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp.name) / 'test.sqlite3')
        self.database.initialize()
        self.database.ensure_admins([1])
        self.database.set_staff_role(2, 'viewer', 1)
        self.context = SimpleNamespace(application=SimpleNamespace(bot_data={'database': self.database}), user_data={})

    def tearDown(self):
        self.temp.cleanup()

    def update(self, user=1, data=None, text=None):
        query = SimpleNamespace(data=data, answer=AsyncMock(), edit_message_text=AsyncMock()) if data else None
        message = SimpleNamespace(text=text, reply_text=AsyncMock())
        if query:
            query.message = message
        return SimpleNamespace(callback_query=query, effective_message=message, effective_user=SimpleNamespace(id=user))

    def test_menus_offer_sales_promocodes_and_readonly_order_operations(self):
        staff = self.database.get_staff(1)
        reply_labels = [b.text for row in bot.main_reply_keyboard(staff).keyboard for b in row]
        inline_data = [b.callback_data for row in bot.main_menu(staff).inline_keyboard for b in row]
        self.assertIn(bot.MENU_PROMOCODES, reply_labels)
        self.assertIn('menu:operations', inline_data)
        self.assertNotIn('menu:warehouse', inline_data)
        operations = [b.callback_data for row in bot.operations_keyboard().inline_keyboard for b in row]
        self.assertIn('orders:browse', operations)
        self.assertFalse(any(d.startswith(('edit:', 'order:edit:', 'op:exchange', 'op:return', 'op:cancel')) for d in operations))
        settings = [b.callback_data for row in bot.settings_keyboard().inline_keyboard for b in row]
        self.assertNotIn('menu:promocodes', settings)
        analytics = [b.callback_data for row in bot.analytics_keyboard().inline_keyboard for b in row]
        self.assertNotIn('analytics:orders', analytics)
        self.assertIn('analytics:social', analytics)
        categories = [b.callback_data for row in bot.orders_keyboard(self.context).inline_keyboard for b in row]
        self.assertIn('orders:sales:windbreaker', categories)
        self.assertIn('orders:sales:ziphoodie', categories)
        self.assertNotIn('orders:sales:longsleeve', categories)
        daily = [b.callback_data for row in bot.sales_report_keyboard('today').inline_keyboard for b in row]
        self.assertIn('revenue:period:yesterday', daily)
        self.assertIn('revenue:period:3days', daily)
        self.assertIn('revenue:dates', daily)
        self.assertNotIn('orders:browse', daily)
        self.assertFalse(any(d.startswith('sales:orders:') for d in daily))
        key = self.database.save_sales_category('Шарфы', [{'kind': 'contains', 'value': 'шарф'}])
        self.assertIn(f'orders:sales:{key}', [b.callback_data for row in bot.orders_keyboard(self.context).inline_keyboard for b in row])

    def test_handlers_keep_employee_promocodes_and_retire_all_order_writes(self):
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            application = Application.builder().token('123456:test').build()
            bot.register_handlers(application)
        def all_handlers(handler):
            yield handler
            if isinstance(handler, ConversationHandler):
                for nested in handler.entry_points + handler.fallbacks + [h for handlers in handler.states.values() for h in handlers]:
                    yield from all_handlers(nested)
        handlers = [nested for group in application.handlers.values() for handler in group for nested in all_handlers(handler)]
        callbacks = {h.callback.__name__ for h in handlers if not isinstance(h, ConversationHandler)}
        self.assertTrue({'begin_promocode_create', 'save_promocode', 'retry_promocode_sync', 'promocodes_history', 'ingest_promocode_channel'} <= callbacks)
        self.assertFalse({'save_manual_edit', 'confirm_bulk_status', 'exchange_confirm', 'return_confirm', 'cancellation_confirm', 'sync_cdek', 'export_cdek_labels', 'ingest_change_message'} & callbacks)
        callback_handlers = [h for h in application.handlers[0] if isinstance(h, CallbackQueryHandler)]
        for old in ['order:edit:1', 'status:set:1:cancelled', 'op:exchange', 'order:cdek_recreate:1', 'status:bulk']:
            match = next(h for h in callback_handlers if h.pattern is None or h.pattern.match(old))
            self.assertEqual(match.callback, bot.retired_action)
        self.assertTrue(self.database.get_staff(1)['can_promocodes'])

    async def test_category_creation_preview_persists_and_viewer_is_denied(self):
        initial_categories = self.database.list_sales_categories()
        denied = self.update(user=2, data='categories:add')
        self.assertEqual(await bot.begin_sales_category(denied, self.context), ConversationHandler.END)
        self.assertEqual(self.database.list_sales_categories(), initial_categories)
        self.assertEqual(await bot.begin_sales_category(self.update(data='categories:add'), self.context), bot.CATEGORY_NAME)
        self.assertEqual(await bot.receive_category_name(self.update(text='Шарфы'), self.context), bot.CATEGORY_RULES)
        self.assertEqual(await bot.receive_category_rules(self.update(text='шарф\nsku:SCARF-001'), self.context), ConversationHandler.END)
        saved = self.database.list_sales_categories()[-1]
        self.assertEqual(saved['label'], 'Шарфы')
        self.assertEqual(saved['rules'][1]['kind'], 'sku')
        self.assertIn('Заказов: 0', bot.sales_category_card(self.database, saved['key'])[0])

    async def test_custom_period_buttons_keep_their_dates_when_new_report_is_opened(self):
        self.database.upsert_order({'order_number': 'october', 'status': 'paid', 'created_at': 1791493200}, [{'product_name': 'Ветровка', 'quantity': 1, 'unit_price_kopecks': 10000}])
        await bot.begin_sales_dates(self.update(data='sales:dates:windbreaker'), self.context)
        message = self.update(text='09.10.2026')
        await bot.receive_sales_dates(message, self.context)
        keyboard = message.effective_message.reply_text.call_args.kwargs['reply_markup']
        data = next(b.callback_data for row in keyboard.inline_keyboard for b in row if b.callback_data.startswith('sales:orders:'))
        await bot.sales_report_period(self.update(data='sales:period:windbreaker:all'), self.context)
        orders = self.update(data=data)
        await bot.sales_orders(orders, self.context)
        self.assertIn('09.10.2026–09.10.2026', orders.callback_query.edit_message_text.call_args.args[0])
        self.assertLessEqual(len(data.encode()), 64)

    async def test_manager_can_create_promocode_and_sync_with_tilda(self):
        self.database.set_staff_role(3, 'manager', 1)
        client = SimpleNamespace(create=AsyncMock(return_value='remote-1'))
        self.context.application.bot_data['tilda_promocodes'] = client
        self.context.user_data['promocode'] = dict(code='STAFF10', discount_type='percent', discount_value='10', usage_limit=100, expires_on='', description='')
        update = self.update(user=3, data='promo:save')
        self.assertEqual(await bot.save_promocode(update, self.context), ConversationHandler.END)
        saved = self.database.list_promocodes()[0]
        self.assertEqual(saved['status'], 'active')
        self.assertEqual(saved['tilda_id'], 'remote-1')
        self.assertEqual(saved['created_by'], 3)
        client.create.assert_awaited_once()
        self.assertIn('создан и проверен в Tilda', update.callback_query.edit_message_text.call_args.args[0])

    async def test_direct_promocode_command_opens_create_and_history_for_manager(self):
        self.database.set_staff_role(3, 'manager', 1)
        self.context.user_data['product_search'] = 'unfinished'
        update = self.update(user=3, text='/promocodes')
        self.assertEqual(await bot.promocodes_menu(update, self.context), ConversationHandler.END)
        self.assertEqual(self.context.user_data, {})
        keyboard = update.effective_message.reply_text.call_args.kwargs['reply_markup']
        data = [b.callback_data for row in keyboard.inline_keyboard for b in row]
        self.assertIn('promo:add', data)
        self.assertIn('promo:list:0', data)

    async def test_direct_promocode_command_keeps_viewer_access_restricted(self):
        update = self.update(user=2, text='/promocodes')
        self.assertEqual(await bot.promocodes_menu(update, self.context), ConversationHandler.END)
        self.assertIn('нет прав', update.effective_message.reply_text.call_args.args[0])
        self.assertIsNone(update.effective_message.reply_text.call_args.kwargs.get('reply_markup'))

    async def test_direct_command_exits_each_active_conversation_before_next_input(self):
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            application = Application.builder().token('123456:test').build()
            bot.register_handlers(application)
        application.bot_data['database'] = self.database
        fake_bot = SimpleNamespace(username='test_bot')
        message = Message(
            message_id=1, date=datetime.now(bot.MOSCOW_TZ), chat=Chat(id=1, type='private'),
            from_user=User(id=1, first_name='Test', is_bot=False), text='/promocodes',
            entities=[MessageEntity(type='bot_command', offset=0, length=len('/promocodes'))],
        )
        message.set_bot(fake_bot)
        update = Update(update_id=1, message=message)
        context = SimpleNamespace(application=application, user_data={})
        top_handlers = application.handlers[0]
        for flow in [handler for handler in top_handlers if isinstance(handler, ConversationHandler)]:
            state = next(iter(flow.states))
            flow._conversations[(1, 1)] = state
            picked = next(handler for handler in top_handlers if handler.check_update(update))
            self.assertIs(picked, flow)
            with patch.object(Message, 'reply_text', new_callable=AsyncMock) as reply:
                await flow.handle_update(update, application, flow.check_update(update), context)
                reply.assert_awaited_once()
            self.assertNotIn((1, 1), flow._conversations)

    async def test_promocode_retry_after_tilda_error_keeps_same_code(self):
        client = SimpleNamespace(create=AsyncMock(side_effect=TildaPromocodeError('test unavailable')))
        self.context.application.bot_data['tilda_promocodes'] = client
        self.context.user_data['promocode'] = dict(code='RETRY10', discount_type='percent', discount_value='10', usage_limit=100, expires_on='', description='')
        await bot.save_promocode(self.update(data='promo:save'), self.context)
        saved = self.database.list_promocodes()[0]
        self.assertEqual(saved['status'], 'sync_failed')
        client.create = AsyncMock(return_value='remote-retry')
        await bot.retry_promocode_sync(self.update(data=f"promo:retry:{saved['id']}"), self.context)
        self.assertEqual(self.database.get_promocode(saved['id'])['status'], 'active')
        self.assertEqual(len(self.database.list_promocodes()), 1)

    async def test_paid_order_promocode_notification_is_sent_once(self):
        promo_id = self.database.create_promocode(code='USED10', discount_type='percent', discount_value='10', usage_limit=100, expires_on='', description='', created_by=1)
        self.database.activate_promocode(promo_id, 1, 'remote-used')
        self.database.set_setting('promocodes_chat_id', '-100123')
        self.database.set_setting('promocodes_thread_id', '0')
        self.database.upsert_order({'order_number': 'ORDER1', 'status': 'paid', 'source': 'tilda', 'promo_code': 'USED10', 'customer_name': 'Test Customer'}, [{'product_name': 'Ветровка', 'unit_price_kopecks': 10000}])
        self.context.application.bot = SimpleNamespace(send_message=AsyncMock())
        await bot.run_promocode_notifications(self.context.application)
        await bot.run_promocode_notifications(self.context.application)
        self.context.application.bot.send_message.assert_awaited_once()
        self.assertIn('ORDER1', self.context.application.bot.send_message.call_args.args[1])

    def test_daily_report_breaks_down_requested_products_with_discounts(self):
        now = datetime(2026, 10, 11, 15, 4, tzinfo=bot.MOSCOW_TZ)
        self.database.upsert_order(
            {'order_number': 'DAILY', 'status': 'paid', 'created_at': int(now.timestamp()),
             'discount_kopecks': 6000, 'delivery_cost_kopecks': 50000},
            [{'product_name': 'DEMO PUFFER BLACK', 'quantity': 1, 'unit_price_kopecks': 10000},
             {'product_name': 'DEMO WINDBREAKER', 'quantity': 2, 'unit_price_kopecks': 10000},
             {'product_name': 'DEMOZIP ZIP-HOODIE', 'quantity': 3, 'unit_price_kopecks': 10000}],
        )
        self.database.upsert_order(
            {'order_number': 'YESTERDAY', 'status': 'paid', 'created_at': int((now - timedelta(days=1)).timestamp())},
            [{'product_name': 'DEMOZIP ZIP-HOODIE', 'quantity': 5, 'unit_price_kopecks': 10000}],
        )
        with patch('bot.datetime', wraps=datetime) as clock:
            clock.now.return_value = now
            text = bot.sales_report_text(self.context, 'today')
        self.assertIn('По типам заказанных вещей:', text)
        self.assertIn('DEMO PUFFERS — 1 шт. · 90,00 ₽', text)
        self.assertIn('DEMO WINDBREAKER — 2 шт. · 180,00 ₽', text)
        self.assertIn('DEMOZIP ZIP — 3 шт. · 270,00 ₽', text)
        self.assertNotIn('BLACK', text)
        self.assertNotIn('Топ-10', text)
        self.assertIn('Всего: 6 шт. · 540,00 ₽', text)

    async def test_viewer_can_pick_exact_product_and_view_its_sales_and_orders(self):
        selected_id = self.database.upsert_order(
            {'order_number': 'SELECTED', 'status': 'paid', 'source': 'tilda'},
            [{'product_name': 'DEMOZIP ZIP BLACK', 'sku': 'BAM-S', 'size': 'S', 'unit_price_kopecks': 10000},
             {'product_name': 'DEMOZIP ZIP BLACK', 'sku': 'BAM-M', 'size': 'M', 'unit_price_kopecks': 20000}],
        )
        self.database.upsert_order(
            {'order_number': 'OTHER', 'status': 'paid', 'source': 'tilda'},
            [{'product_name': 'DEMOZIP ZIP BROWN', 'unit_price_kopecks': 90000}],
        )
        categories_before = self.database.list_sales_categories()
        picker = self.update(user=2, data='products:open')
        await bot.sales_products_menu(picker, self.context)
        keyboard = picker.callback_query.edit_message_text.call_args.kwargs['reply_markup']
        data = next(b.callback_data for row in keyboard.inline_keyboard for b in row if b.text == 'DEMOZIP ZIP BLACK')
        report = self.update(user=2, data=data)
        await bot.sales_report(report, self.context)
        text = report.callback_query.edit_message_text.call_args.args[0]
        self.assertIn('Заказов: 1', text)
        self.assertIn('Продано товаров: 2 шт.', text)
        self.assertIn('Товарная выручка: 300,00 ₽', text)
        self.assertNotIn('BROWN', text)
        report_keyboard = report.callback_query.edit_message_text.call_args.kwargs['reply_markup']
        orders_data = next(b.callback_data for row in report_keyboard.inline_keyboard for b in row if b.callback_data.startswith('sales:orders:'))
        orders = self.update(user=2, data=orders_data)
        await bot.sales_orders(orders, self.context)
        orders_keyboard = orders.callback_query.edit_message_text.call_args.kwargs['reply_markup']
        order_buttons = [b.callback_data for row in orders_keyboard.inline_keyboard for b in row if b.callback_data.startswith('order:readonly:')]
        self.assertEqual(order_buttons, [f'order:readonly:{selected_id}'])
        self.assertEqual(self.database.list_sales_categories(), categories_before)
        self.assertLessEqual(len(orders_data.encode()), 64)

    async def test_product_search_by_tilda_sku_preserves_exact_selection(self):
        self.database.upsert_order(
            {'order_number': 'SKU', 'status': 'paid', 'source': 'tilda'},
            [{'product_name': 'Ветровка BLACK', 'sku': 'WB-001', 'unit_price_kopecks': 10000}],
        )
        self.assertEqual(await bot.begin_product_search(self.update(user=2, data='products:search'), self.context), bot.PRODUCT_SEARCH)
        search = self.update(user=2, text='wb-001')
        self.assertEqual(await bot.receive_product_search(search, self.context), ConversationHandler.END)
        keyboard = search.effective_message.reply_text.call_args.kwargs['reply_markup']
        data = next(b.callback_data for row in keyboard.inline_keyboard for b in row if b.text == 'Ветровка BLACK')
        self.database.upsert_order(
            {'order_number': 'ADDED', 'status': 'paid', 'source': 'tilda'},
            [{'product_name': 'AAAA another product', 'unit_price_kopecks': 10000}],
        )
        report = self.update(user=2, data=data)
        await bot.sales_report(report, self.context)
        self.assertTrue(report.callback_query.edit_message_text.call_args.args[0].startswith('Ветровка BLACK'))

    async def test_product_picker_denies_user_without_order_permission(self):
        update = self.update(user=99, data='products:open')
        await bot.sales_products_menu(update, self.context)
        update.callback_query.edit_message_text.assert_not_awaited()
        update.callback_query.answer.assert_awaited_once()

    def test_product_picker_paginates_unique_names_across_sizes(self):
        self.database.upsert_order(
            {'order_number': 'MANY', 'status': 'paid', 'source': 'tilda'},
            [{'product_name': f'Product {i}', 'size': size, 'unit_price_kopecks': 10000}
             for i in range(9) for size in ['S', 'M']],
        )
        _, first = bot.products_picker(self.context)
        first_data = [b.callback_data for row in first.inline_keyboard for b in row]
        self.assertEqual(sum(d.startswith('orders:sales:i') for d in first_data), 8)
        self.assertIn('products:page:1', first_data)
        _, second = bot.products_picker(self.context, 1)
        second_data = [b.callback_data for row in second.inline_keyboard for b in row]
        self.assertEqual(sum(d.startswith('orders:sales:i') for d in second_data), 1)
        self.assertIn('products:page:0', second_data)

    async def test_reply_menu_opens_only_selected_section(self):
        orders = self.update(user=2, text=bot.MENU_ORDERS)
        await bot.persistent_menu_button(orders, self.context)
        orders.effective_message.reply_text.assert_awaited_once()
        self.assertTrue(orders.effective_message.reply_text.call_args.args[0].startswith('📦 Заказы'))
        analytics = self.update(text=bot.MENU_ANALYTICS)
        await bot.persistent_menu_button(analytics, self.context)
        analytics.effective_message.reply_text.assert_awaited_once()
        self.assertTrue(analytics.effective_message.reply_text.call_args.args[0].startswith('📊 Аналитика'))
        operations = self.update(user=2, text=bot.MENU_OPERATIONS)
        await bot.persistent_menu_button(operations, self.context)
        operations.effective_message.reply_text.assert_awaited_once()
        keyboard = operations.effective_message.reply_text.call_args.kwargs['reply_markup']
        self.assertIn('orders:browse', [b.callback_data for row in keyboard.inline_keyboard for b in row])

    async def test_telegram_statistics_do_not_collect_or_show_other_networks(self):
        unused = SimpleNamespace(tiktok_followers=AsyncMock(), instagram_followers=AsyncMock())
        self.context.application.bot_data['social_stats'] = unused
        self.context.application.bot = SimpleNamespace(get_chat_member_count=AsyncMock(return_value=123))
        self.database.save_social_followers('tiktok', '2026-10-01', 500, 'legacy')
        self.database.save_social_followers('instagram', '2026-10-01', 700, 'legacy')
        update = self.update(data='analytics:social')
        await bot.social_network_stats(update, self.context)
        unused.tiktok_followers.assert_not_awaited()
        unused.instagram_followers.assert_not_awaited()
        self.context.application.bot.get_chat_member_count.assert_awaited_once()
        text = update.callback_query.edit_message_text.call_args.args[0]
        self.assertIn('Telegram', text)
        self.assertIn('123', text)
        self.assertNotIn('TikTok', text)
        self.assertNotIn('Instagram', text)
        self.assertEqual(len(update.callback_query.message.reply_text.call_args_list), 1)

    async def test_yesterdays_revenue_available_to_viewer_and_keeps_full_order_amounts(self):
        now = datetime(2026, 10, 14, 15, 0, tzinfo=bot.MOSCOW_TZ)
        yesterday = int((now - timedelta(days=1)).timestamp())
        self.database.upsert_order(
            {'order_number': 'YESTERDAY', 'status': 'paid', 'created_at': yesterday, 'total_kopecks': 15000},
            [{'product_name': 'DEMOZIP ZIP', 'unit_price_kopecks': 10000}],
        )
        self.database.upsert_order(
            {'order_number': 'CANCELLED', 'status': 'cancelled', 'created_at': yesterday, 'total_kopecks': 20000},
            [{'product_name': 'DEMO PUFFER', 'unit_price_kopecks': 20000}],
        )
        update = self.update(user=2, data='revenue:period:yesterday')
        with patch('bot.datetime', wraps=datetime) as clock:
            clock.now.return_value = now
            await bot.revenue_period(update, self.context)
        text = update.callback_query.edit_message_text.call_args.args[0]
        self.assertIn('Выручка: вчера', text)
        self.assertIn('13.10.2026–13.10.2026', text)
        self.assertIn('Всего: 1 шт. · 100,00 ₽', text)
        self.assertIn('Полная сумма заказов, включая доставку: 350,00 ₽', text)
        self.assertNotIn('DEMO PUFFERS', text)
        buttons = update.callback_query.edit_message_text.call_args.kwargs['reply_markup']
        self.assertFalse(any(b.callback_data.startswith(('orders:browse', 'sales:orders:')) for row in buttons.inline_keyboard for b in row))

    async def test_revenue_custom_dates_retain_inclusive_end_and_new_navigation(self):
        start = datetime(2026, 10, 13, tzinfo=bot.MOSCOW_TZ)
        self.database.upsert_order(
            {'order_number': 'LATE', 'status': 'paid', 'created_at': int((start+timedelta(days=1, hours=23)).timestamp())},
            [{'product_name': 'DEMO WINDBREAKER', 'quantity': 2, 'unit_price_kopecks': 10000}],
        )
        self.database.upsert_order(
            {'order_number': 'AFTER', 'status': 'paid', 'created_at': int((start+timedelta(days=2)).timestamp())},
            [{'product_name': 'DEMO WINDBREAKER', 'quantity': 9, 'unit_price_kopecks': 10000}],
        )
        self.assertEqual(await bot.begin_revenue_dates(self.update(user=2, data='revenue:dates'), self.context), bot.REVENUE_DATE_RANGE)
        message = self.update(user=2, text='13.10.2026 — 14.10.2026')
        self.assertEqual(await bot.receive_revenue_dates(message, self.context), ConversationHandler.END)
        text = message.effective_message.reply_text.call_args.args[0]
        self.assertIn('DEMO WINDBREAKER — 2 шт.', text)
        self.assertIn('13.10.2026–14.10.2026', text)

    async def test_readonly_operations_deny_unknown_user(self):
        update = self.update(user=99, data='menu:operations')
        await bot.operations_menu(update, self.context)
        update.callback_query.edit_message_text.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
