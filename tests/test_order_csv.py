import csv
import unittest
from io import StringIO

from import_tilda_csv import parse_products
from order_csv import export_orders_csv


class OrderCsvTest(unittest.TestCase):
    def test_tilda_compatible_export_uses_current_values(self) -> None:
        content = export_orders_csv(
            [
                {
                    "order_number": "1234567890",
                    "created_at": 1_757_347_200,
                    "raw_source_json": '{"paymentid":"pay-1","custom":"value"}',
                    "customer_name": "Иван",
                    "phone": "+79990000000",
                    "email": "mail@example.com",
                    "telegram_username": "loaded",
                    "delivery_method": "CDEK = 500",
                    "delivery_address": "Москва",
                    "delivery_city": "Москва",
                    "payment_method": "Полная оплата",
                    "promo_code": "SALE",
                    "discount_kopecks": 10000,
                    "total_kopecks": 250000,
                    "currency": "RUB",
                    "status": "processing",
                    "cdek_number": "TRACK-1",
                    "items": [
                        {
                            "product_name": "Худи",
                            "size": "M",
                            "quantity": 2,
                            "unit_price_kopecks": 125000,
                            "item_status": "active",
                        },
                        {
                            "product_name": "Старый товар",
                            "size": "L",
                            "quantity": 1,
                            "unit_price_kopecks": 100000,
                            "item_status": "cancelled",
                        },
                    ],
                }
            ]
        )
        rows = list(csv.DictReader(StringIO(content.decode("utf-8-sig"))))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["tilda_orderid"], "1234567890")
        self.assertEqual(row["paymentid"], "pay-1")
        self.assertEqual(row["custom"], "value")
        self.assertEqual(row["crm_status"], "processing")
        self.assertEqual(row["cdek_number"], "TRACK-1")
        self.assertEqual(parse_products(row["product"])[0]["size"], "M")
        self.assertNotIn("Старый товар", row["product"])


if __name__ == "__main__":
    unittest.main()
