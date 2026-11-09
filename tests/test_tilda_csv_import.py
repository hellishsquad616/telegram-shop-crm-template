import tempfile
import unittest
from pathlib import Path

from database import Database
from import_tilda_csv import import_paid_orders, is_paid_row, parse_products


class TildaCsvImportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp_dir.name) / "test.sqlite3")
        self.database.initialize()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_paid_marker_requires_transaction_and_allowed_status(self) -> None:
        self.assertTrue(is_paid_row({"paymentid": "pay-1", "tilda_status": "sent"}))
        self.assertFalse(is_paid_row({"paymentid": "", "tilda_status": "blocked"}))
        self.assertFalse(is_paid_row({"paymentid": "pay-1", "tilda_status": "status3"}))

    def test_parse_multiple_products(self) -> None:
        items = parse_products(
            "DEMO PUFFER BLACK (Размер: S) - 1x11990 = 11990; "
            "DEMO PUFFER RED (Размер: M) - 2x11990 = 23980"
        )
        self.assertEqual(len(items), 2)
        self.assertEqual(items[1]["size"], "M")
        self.assertEqual(items[1]["quantity"], 2)
        self.assertEqual(items[1]["unit_price_kopecks"], 1_199_000)

    def test_parse_product_without_size(self) -> None:
        item = parse_products("test - 1x10 = 10")[0]
        self.assertEqual(item["product_name"], "test")
        self.assertEqual(item["size"], "")

    def test_import_is_paid_only_and_idempotent(self) -> None:
        base = {
            "created": "2026-08-21 15:00:00",
            "name": "Тест",
            "phone": "+79990000000",
            "email": "customer@example.com",
            "paymentsystem": "yakassa",
            "product": "DEMO PUFFER BLACK (Размер: S) - 1x11990 = 11990",
            "amount": "11990",
            "tilda_order_currency": "RUB",
            "order_delivery_name": "CDEK = 500",
            "ваш_username_в_telegram": "@test",
            "promocode": "",
            "discount": "",
        }
        paid = dict(base, tilda_orderid="100", tilda_status="sent", paymentid="pay-1")
        blocked = dict(base, tilda_orderid="101", tilda_status="blocked", paymentid="")
        result = import_paid_orders(self.database, [paid, blocked])
        self.assertEqual(result["imported"], 1)
        self.assertEqual(result["unpaid"], 1)
        order = self.database.get_order_by_number("100")
        self.assertEqual(order["status"], "paid")
        self.assertEqual(order["email"], "customer@example.com")
        self.assertEqual(order["delivery_method"], "CDEK = 500")
        self.assertEqual(order["delivery_cost_kopecks"], 50_000)
        repeated = import_paid_orders(self.database, [paid])
        self.assertEqual(repeated["duplicates"], 1)
        self.assertEqual(self.database.dashboard()["orders_count"], 1)
