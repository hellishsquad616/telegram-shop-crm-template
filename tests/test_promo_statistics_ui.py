import tempfile
import unittest
import warnings
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram.error import BadRequest
from telegram.ext import Application, CallbackQueryHandler, ConversationHandler

import bot
from database import Database


class PromoStatisticsUITest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp.name) / 'stats.sqlite3')
        self.database.initialize()
        self.database.ensure_admins([1])
        self.database.set_staff_role(2, 'manager', 1)
        self.database.set_staff_role(3, 'viewer', 1)
        self.context = SimpleNamespace(application=SimpleNamespace(bot_data={'database': self.database}), user_data={})
        self.now = datetime(2026, 10, 19, 10, 47, tzinfo=bot.MOSCOW_TZ)

    def tearDown(self):
        self.temp.cleanup()

    def update(self, data=None, text=None, user=1):
        query = SimpleNamespace(data=data, answer=AsyncMock(), edit_message_text=AsyncMock()) if data else None
        return SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=user),
                               effective_message=SimpleNamespace(text=text, reply_text=AsyncMock()))

    def order(self, number, code, when=None, **values):
        order = {'order_number': number, 'status': 'paid', 'promo_code': code,
                 'created_at': int((when or self.now).timestamp()), 'total_kopecks': 15000}
        order.update(values)
        self.database.upsert_order(order, [{'product_name': 'DEMO PUFFER', 'unit_price_kopecks': 10000}])

    def frozen(self):
        return patch('bot.datetime', wraps=datetime)

    async def test_today_report_matches_reference_and_has_required_periods(self):
        self.order('one', 'BLOGGER', phone='79990000000')
        self.order('two', 'BLOGGER', phone='79990000000')
        self.order('old', 'OLD', self.now-timedelta(days=1))
        update = self.update(data='ps:r:a:today:0', user=2)
        with self.frozen() as clock:
            clock.now.return_value = self.now
            await bot.show_promo_statistics(update, self.context)
        text = update.callback_query.edit_message_text.call_args.args[0]
        self.assertIn('Сегодня · 19.10.2026', text)
        self.assertIn('BLOGGER — 1 чел. — 2 — 300,00 ₽', text)
        self.assertNotIn('OLD —', text)
        self.assertIn('Всего покупателей: 1', text)
        self.assertIn('после скидки, с доставкой', text)
        keyboard = update.callback_query.edit_message_text.call_args.kwargs['reply_markup']
        buttons = [b.callback_data for row in keyboard.inline_keyboard for b in row]
        self.assertIn('ps:r:a:all:0', buttons)
        self.assertIn('ps:dates:a', buttons)
        self.assertIn('ps:days:a:0', buttons)
        self.assertIn('ps:r:a:month:0', buttons)
        self.assertTrue(all(len(data.encode()) <= 64 for data in buttons))

    async def test_specific_code_card_opens_filtered_all_time_report(self):
        promo_id = self.database.create_promocode(code='ONE', discount_type='percent', discount_value='10',
                                                   usage_limit=0, expires_on='', description='', created_by=1)
        self.order('one', 'ONE')
        self.order('two', 'OTHER')
        keyboard = bot.promocode_card_keyboard(self.database.get_promocode(promo_id))
        self.assertIn(f'ps:r:p{promo_id}:all:0', [b.callback_data for row in keyboard.inline_keyboard for b in row])
        text, _ = bot.promo_statistics_report(self.context, f'p{promo_id}', 'all')
        self.assertIn('Промокод ONE', text)
        self.assertIn('ONE —', text)
        self.assertNotIn('OTHER —', text)
        self.assertIn('Всего использований (заказов): 1', text)

    async def test_custom_dates_include_last_day_and_old_buttons_keep_their_period(self):
        start = self.now.replace(hour=0, minute=0)
        self.order('before', 'BEFORE', start-timedelta(seconds=1))
        self.order('start', 'FIRST', start)
        self.order('last', 'LAST', start+timedelta(days=1, hours=23, minutes=59))
        self.order('outside', 'OUTSIDE', start+timedelta(days=2))
        self.assertEqual(await bot.begin_promo_statistics_dates(self.update(data='ps:dates:a'), self.context), bot.PROMO_STATS_DATES)
        message = self.update(text='19.10.2026 — 20.10.2026')
        self.assertEqual(await bot.receive_promo_statistics_dates(message, self.context), ConversationHandler.END)
        text = message.effective_message.reply_text.call_args.args[0]
        self.assertIn('FIRST —', text)
        self.assertIn('LAST —', text)
        self.assertNotIn('BEFORE —', text)
        self.assertNotIn('OUTSIDE —', text)
        keyboard = message.effective_message.reply_text.call_args.kwargs['reply_markup']
        refresh = keyboard.inline_keyboard[0][0].callback_data
        self.assertEqual(refresh, 'ps:r:a:20261019-20261020:0')
        self.context.user_data['promo_stats_scope'] = 'p999'
        old = self.update(data=refresh)
        await bot.show_promo_statistics(old, self.context)
        self.assertIn('LAST —', old.callback_query.edit_message_text.call_args.args[0])

    async def test_invalid_dates_keep_input_dialog_open(self):
        self.context.user_data['promo_stats_scope'] = 'a'
        for value in ['31.02.2026', '20.10.2026 — 19.10.2026', '19.10.2026 extra']:
            self.assertEqual(await bot.receive_promo_statistics_dates(self.update(text=value), self.context), bot.PROMO_STATS_DATES)

    async def test_opening_saved_report_closes_unfinished_date_scope(self):
        self.context.user_data['promo_stats_scope'] = 'p999'
        update = self.update(data='ps:r:a:all:0')
        self.assertEqual(await bot.navigate_promo_statistics(update, self.context), ConversationHandler.END)
        self.assertNotIn('promo_stats_scope', self.context.user_data)
        update.callback_query.edit_message_text.assert_awaited_once()

    async def test_refresh_handles_unchanged_telegram_message(self):
        update = self.update(data='ps:r:a:all:0')
        update.callback_query.edit_message_text.side_effect = BadRequest('Message is not modified')
        await bot.show_promo_statistics(update, self.context)
        update.callback_query.answer.assert_awaited_once()

    async def test_viewer_and_unknown_user_cannot_read_reports(self):
        for user in (3, 999):
            update = self.update(data='ps:r:a:all:0', user=user)
            await bot.show_promo_statistics(update, self.context)
            update.callback_query.edit_message_text.assert_not_awaited()
            update.callback_query.answer.assert_awaited_once()

    async def test_previous_days_select_exact_moscow_day(self):
        update = self.update(data='ps:days:a:0')
        with self.frozen() as clock:
            clock.now.return_value = self.now
            await bot.promo_statistics_previous_days(update, self.context)
        keyboard = update.callback_query.edit_message_text.call_args.kwargs['reply_markup']
        self.assertEqual(keyboard.inline_keyboard[0][0].text, '18.10.2026')
        self.assertEqual(keyboard.inline_keyboard[0][0].callback_data, 'ps:r:a:20261018:0')

    def test_month_period_handles_december_year_boundary(self):
        with self.frozen() as clock:
            clock.now.return_value = self.now.replace(month=12)
            start, end, _ = bot.promo_statistics_period('month')
        self.assertEqual(datetime.fromtimestamp(start, bot.MOSCOW_TZ), datetime(2026,12,1,tzinfo=bot.MOSCOW_TZ))
        self.assertEqual(datetime.fromtimestamp(end, bot.MOSCOW_TZ), datetime(2027,1,1,tzinfo=bot.MOSCOW_TZ))

    def test_large_reports_paginate_without_losing_complete_totals(self):
        for index in range(25):
            self.order(str(index), f'PROMO{index:02}')
        text, keyboard = bot.promo_statistics_report(self.context, 'a', 'all')
        self.assertIn('Всего использований (заказов): 25', text)
        self.assertIn('Сумма заказов: 3 750,00 ₽', text)
        self.assertIn('Страница 1 из 3', text)
        self.assertLess(len(text), 4096)
        self.assertIn('ps:r:a:all:1', [b.callback_data for row in keyboard.inline_keyboard for b in row])
        last, _ = bot.promo_statistics_report(self.context, 'a', 'all', 2)
        self.assertIn('Страница 3 из 3', last)
        self.assertIn('Всего использований (заказов): 25', last)

    def test_statistics_handlers_are_registered_before_retired_buttons(self):
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            application = Application.builder().token('123456:test').build()
            bot.register_handlers(application)
        handlers = [h for h in application.handlers[0] if isinstance(h, CallbackQueryHandler)]
        for data, callback in [('ps:r:a:today:0', bot.show_promo_statistics), ('ps:days:a:0', bot.promo_statistics_previous_days)]:
            matched = next(h for h in handlers if h.pattern is None or h.pattern.match(data))
            self.assertEqual(matched.callback, callback)
        self.assertIn('ps:r:a:today:0', [b.callback_data for row in bot.promocodes_keyboard().inline_keyboard for b in row])

    def model(self, key='ziphoodie'):
        return 'm' + str(self.database.get_sales_category(key)['id'])

    async def test_model_picker_uses_actual_order_models_and_historical_code(self):
        order_id = self.database.upsert_order({'order_number': 'ziphoodie', 'status': 'paid', 'promo_code': 'CREATOR',
                                               'created_at': int(self.now.timestamp())}, [
            {'product_name': 'DEMOZIP ZIP', 'unit_price_kopecks': 400},
            {'product_name': 'DEMO PUFFER', 'unit_price_kopecks': 600},
        ])
        query = self.update(data=f'ps:models:o{order_id}:all:0')
        await bot.promo_statistics_picker(query, self.context)
        keyboard = query.callback_query.edit_message_text.call_args.kwargs['reply_markup']
        buttons = [b for row in keyboard.inline_keyboard for b in row]
        self.assertTrue(any('DEMOZIP ZIP' in b.text and '4,00 ₽' in b.text for b in buttons))
        self.assertIn(f'ps:r:o{order_id}:all:0:{self.model()}', [b.callback_data for b in buttons])
        codes = self.update(data=f'ps:codes:a:20261001-20261031:0:{self.model()}')
        await bot.promo_statistics_picker(codes, self.context)
        codes_keyboard = codes.callback_query.edit_message_text.call_args.kwargs['reply_markup']
        self.assertIn(f'ps:r:o{order_id}:20261001-20261031:0:{self.model()}',
                      [b.callback_data for row in codes_keyboard.inline_keyboard for b in row])

    async def test_selected_model_survives_dates_refresh_and_previous_days_without_session_filter(self):
        self.database.upsert_order({'order_number': 'ziphoodie', 'status': 'paid', 'promo_code': 'CREATOR',
                                    'created_at': int(self.now.timestamp()), 'total_kopecks': 900}, [
            {'product_name': 'DEMOZIP ZIP', 'unit_price_kopecks': 400},
        ])
        model = self.model()
        await bot.begin_promo_statistics_dates(self.update(data=f'ps:dates:a:{model}'), self.context)
        update = self.update(text='01.10.2026 — 31.10.2026')
        self.assertEqual(await bot.receive_promo_statistics_dates(update, self.context), ConversationHandler.END)
        text = update.effective_message.reply_text.call_args.args[0]
        self.assertIn('Модель: DEMOZIP ZIP', text)
        self.assertIn('Всего вещей: 1 шт.', text)
        self.assertIn('Стоимость вещей: 4,00 ₽', text)
        self.assertIn('19.10.2026 10:47', text)
        self.assertNotIn('Сумма заказов: 9,00', text)
        keyboard = update.effective_message.reply_text.call_args.kwargs['reply_markup']
        refresh = keyboard.inline_keyboard[0][0].callback_data
        self.assertEqual(refresh, f'ps:r:a:20261001-20261031:0:{model}')
        self.context.user_data.update(promo_stats_scope='p999', promo_stats_model='m999')
        old = self.update(data=refresh)
        await bot.show_promo_statistics(old, self.context)
        self.assertIn('Модель: DEMOZIP ZIP', old.callback_query.edit_message_text.call_args.args[0])
        days = self.update(data=f'ps:days:a:0:{model}')
        await bot.promo_statistics_previous_days(days, self.context)
        days_keyboard = days.callback_query.edit_message_text.call_args.kwargs['reply_markup']
        self.assertTrue(days_keyboard.inline_keyboard[0][0].callback_data.endswith(':' + model))
        self.assertTrue(all(len(b.callback_data.encode()) <= 64 for row in keyboard.inline_keyboard for b in row))

    async def test_picker_permissions_and_hidden_model_errors(self):
        denied = self.update(data='ps:models:a:all:0', user=3)
        await bot.promo_statistics_picker(denied, self.context)
        denied.callback_query.edit_message_text.assert_not_awaited()
        stale = self.update(data='ps:r:a:all:0:m999')
        await bot.show_promo_statistics(stale, self.context)
        stale.callback_query.answer.assert_awaited_once()
        stale.callback_query.edit_message_text.assert_not_awaited()

    def test_model_handlers_accept_saved_buttons_and_callbacks_fit_telegram_limit(self):
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            application = Application.builder().token('123456:test').build()
            bot.register_handlers(application)
        handlers = [h for h in application.handlers[0] if isinstance(h, CallbackQueryHandler)]
        for data, callback in [(f'ps:r:o123:20261001-20261031:0:{self.model()}', bot.show_promo_statistics),
                               (f'ps:days:o123:0:{self.model()}', bot.promo_statistics_previous_days),
                               ('ps:models:a:all:0', bot.promo_statistics_picker),
                               (f'ps:codes:o123:all:0:{self.model()}', bot.promo_statistics_picker)]:
            matched = next(h for h in handlers if h.pattern is None or h.pattern.match(data))
            self.assertEqual(matched.callback, callback)
        keyboard = bot.promo_statistics_keyboard('o1234567890', '20261001-20261031', 99, 100, 'm1234567890')
        self.assertTrue(all(len(b.callback_data.encode()) <= 64 for row in keyboard.inline_keyboard for b in row))


if __name__ == '__main__':
    unittest.main()
