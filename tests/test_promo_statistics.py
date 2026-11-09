import tempfile
import unittest
from pathlib import Path

from database import Database
from promo_statistics import buyer_contacts


class PromoStatisticsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp.name) / 'stats.sqlite3')
        self.database.initialize()

    def tearDown(self):
        self.temp.cleanup()

    def order(self, number, code='BLOGGER', **values):
        order = {'order_number': number, 'status': 'paid', 'source': 'tilda',
                 'promo_code': code, 'created_at': 1000, 'total_kopecks': 10000}
        order.update(values)
        return self.database.upsert_order(order, [
            {'product_name': 'DEMO PUFFER', 'quantity': 2, 'unit_price_kopecks': 10000},
            {'product_name': 'DEMOZIP ZIP', 'quantity': 1, 'unit_price_kopecks': 10000},
        ])

    def test_repeat_buyer_uses_count_orders_and_final_amount_including_delivery(self):
        self.order('1', ' blogger ', phone='+7 (999) 000-00-00', total_kopecks=15000,
                   discount_kopecks=5000, delivery_cost_kopecks=5000)
        self.order('2', 'BLOGGER', phone='89990000000', total_kopecks=25000)
        self.order('3', 'blogger', phone='79991111111', total_kopecks=30000)
        report = self.database.promocode_sales_report()
        self.assertEqual(report['orders_count'], 3)
        self.assertEqual(report['buyers_count'], 2)
        self.assertEqual(report['amount_kopecks'], 70000)
        self.assertEqual(report['rows'], [{'code': 'BLOGGER', 'orders_count': 3,
                                          'buyers_count': 2, 'amount_kopecks': 70000}])

    def test_contacts_bridge_email_only_order_and_deduplicate_across_codes(self):
        self.order('1', 'FIRST', phone='79990000000', email='Shopper@Example.com')
        self.order('2', 'SECOND', email=' shopper@example.com ')
        self.order('3', 'SECOND', phone='79990000000', telegram_username='@Shopper')
        self.order('4', 'FIRST', telegram_username='https://t.me/shopper/')
        report = self.database.promocode_sales_report()
        self.assertEqual(report['buyers_count'], 1)
        self.assertEqual(report['orders_count'], 4)
        self.assertEqual(sum(row['buyers_count'] for row in report['rows']), 2)
        self.assertEqual(self.database.promocode_sales_report(code='second')['buyers_count'], 1)

    def test_do_not_merge_people_by_name_when_contacts_are_missing(self):
        self.order('1', customer_name='Александр')
        self.order('2', customer_name='Александр')
        report = self.database.promocode_sales_report()
        self.assertEqual(report['buyers_count'], 2)
        self.assertEqual(report['orders_without_contacts'], 2)

    def test_paid_statuses_exclude_unpaid_cancelled_and_fully_refunded(self):
        for status in ('paid', 'processing', 'partially_returned', 'exchange', 'exchange_inbound', 'deposit',
                       'new', 'cancelled', 'returned', 'refund_completed'):
            self.order(status, status=status)
        report = self.database.promocode_sales_report()
        self.assertEqual(report['orders_count'], 6)
        self.assertEqual(report['amount_kopecks'], 60000)

    def test_date_window_is_half_open_and_sums_are_consistent(self):
        for number, created_at in [('before', 999), ('first', 1000), ('last', 1999), ('after', 2000)]:
            self.order(number, created_at=created_at)
        report = self.database.promocode_sales_report(1000, 2000)
        self.assertEqual(report['orders_count'], 2)
        self.assertEqual(report['amount_kopecks'], 20000)
        self.assertEqual(self.database.promocode_sales_report()['orders_count'], 4)

    def test_codes_created_outside_bot_and_deleted_managed_codes_keep_sales(self):
        promo_id = self.database.create_promocode(code='OLD', discount_type='percent', discount_value='10',
                                                   usage_limit=0, expires_on='', description='', created_by=1)
        self.database.delete_promocode(promo_id, 1)
        self.order('old', 'OLD')
        self.order('manual', 'MANUAL-TILDA')
        self.order('csv', 'CSV', source='tilda_csv')
        self.order('archive', 'ARCHIVE', source='telegram_archive')
        report = self.database.promocode_sales_report()
        self.assertEqual(report['codes_count'], 4)
        self.assertEqual(report['orders_count'], 4)
        self.assertEqual(self.database.promocode_sales_report(code=' old ')['orders_count'], 1)

    def test_repeated_webhook_does_not_double_count_orders_or_joined_items(self):
        self.order('same', phone='79990000000')
        self.order('same', phone='79990000000')
        report = self.database.promocode_sales_report()
        self.assertEqual(report['orders_count'], 1)
        self.assertEqual(report['buyers_count'], 1)
        self.assertEqual(report['amount_kopecks'], 10000)

    def test_empty_codes_are_ignored_and_missing_report_is_zero(self):
        self.order('blank', '   ')
        self.order('whitespace', '\t\n')
        self.order('no-code', '')
        report = self.database.promocode_sales_report()
        self.assertEqual(report, {'rows': [], 'buyers_count': 0, 'orders_count': 0,
                                  'amount_kopecks': 0, 'codes_count': 0, 'orders_without_contacts': 0})
        self.assertEqual(self.database.promocode_sales_report(code="' OR 1=1 --")['orders_count'], 0)

    def test_card_paid_usage_count_matches_statistics(self):
        promo_id = self.database.create_promocode(code='BLOGGER', discount_type='percent', discount_value='10',
                                                   usage_limit=0, expires_on='', description='', created_by=1)
        self.order('paid', status='paid')
        self.order('new', status='new')
        self.order('refund', status='returned')
        self.assertEqual(self.database.get_promocode(promo_id)['paid_orders_count'], 1)
        self.assertEqual(self.database.list_promocodes()[0]['paid_orders_count'], 1)
        self.assertEqual(self.database.promocode_sales_report(code='BLOGGER')['orders_count'], 1)

    def test_normalizes_contacts_without_using_names(self):
        self.assertEqual(buyer_contacts({'phone': '8 (999) 000-00-00', 'email': ' USER@EXAMPLE.COM ',
                                        'telegram_username': 'https://t.me/UserName/'}),
                         ['phone:79990000000', 'email:user@example.com', 'telegram:username'])
        self.assertEqual(buyer_contacts({'customer_name': 'Same Name', 'phone': '-', 'telegram_username': 'нет'}), [])

    def test_buyer_identity_stays_consistent_when_date_filter_excludes_contact_link(self):
        self.order('link', 'OTHER', created_at=100, phone='79990000000', email='same@example.com')
        self.order('phone', created_at=1000, phone='79990000000')
        self.order('email', created_at=1000, email='same@example.com')
        filtered = self.database.promocode_sales_report(1000, 2000, code='BLOGGER')
        self.assertEqual(filtered['orders_count'], 2)
        self.assertEqual(filtered['buyers_count'], 1)

    def model(self, key):
        return 'm' + str(self.database.get_sales_category(key)['id'])

    def test_model_report_separates_mixed_cart_quantity_and_allocates_discount_without_delivery(self):
        self.database.upsert_order({'order_number': 'mixed', 'status': 'paid', 'promo_code': 'CREATOR',
                                    'created_at': 1000, 'phone': '79990000000', 'discount_kopecks': 301,
                                    'delivery_cost_kopecks': 500, 'total_kopecks': 1199}, [
            {'product_name': 'DEMO PUFFER BLACK', 'quantity': 2, 'unit_price_kopecks': 300},
            {'product_name': 'DEMOZIP ZIP BROWN', 'quantity': 1, 'unit_price_kopecks': 400},
            {'product_name': 'DEMO SCARF GIFT', 'quantity': 1, 'unit_price_kopecks': 0},
        ])
        puffer = self.database.promocode_sales_report(code='creator', model=self.model('puffer'))
        ziphoodie = self.database.promocode_sales_report(code='creator', model=self.model('ziphoodie'))
        all_goods = self.database.promocode_sales_report(code='creator', model='all')
        self.assertEqual((puffer['orders_count'], puffer['items_quantity'], puffer['amount_kopecks']), (1, 2, 419))
        self.assertEqual((ziphoodie['buyers_count'], ziphoodie['orders_count'], ziphoodie['items_quantity'], ziphoodie['amount_kopecks']), (1, 1, 1, 280))
        self.assertEqual(all_goods['amount_kopecks'], puffer['amount_kopecks'] + ziphoodie['amount_kopecks'])
        self.assertEqual(all_goods['orders_count'], 1)
        self.assertEqual(all_goods['items_quantity'], 3)
        self.assertEqual(len(all_goods['models']), 2)
        self.assertEqual({row['first_at'] for row in all_goods['models']}, {1000})
        self.assertEqual(self.database.promocode_sales_report(code='creator')['amount_kopecks'], 1199)

    def test_model_first_purchase_and_period_exclude_other_codes_models_and_inactive_items(self):
        self.database.upsert_order({'order_number': 'old-puffer', 'status': 'paid', 'promo_code': 'CREATOR',
                                    'created_at': 100}, [{'product_name': 'DEMO PUFFER', 'unit_price_kopecks': 300}])
        self.database.upsert_order({'order_number': 'ziphoodie', 'status': 'paid', 'promo_code': ' creator ',
                                    'created_at': 2000, 'email': 'same@example.com'}, [
            {'product_name': 'DEMOZIP ZIP BLACK', 'unit_price_kopecks': 400},
            {'product_name': 'DEMOZIP ZIP GREY', 'unit_price_kopecks': 500},
        ])
        self.database.upsert_order({'order_number': 'other', 'status': 'paid', 'promo_code': 'OTHER',
                                    'created_at': 1000}, [{'product_name': 'DEMOZIP ZIP', 'unit_price_kopecks': 900}])
        with self.database.connect() as connection:
            connection.execute("UPDATE order_items SET item_status='returned' WHERE product_name='DEMOZIP ZIP GREY'")
        report = self.database.promocode_sales_report(code='CREATOR', model=self.model('ziphoodie'))
        self.assertEqual((report['first_at'], report['last_at'], report['orders_count'], report['items_quantity']), (2000, 2000, 1, 1))
        self.assertEqual(report['amount_kopecks'], 400)
        self.assertEqual(self.database.promocode_sales_report(100, 2000, 'CREATOR', self.model('ziphoodie'))['orders_count'], 0)
        self.assertEqual(self.database.promocode_sales_report(2000, 2001, 'CREATOR', self.model('ziphoodie'))['orders_count'], 1)

    def test_model_rules_are_dynamic_case_insensitive_and_overlaps_count_once(self):
        key = self.database.save_sales_category('Новая модель', [{'kind': 'sku', 'value': 'NEW-123'}])
        self.database.upsert_order({'order_number': 'new', 'status': 'paid', 'promo_code': 'CREATOR',
                                    'created_at': 1000}, [
            {'product_name': 'Новинка', 'sku': 'new-123', 'unit_price_kopecks': 100},
            {'product_name': 'DEMOZIP PUFFER', 'unit_price_kopecks': 200},
            {'product_name': 'Без категории', 'unit_price_kopecks': 300},
        ])
        report = self.database.promocode_sales_report(model='all')
        self.assertEqual(report['items_quantity'], 3)
        self.assertEqual(sum(row['amount_kopecks'] for row in report['models']), 600)
        self.assertEqual(self.database.promocode_sales_report(model=self.model(key))['amount_kopecks'], 100)
        self.assertEqual(self.database.promocode_sales_report(model='other')['amount_kopecks'], 300)
        self.database.set_sales_category_active(key, False)
        with self.assertRaises(ValueError):
            self.database.promocode_sales_report(model=self.model(key))
        with self.assertRaises(ValueError):
            self.database.promocode_sales_report(model="' OR 1=1 --")

    def test_model_discount_cannot_make_revenue_negative_and_empty_codes_are_excluded(self):
        self.order('discount', discount_kopecks=999999)
        self.order('unpaid', status='new')
        self.order('cancelled', status='cancelled')
        self.order('returned', status='returned')
        self.order('spaces', code='\t\n')
        report = self.database.promocode_sales_report(model=self.model('ziphoodie'))
        self.assertEqual((report['orders_count'], report['amount_kopecks'], report['items_quantity']), (1, 0, 1))
        self.assertEqual(sum(row['items_quantity'] for row in report['models']), 3)

    def test_historical_codes_picker_includes_unmanaged_codes_once_with_pagination(self):
        self.order('one', ' creator ')
        self.order('two', 'CREATOR')
        self.order('three', 'ANOTHER')
        self.order('no', '', status='new')
        self.assertEqual([row['code'] for row in self.database.promocode_statistics_codes()], ['ANOTHER', 'CREATOR'])
        self.assertEqual(len(self.database.promocode_statistics_codes(limit=1, offset=1)), 1)


if __name__ == '__main__':
    unittest.main()
