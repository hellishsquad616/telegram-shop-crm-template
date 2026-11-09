import tempfile
import unittest
from pathlib import Path

from database import Database
from integrations.tilda import normalize_tilda_order
from sales_categories import parse_rules


class SalesCategoriesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'test.sqlite3'
        self.database = Database(self.path)
        self.database.initialize()

    def tearDown(self):
        self.temp.cleanup()

    def order(self, number, products, **values):
        created_at = values.pop('created_at', 1000)
        order, items = normalize_tilda_order({
            'payment': {'orderid': number, 'products': products},
            'created_at': created_at,
        })
        order["created_at"] = created_at
        order.update(values)
        return self.database.upsert_order(order, items)

    def test_windbreaker_matches_russian_and_english_tilda_names(self):
        self.order('1', [{'name': 'DEMO Windbreaker BLACK', 'quantity': 2, 'price': 100, 'size': 'M'}])
        self.order('2', [{'name': 'ВЕТРОВКА голубая', 'price': 200, 'size': 'S'}])
        self.order('3', [{'name': 'BASE black', 'price': 300}])
        stats = self.database.sales_summary('windbreaker')
        self.assertEqual(stats['orders_count'], 2)
        self.assertEqual(stats['items_quantity'], 3)
        self.assertEqual(stats['revenue_kopecks'], 40000)
        self.assertEqual(sum(item['quantity'] for item in self.database.sales_top_items('windbreaker')), 3)

    def test_categories_work_for_past_future_and_changed_tilda_name_with_same_sku(self):
        self.order('old', [{'name': 'First title', 'externalid': 'sku-001', 'price': 100}])
        key = self.database.save_sales_category('Новая вещь', parse_rules('sku:SKU-001'))
        self.order('future', [{'name': 'Renamed title', 'externalid': 'SKU-001', 'price': 200}])
        self.order('other', [{'name': 'First title', 'externalid': 'SKU-002', 'price': 900}])
        self.assertEqual(self.database.sales_summary(key)['revenue_kopecks'], 30000)
        self.assertEqual({r['order_number'] for r in self.database.sales_category_orders(key)}, {'old', 'future'})
        catalog = self.database.tilda_sales_catalog()
        self.assertIn('SKU-001', {r['sku'] for r in catalog})

    def test_mixed_orders_allocate_discount_and_never_include_delivery(self):
        self.order('mix', [
            {'name': 'Ветровка', 'price': 100, 'quantity': 2},
            {'name': 'PUFFER', 'price': 200},
        ], discount_kopecks=4000, delivery_cost_kopecks=10000, total_kopecks=46000)
        key = self.database.save_sales_category('Ветровки по двум правилам', parse_rules('ветров\nname:Ветровка'))
        wind = self.database.sales_summary(key)
        puffer = self.database.sales_summary('puffer')
        all_items = self.database.sales_summary('all')
        self.assertEqual(wind['items_quantity'], 2)
        self.assertEqual(wind['revenue_kopecks'], 18000)
        self.assertEqual(puffer['revenue_kopecks'], 18000)
        self.assertEqual(all_items['revenue_kopecks'], 36000)
        self.assertEqual(self.database.sales_top_items(key)[0]['revenue_kopecks'], 18000)

    def test_report_and_order_list_use_identical_exclusions_and_half_open_period(self):
        for number, status, created_at in [('before', 'paid', 999), ('start', 'paid', 1000),
                                           ('last', 'paid', 1999), ('end', 'paid', 2000),
                                           ('unpaid', 'new', 1500), ('cancel', 'cancelled', 1500),
                                           ('returned', 'returned', 1500), ('refund', 'refund_completed', 1500)]:
            self.order(number, [{'name': 'Ветровка', 'price': 100}], status=status, created_at=created_at)
        stats = self.database.sales_summary('windbreaker', 1000, 2000)
        rows = self.database.sales_category_orders('windbreaker', 1000, 2000)
        self.assertEqual(stats['orders_count'], 2)
        self.assertEqual({r['order_number'] for r in rows}, {'start', 'last'})
        self.assertEqual(sum(r['quantity'] for r in self.database.sales_top_items('windbreaker', 1000, 2000)), 2)

    def test_hidden_and_edited_defaults_survive_initialization_without_losing_orders(self):
        self.order('1', [{'name': 'Куртка', 'price': 100}])
        self.database.save_sales_category('Куртки', parse_rules('куртк'), key='windbreaker')
        self.database.set_sales_category_active('windbreaker', False)
        self.database.initialize()
        self.database = Database(self.path)
        self.assertEqual(self.database.get_sales_category('windbreaker')['label'], 'Куртки')
        self.assertNotIn('windbreaker', {c['key'] for c in self.database.list_sales_categories()})
        with self.assertRaises(ValueError):
            self.database.sales_summary('windbreaker')
        self.database.set_sales_category_active('windbreaker', True)
        self.assertEqual(self.database.sales_summary('windbreaker')['orders_count'], 1)
        self.assertIsNotNone(self.database.get_order_by_number('1'))

    def test_rules_are_unicode_case_insensitive_exact_names_and_sql_literal(self):
        self.order('1', [{'name': 'Товар 50%_ красный', 'price': 100}])
        self.order('2', [{'name': 'Товар 50%_ красный extra', 'price': 200}])
        self.order('3', [{'name': 'Товар 500 красный', 'price': 300}])
        exact = self.database.save_sales_category('Точный', parse_rules('name:ТОВАР 50%_ КРАСНЫЙ'))
        partial = self.database.save_sales_category('Часть', parse_rules('50%_'))
        injection = self.database.save_sales_category('SQL', parse_rules("' OR 1=1 --"))
        self.assertEqual(self.database.sales_summary(exact)['items_quantity'], 1)
        self.assertEqual(self.database.sales_summary(partial)['items_quantity'], 2)
        self.assertEqual(self.database.sales_summary(injection)['items_quantity'], 0)
        with self.assertRaises(ValueError):
            parse_rules('sku:')
        with self.assertRaises(ValueError):
            parse_rules('')

    def test_repeated_tilda_payload_does_not_duplicate_sales(self):
        products = [{'name': 'Ветровка', 'price': 100}]
        first = self.order('1', products)
        second = self.order('1', products)
        self.assertEqual(first, second)
        self.assertEqual(self.database.sales_summary('windbreaker')['items_quantity'], 1)

    def test_ziphoodie_aliases_include_russian_and_english_model_names(self):
        for index, name in enumerate(['DEMOZIP ZIP-HOODIE', 'DEMOZIP ZIP HOODIE', 'Зип-худи демозип', 'ЗИП-ХУДИ ДЕМОЗИП']):
            self.order(str(index), [{'name': name, 'price': 100}])
        self.order('other', [{'name': 'ZIP HOODIE OTHER', 'price': 900}])
        self.assertEqual(self.database.sales_summary('ziphoodie')['items_quantity'], 4)
        self.assertEqual(self.database.sales_summary('ziphoodie')['revenue_kopecks'], 40000)

    def test_existing_category_settings_survive_adding_new_default(self):
        with self.database.connect() as connection:
            connection.execute("DELETE FROM sales_categories WHERE key='ziphoodie'")
        self.database.save_sales_category('Мои ветровки', parse_rules('sku:MY-WB'), key='windbreaker')
        self.database.set_sales_category_active('longsleeve', False)
        self.database.initialize()
        self.assertEqual(self.database.get_sales_category('windbreaker')['rules'], parse_rules('sku:MY-WB'))
        self.assertEqual(self.database.get_sales_category('windbreaker')['label'], 'Мои ветровки')
        self.assertFalse(self.database.get_sales_category('longsleeve')['is_active'])
        self.assertIsNotNone(self.database.get_sales_category('ziphoodie'))

    def test_missing_selected_product_has_no_report(self):
        with self.assertRaises(ValueError):
            self.database.sales_summary('i99999')

    def test_model_breakdown_merges_colors_sizes_and_counts_overlapping_rules_once(self):
        self.order('mixed', [
            {'name': 'DEMO PUFFER BLACK', 'size': 'S', 'quantity': 2, 'price': 100},
            {'name': 'DEMO PUFFER RED', 'size': 'M', 'quantity': 3, 'price': 100},
            {'name': 'DEMOZIP ZIP BLACK', 'size': 'L', 'quantity': 4, 'price': 100},
            {'name': 'DEMO WINDBREAKER GREY', 'size': 'M', 'quantity': 1, 'price': 100},
            {'name': 'BASE BLACK', 'quantity': 2, 'price': 100},
        ], discount_kopecks=12000, created_at=1000)
        self.database.save_sales_category('Все куртки', parse_rules('PUFFER\nWINDBREAKER'))
        rows = self.database.sales_model_breakdown(1000, 2000)
        models = {row['model']: row for row in rows}
        self.assertEqual(models['DEMO PUFFERS']['quantity'], 5)
        self.assertEqual(models['DEMOZIP ZIP']['quantity'], 4)
        self.assertEqual(models['DEMO WINDBREAKER']['quantity'], 1)
        self.assertEqual(models['Другие изделия']['quantity'], 2)
        self.assertNotIn('Все куртки', models)
        self.assertFalse(any('BLACK' in name or 'GREY' in name or 'RED' in name for name in models))
        summary = self.database.sales_summary('all', 1000, 2000)
        self.assertEqual(sum(r['quantity'] for r in rows), summary['items_quantity'])
        self.assertEqual(sum(r['revenue_kopecks'] for r in rows), summary['revenue_kopecks'])

    def test_model_breakdown_respects_dates_status_and_custom_current_products(self):
        self.order('start', [{'name': 'Шарф', 'quantity': 2, 'price': 100}], created_at=1000)
        self.order('outside', [{'name': 'Шарф', 'quantity': 7, 'price': 100}], created_at=2000)
        self.order('cancelled', [{'name': 'Шарф', 'quantity': 5, 'price': 100}], status='cancelled', created_at=1000)
        self.database.save_sales_category('SCARF', parse_rules('шарф'))
        self.assertEqual(self.database.sales_model_breakdown(1000, 2000), [
            {'model': 'SCARF', 'quantity': 2, 'revenue_kopecks': 20000},
        ])

    def test_retiring_longsleeves_on_existing_database_preserves_rules_and_orders(self):
        order_id = self.order('old', [{'name': 'BASE', 'price': 100}])
        self.database.save_sales_category('Старые лонгсливы', parse_rules('BASE'), key='longsleeve')
        with self.database.connect() as connection:
            connection.execute("DELETE FROM app_settings WHERE key='retired_longsleeve_v1'")
            connection.execute("UPDATE sales_categories SET is_active=1 WHERE key='longsleeve'")
        self.database.initialize()
        legacy = self.database.get_sales_category('longsleeve')
        self.assertFalse(legacy['is_active'])
        self.assertEqual(legacy['label'], 'Старые лонгсливы')
        self.assertEqual(legacy['rules'], parse_rules('BASE'))
        self.assertEqual(self.database.get_order(order_id)['items'][0]['product_name'], 'BASE')
        self.assertEqual(self.database.sales_summary('all')['items_quantity'], 1)


if __name__ == '__main__':
    unittest.main()
